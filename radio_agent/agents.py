"""Agent backends. Each one takes what the operator said and returns text in SAY/DETAIL/CONFIRM form."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.request

from .events import log

RADIO_PROMPT = """You are being operated by voice over amateur radio. The operator holds a handheld radio; \
what you receive is a speech-to-text transcript, so expect mis-heard words (e.g. "dev" for "div", \
"get" for "git", "pie test" for "pytest"). Read it the way you'd read a message full of \
typos: use the conversation so far and the files in the project to work out what was meant. If you can \
make a confident guess, act on it and say in the SAY line what you understood ("I took that as …"). \
Only ask them to repeat if it's truly unintelligible.

Format EVERY reply exactly like this:
SAY: one to three short plain sentences that will be read aloud over the radio. No code, no file paths \
longer than a filename, no URLs, no markdown, no lists. Talk like a calm colleague on the radio. Never \
say "over" or a callsign – the station adds those.
DETAIL: (optional) anything that doesn't speak well – code, diffs, paths, command output. This goes to the \
operator's phone screen.
CONFIRM: (only if needed) a one-line description of the change or command you want to make. Use this \
whenever you would modify files or run anything other than reading. Do not make the change yet – wait \
until the operator confirms.

You are running read-only until the operator confirms. Never mention plan mode, permission modes, \
keyboard shortcuts or the computer's screen settings – the operator is away from the keyboard, holding a \
radio. The radio station handles confirmation: just give a CONFIRM line and stop. When it runs your \
confirmed action you will have edit permissions.

Only write the SAY line for the operator; never invent their side of the conversation. Keep content \
suitable for amateur radio: plain language, no profanity, no business advertising."""

DIRECT_PROMPT = RADIO_PROMPT.split("CONFIRM: (only if needed)")[0] + """\
Act on instructions straight away: make the change, run what's needed, then report what you did in the \
SAY line. Don't ask for confirmation and don't output a CONFIRM line. Only ask a question if the \
instruction is genuinely unclear. Never mention permission modes or keyboard shortcuts – the operator \
is away from the keyboard, holding a radio. Keep content suitable for amateur radio."""


SSTV_PROMPT = ("SSTV: (optional) the station can send the operator a slow-scan TV picture of its location. "
               "If they ask for a picture, an image or an SSTV, add the line 'SSTV: picture' and mention it "
               "in the SAY line. The station strips that line and sends the picture.")


SIGNOFF_PROMPT = ("SIGNOFF: (optional) when the conversation is coming to an end – they say 73, goodbye or thanks "
                  "for the contact, or you've clearly wrapped up – say your farewell in the SAY line and add the "
                  "line 'SIGNOFF: yes'. The station then signs off with your callsign.")


def with_persona(prompt: str, cfg: dict) -> str:
    """The radio rules plus what the station can do (SSTV) and agent.persona, a character for it to play.

    The persona can use {callsign}, {operator}, {name}, {qth} and {locator} from the station settings.
    """
    st = cfg["station"]
    fields = {"callsign": st.get("callsign", ""), "operator": st.get("operator") or "the operator",
              "name": st.get("name", ""), "qth": st.get("qth", ""), "locator": st.get("locator", "")}
    persona = (cfg["agent"].get("persona") or "").strip()
    for k, v in fields.items():
        persona = persona.replace("{" + k + "}", str(v))
    return "\n\n".join(p for p in (prompt, SSTV_PROMPT, SIGNOFF_PROMPT, persona) if p)


CONFIRMED_PROMPT = "The operator confirmed over the radio. Go ahead and do this now: {action}\n" \
                   "Then reply in the SAY/DETAIL format with the result (test results, what changed)."


class Agent:
    def ask(self, text: str) -> str: ...
    def execute(self, action: str) -> str: ...
    def reset(self) -> None: ...


class EchoAgent(Agent):
    """Repeats you back. For testing the radio side."""

    def ask(self, text: str) -> str:
        if "change" in text.lower() or "fix" in text.lower():
            return f"SAY: I'd make that change: {text}. Shall I go ahead?\nCONFIRM: {text}"
        return f"SAY: I heard: {text}\nDETAIL: echo backend, nothing was run."

    def execute(self, action: str) -> str:
        return f"SAY: Done. Pretended to {action}.\nDETAIL: echo backend – no files touched."

    def reset(self) -> None: ...


