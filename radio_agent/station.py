"""The station: decides whether an over is for us, talks to the agent, and manages confirm/lock state.

Kept free of audio so it can be tested with plain strings.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Callable

from . import speech
from .agents import Agent
from .events import log
from .sidecar import Log


class Station:
    def __init__(self, cfg: dict, agent: Agent, log: Log | None = None,
                 ack: Callable[[str], None] | None = None, clock: Callable[[], float] = time.time,
                 on_sstv: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.agent = agent
        self.log = log or Log()
        self.ack = ack or (lambda s: None)
        self.on_sstv = on_sstv or (lambda subject: None)  # send a slow-scan picture (doesn't block)
        self._sstv_this_over = False
        self.signoff = False  # the reply just given ends the QSO: sign off with the callsign
        self.clock = clock
        lc = cfg["listen"]
        self.wake = [w.lower() for w in lc["wake_words"]]
        self.window = lc["conversation_window_seconds"]
        self.pin = str(lc["pin"]) if lc.get("pin") not in (None, "") else None
        self.lock_after = lc["lock_after_idle_seconds"]
        self.locked = self.pin is not None
        self.require_wake = lc.get("require_wake_word", True)
        # Only say "stand by" if the agent is still thinking after this long (0 = always say it first)
        self.standby_after = float(lc.get("standby_after_seconds", 3))
        self._pool = ThreadPoolExecutor(max_workers=1)
        self.last_ignored = ""
        self.last_activity = -1e9
        self.pending: str | None = None
        self.last_say = ""
        self.last_request = ""
        self._started: float | None = None  # when the agent began on the current over

    # ------------------------------------------------------------------
    def in_conversation(self) -> bool:
        return self.clock() - self.last_activity < self.window

    def handle(self, text: str = "", dtmf: str = "") -> str | None:
        """Process one over. Returns what to say back, or None to stay silent."""
        text = (text or "").strip()
        now = self.clock()
        self.signoff = False

        if self.pin and now - self.last_activity > self.lock_after:
            self.locked = True

        if self.pin and f"*{self.pin}#" in dtmf:
            self.locked = False
            return self._done(f"[DTMF unlock]", "Unlocked. Go ahead.")

        addressed = (not self.require_wake) or speech.has_wake_word(text, self.wake) or self.in_conversation()
        if self.locked:
            if speech.has_wake_word(text, self.wake):
                return "Station locked. Send your PIN on the keypad."
            return None

        cmd = speech.command_of(text, self.wake) if (text and addressed) else None
        if not cmd and self.pending and text and addressed:
            cmd = speech.loose_command(text, self.wake)
        for d in dtmf:
            if d in speech.DTMF_COMMANDS:
                cmd = speech.DTMF_COMMANDS[d]
                break

        if cmd:
            return self._command(cmd, text or f"[DTMF {dtmf}]")

        if not text or not addressed:
            if text:
                self.last_ignored = text
                log.info(f"   (ignored – no wake word: say \"{self.wake[0].title()}, …\" to start)")
                self.log.bus.emit("ignored", text=text, reason="no wake word")
            return None  # not for us – stay off the air

        request = speech.strip_wake_word(speech.strip_over(text), self.wake)
        self.last_request = request
        # Asked for a picture? Send it straight away; the agent still answers as normal
        self._sstv_this_over = bool(request) and speech.wants_sstv(request)
        if self._sstv_this_over:
            self.on_sstv("picture")
        if not request:
            return self._done(text, "Listening. Go ahead.")

        self.last_activity = now  # keep the conversation open while we think
        raw = self._think(lambda: self.agent.ask(request), "Roger, stand by.")
        return self._agent_reply(text, raw)

    def _think(self, work: Callable[[], str], standby: str) -> str:
        """Run the agent; only say `standby` if it hasn't answered within standby_after seconds."""
        self._started = time.time()
        self.log.bus.emit("agent_started", request=self.last_request)
        if self.standby_after <= 0:
            self.ack(standby)
            return work()
        job = self._pool.submit(work)
        try:
            return job.result(timeout=self.standby_after)
        except FutureTimeout:
            self.ack(standby)
            return job.result()

    # ------------------------------------------------------------------
    def _command(self, cmd: str, heard: str) -> str:
        if cmd == "confirm":
            if not self.pending:
                return self._done(heard, "Nothing waiting for confirmation.")
            action, self.pending = self.pending, None
            self.last_request = action
            raw = self._think(lambda: self.agent.execute(action), "Roger, going ahead. Stand by.")
            return self._agent_reply(heard, raw)
        if cmd == "cancel":
            had = self.pending
            self.pending = None
            return self._done(heard, "Cancelled. Nothing was changed." if had else "Nothing to cancel.")
        if cmd == "repeat":
            return self._done(heard, self.last_say or "Nothing to repeat yet.", remember=False)
        if cmd == "reset":
            self.agent.reset()
            self.pending = None
            return self._done(heard, "New session started.")
        if cmd == "close":
            self.log.bus.emit("sstv_close")
            return self._done(heard, "Picture closed.")
        if cmd == "status":
            msg = f"Waiting for your confirm on: {self.pending}" if self.pending else "Idle and listening."
            return self._done(heard, msg)
        return self._done(heard, "Say again?")

    def _agent_reply(self, heard: str, raw: str) -> str:
        r = speech.parse_agent_output(raw)
        if r.sstv and not self._sstv_this_over:  # the agent decided to send one ("SSTV: picture")
            self.on_sstv(r.sstv)
        self._sstv_this_over = False
        self.signoff = r.signoff or speech.sounds_like_goodbye(heard)
        self.pending = r.confirm
        say = r.say or "Done."
        if r.confirm:
            say += " Say confirm, or key one, to go ahead. Key nine to cancel."
        return self._done(heard, say, detail=r.detail)

    def _done(self, heard: str, say: str, detail: str = "", remember: bool = True) -> str:
        self.last_activity = self.clock()
        if remember:
            self.last_say = say
        self.log.add(heard, say, detail, self.pending)
        stats = {}
        if self._started is not None:
            stats = {"seconds": time.time() - self._started, **getattr(self.agent, "last_stats", {})}
            stats.setdefault("name", type(self.agent).__name__.replace("Agent", ""))
            self._started = None
        self.log.bus.emit("agent_done", say=say, detail=detail, pending=self.pending, stats=stats)
        return say