def _find_claude() -> str:
    """Find the Claude Code CLI, including the usual Windows install spots not always on PATH."""
    found = shutil.which("claude")
    if found:
        return found
    home = os.path.expanduser("~")
    for c in [os.path.join(home, ".local", "bin", "claude.exe"),
              os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd"),
              os.path.join(home, ".local", "bin", "claude")]:
        if os.path.exists(c):
            return c
    return "claude"


class ClaudeCodeAgent(Agent):
    """Drives the Claude Code CLI headless in the project folder.

    One `claude` process stays running between overs (stream-json in and out), so each over only
    waits for the model, not for the CLI to start up. It's started as soon as the station comes up.

    Questions run in plan mode (read-only). Once the operator says "confirm", the process is
    restarted in the edit mode and resumes the same session with the allowed tools.
    """

    def __init__(self, cfg: dict):
        a = cfg["agent"]
        self.exe = a.get("claude_path") or _find_claude()
        self.workdir = a["workdir"]
        self.allowed = a["allowed_tools"]
        self.model = a.get("model")
        self.timeout = a["timeout_seconds"]
        # What a confirmed action may do: "acceptEdits" = file edits + allowed_tools only;
        # "bypassPermissions" = anything, no questions asked (your radio confirm is the only gate)
        self.execute_mode = a.get("execute_mode", "acceptEdits")
        # require_confirm: false = "direct" mode – act on the instruction immediately, no confirm step
        self.require_confirm = a.get("require_confirm", True)
        self.cfg = cfg
        self.session_id: str | None = None
        os.makedirs(self.workdir, exist_ok=True)
        self.last_cost: float | None = None
        self.proc: subprocess.Popen | None = None
        self.proc_mode: str | None = None
        self.lines: queue.Queue = queue.Queue()
        self.errors: list[str] = []
        self.api_ms = 0  # stream-json reports thinking time as a running total for the process
        try:
            self._start(self._ask_mode())  # warm up now so the first over is quick too
        except OSError:
            pass  # reported properly on the first over

    def _ask_mode(self) -> str:
        return "plan" if self.require_confirm else self.execute_mode

    def _start(self, mode: str) -> None:
        system = with_persona(RADIO_PROMPT if self.require_confirm else DIRECT_PROMPT, self.cfg)
        if os.name == "nt":
            # On Windows `claude` may be a .cmd shim; cmd.exe mangles newlines and double quotes in arguments
            system = " ".join(system.replace('"', "'").split())
        cmd = [self.exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--append-system-prompt", system, "--permission-mode", mode]
        if mode != "plan" and self.allowed:
            cmd += ["--allowedTools", *self.allowed]
        if self.model:
            cmd += ["--model", self.model]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        self.proc = subprocess.Popen(cmd, cwd=self.workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        self.proc_mode = mode
        self.lines = queue.Queue()
        self.errors = []
        self.api_ms = 0
        threading.Thread(target=self._pump, args=(self.proc.stdout, self.lines), daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc.stderr, None), daemon=True).start()

    def _pump(self, stream, out: queue.Queue | None) -> None:
        for line in stream:
            if out is None:
                self.errors = (self.errors + [line.strip()])[-20:]
            else:
                out.put(line)
        if out is not None:
            out.put(None)  # process ended

    def _stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
        self.proc = None

    def _send(self, prompt: str, mode: str) -> None:
        if self.proc is None or self.proc.poll() is not None or self.proc_mode != mode:
            self._stop()
            self._start(mode)
        msg = json.dumps({"type": "user", "message": {"role": "user", "content": prompt}})
        self.proc.stdin.write(msg + "\n")
        self.proc.stdin.flush()

    def _run(self, prompt: str, mode: str) -> str:
        t0 = time.time()
        try:
            try:
                self._send(prompt, mode)
            except OSError:  # it died while idle: start a fresh one on the same session
                self._stop()
                self._send(prompt, mode)
        except FileNotFoundError:
            return ("SAY: I can't find Claude Code on this computer. Check the screen.\n"
                    "DETAIL: The `claude` command wasn't found. Install Claude Code and log in, "
                    "or set agent.claude_path in config.yaml.")
        while True:
            left = self.timeout - (time.time() - t0)
            try:
                line = self.lines.get(timeout=max(left, 0.1))
            except queue.Empty:
                self.proc.kill()
                self.proc = None
                return "SAY: That took too long, so I stopped it. Try a smaller request."
            if line is None:
                self.proc = None
                detail = "\n".join(self.errors) or "Claude Code stopped unexpectedly."
                return f"SAY: Claude Code hit an error. Details are on your screen.\nDETAIL: {detail}"
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "result":
                break
        api_ms, self.api_ms = data.get("duration_api_ms", 0) - self.api_ms, data.get("duration_api_ms", 0)
        log.info(f"   ⏱ Claude Code: {time.time() - t0:.1f}s total, {api_ms / 1000:.1f}s of it thinking"
                 f" ({data.get('num_turns', '?')} turns)")
        self.last_stats = {"name": "Claude Code", "seconds": time.time() - t0, "thinking": api_ms / 1000,
                           "turns": data.get("num_turns")}
        self.session_id = data.get("session_id", self.session_id)
        self.last_cost = data.get("total_cost_usd")
        if data.get("is_error"):
            return f"SAY: Claude Code hit an error. Details are on your screen.\nDETAIL: {data.get('result', '')}"
        return data.get("result", "")

    def ask(self, text: str) -> str:
        return self._run(text, self._ask_mode())

    def execute(self, action: str) -> str:
        return self._run(CONFIRMED_PROMPT.format(action=action), self.execute_mode)

    def reset(self) -> None:
        self._stop()
        self.session_id = None
        try:
            self._start(self._ask_mode())
        except OSError:
            pass


class _ChatAgent(Agent):
    """Shared history handling for plain chat APIs (no file access)."""

    def __init__(self, cfg: dict):
        self.model = cfg["agent"].get("model")
        self.system = with_persona(RADIO_PROMPT, cfg)
        self.history: list[dict] = []

    def _complete(self, messages: list[dict]) -> str:  # pragma: no cover - network
        raise NotImplementedError

    def ask(self, text: str) -> str:
        self.history.append({"role": "user", "content": text})
        out = self._complete(self.history[-20:])
        self.history.append({"role": "assistant", "content": out})
        return out

    def execute(self, action: str) -> str:
        return self.ask(CONFIRMED_PROMPT.format(action=action) + "\n(You have no tools: describe exactly what to do.)")

    def reset(self) -> None:
        self.history = []


def _post(url: str, body: dict, headers: dict) -> dict:  # pragma: no cover - network
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


class AnthropicAgent(_ChatAgent):
    def _complete(self, messages):  # pragma: no cover - network
        d = _post("https://api.anthropic.com/v1/messages",
                  {"model": self.model or "claude-sonnet-4-5", "max_tokens": 1500, "system": self.system, "messages": messages},
                  {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"})
        return "".join(b.get("text", "") for b in d["content"])


class OpenAIAgent(_ChatAgent):
    def _complete(self, messages):  # pragma: no cover - network
        d = _post("https://api.openai.com/v1/chat/completions",
                  {"model": self.model or "gpt-4o-mini", "messages": [{"role": "system", "content": self.system}, *messages]},
                  {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})
        return d["choices"][0]["message"]["content"]


class OllamaAgent(_ChatAgent):
    """Local model – works with no internet. Handy for portable / emergency-comms setups."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.url = cfg["agent"]["ollama_url"].rstrip("/")

    def _complete(self, messages):  # pragma: no cover - network
        d = _post(f"{self.url}/api/chat",
                  {"model": self.model or "llama3.1:8b", "stream": False,
                   "messages": [{"role": "system", "content": self.system}, *messages]}, {})
        return d["message"]["content"]


def make(cfg: dict) -> Agent:
    b = cfg["agent"]["backend"]
    return {
        "echo": lambda c: EchoAgent(),
        "claude-code": ClaudeCodeAgent,
        "anthropic": AnthropicAgent,
        "openai": OpenAIAgent,
        "ollama": OllamaAgent,
    }[b](cfg)
