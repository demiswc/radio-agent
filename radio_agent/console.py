"""The run.bat screen, styled after the Claude Code terminal: a banner, one block per exchange,
and a status line pinned at the bottom. Driven entirely by station events (see events.py).

Finished blocks are printed into the terminal's scrollback above the live status line, so the
conversation scrolls naturally and the newest exchange is always at the bottom.
"""
from __future__ import annotations

import time
from collections import deque

from rich.console import Console, Group
from rich.live import Live
from rich.padding import Padding
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import dial_mhz

AMBER = "#d9a441"
CLAY = "#d97757"
OVER_BG = "on grey23"

# An original little base-station radio: antenna, dial, speaker grille
ICON = [
    [("▝▚", AMBER), ("▄▄▄▄▄▖", AMBER)],
    [(" ▐", AMBER), ("▪", "bold white"), (" ", ""), ("▓▓▓", "grey50"), ("▌", AMBER)],
    [(" ▝▀▀▀▀▀▘", AMBER)],
]

AGENT_NAMES = {"claude-code": "Claude Code", "anthropic": "Anthropic API", "openai": "OpenAI",
               "ollama": "Ollama", "echo": "Echo"}


def banner(cfg: dict) -> Table:
    a, s, r, st = cfg["agent"], cfg["stt"], cfg["radio"], cfg["station"]
    agent = AGENT_NAMES.get(a["backend"], a["backend"]) + (f" ({a['model']})" if a.get("model") else "")
    stt = f"STT {s['model']} on {s.get('device', 'cpu')}" if s["backend"] == "faster-whisper" else f"STT {s['backend']}"
    me = st["callsign"] + (f" ({st['name']})" if st.get("name") else "")
    if st.get("operator") and st["operator"] != st["callsign"]:
        me += f" ⇄ {st['operator']}"
    third = [me, r["mode"], f"reply: {r.get('reply', 'radio')}"]
    third.append(f"{dial_mhz(cfg)} MHz")
    lines = [Text.assemble(("radio-agent", "bold"), (f" v{__version__}", "bold")),
             Text(f"{agent} · {stt}", style="dim"),
             Text(" · ".join(third), style="dim")]
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)
    grid.add_column()
    for icon, line in zip(ICON, lines):
        grid.add_row(Text.assemble(*icon), line)
    return grid


def level_meter(level: float, squelch: float, cells: int = 10) -> Text:
    """▁▂▃▄▅▆▇█ bar, scaled so the squelch threshold sits a third of the way up."""
    full = max(squelch * 3, 1e-6)
    n = max(0, min(cells, round(level / full * cells)))
    bars = "▁▂▃▄▅▆▇█"
    t = Text()
    for i in range(cells):
        ch = bars[min(len(bars) - 1, i * len(bars) // cells)] if i < n else "·"
        t.append(ch, style=("green" if i < cells * .6 else "yellow" if i < cells * .85 else "red") if i < n else "grey35")
    return t


class ConsoleUI:
    def __init__(self, cfg: dict, bus, level=lambda: 0.0, console: Console | None = None):
        self.cfg = cfg
        self.level = level
        self.squelch = float(cfg["radio"]["squelch_threshold"])
        self.name = cfg["station"].get("name") or "Claude"
        self.console = console or Console(highlight=False)
        self.history: deque = deque(maxlen=200)  # last exchanges, as printed
        self.spinner = Spinner("dots", style=CLAY)
        self.rx_since: float | None = None
        self.wait_until: float | None = None
        self.think_since: float | None = None
        self.tx_since: float | None = None
        self.tx_text = ""
        self.flash: tuple[str, float] | None = None
        self.last_reply_s: float | None = None
        self.sstv: tuple[str, float, float] | None = None  # mode, started, seconds
        self.cq_next: float | None = None
        self.cq_paused = False
        self.calling_cq = False
        self.live: Live | None = None
        self.ready = False  # set once Whisper, the voice and the agent have loaded
        bus.subscribe(self.on_event)

    # ---------- lifecycle ----------
    def start(self) -> None:
        self.console.print()
        self.console.print(banner(self.cfg))
        self.console.print()
        self.live = Live(get_renderable=self.status, console=self.console, refresh_per_second=10,
                         transient=True, redirect_stdout=False, redirect_stderr=False)
        self.live.start()

    def stop(self) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None

    def note(self, text: str, style: str = "dim") -> None:
        """A one-off line in the scrollback (e.g. the sidecar URLs at start-up)."""
        self.console.print(Text(text, style=style))

    # ---------- events ----------
    def on_event(self, name: str, d: dict) -> None:
        now = time.time()
        if name == "over_started":
            self.rx_since, self.wait_until = now, None
        elif name == "stt_done":
            self.rx_since = None
        elif name == "over_waiting":
            self.rx_since, self.wait_until = None, now + float(d.get("timeout", 0))
        elif name == "over_finished":
            self.rx_since = self.wait_until = None
            self._block(self._over(d))
        elif name == "ignored":
            self.rx_since = None
            self.flash = (f"(ignored – {d.get('reason', 'noise')})", now + 2)
        elif name == "agent_started":
            self.think_since = now
        elif name == "agent_done":
            self.think_since = None
            self.last_reply_s = d.get("stats", {}).get("seconds")
            self._block(self._reply(d))
        elif name == "tx_started":
            self.calling_cq = bool(d.get("cq"))
            if self.calling_cq:
                self._block(Text(f"  📣 {d.get('display') or d['text']}", style="green"))
            self.tx_since, self.tx_text = now, ("" if not d.get("ack") else d["text"])
        elif name == "tx_done":
            self.tx_since, self.calling_cq = None, False
        elif name == "cq_next":
            self.cq_next, self.cq_paused = float(d["at"]), False
        elif name == "cq_paused":
            self.cq_paused = True
        elif name == "sstv":
            self.sstv = (d["mode"], now, float(d["seconds"]))
        elif name == "sstv_done":
            if self.sstv:
                self._block(Text(f"  ▦ SSTV {self.sstv[0]} sent · {d.get('path', '')}", style="dim"))
            self.sstv = None
        elif name == "error":
            self._block(self._error(d))

    def _block(self, renderable) -> None:
        self.history.append(renderable)
        self.console.print(renderable)

    # ---------- blocks ----------
    def _over(self, d: dict):
        row = Padding(Text.assemble(("› ", "bold"), d["text"]), (0, 1), style=OVER_BG, expand=True)
        bits = []
        if d.get("rx_s"):
            bits.append(f"RX {d['rx_s']:.1f}s")
        if d.get("stt_s"):
            bits.append(f"STT {d['stt_s']:.1f}s")
        if d.get("ended_by"):
            bits.append(f"ended by {d['ended_by']}")
        meta = Text("  " + " · ".join(bits), style="dim") if bits else Text()
        return Group(Text(), row, meta)

    def _reply(self, d: dict):
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=1, no_wrap=True)
        grid.add_column(ratio=1)
        grid.add_row(Text("●", style="bold white"), Text(d["say"]))
        parts = [grid]
        if d.get("detail"):
            lines = d["detail"].splitlines()
            shown = "\n".join(lines[:8]) + (f"\n… {len(lines) - 8} more lines on the sidecar page" if len(lines) > 8 else "")
            parts.append(Padding(Text(shown, style="grey62"), (0, 0, 0, 2)))
        s = d.get("stats") or {}
        if s.get("seconds") is not None:
            line = f"{s.get('name', 'Agent')} {s['seconds']:.1f}s"
            if s.get("thinking"):
                line += f" ({s['thinking']:.1f}s thinking)"
            if s.get("turns"):
                line += f" · {s['turns']} turn{'s' if s['turns'] != 1 else ''}"
            parts.append(Text("  " + line, style="dim"))
        return Group(Text(), *parts)

    def _error(self, d: dict):
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=1, no_wrap=True)
        grid.add_column(ratio=1)
        grid.add_row(Text("●", style="bold red"), Text(d["summary"], style="red"))
        return Group(Text(), grid, Text("  full traceback in logs/radio-agent.log", style="dim"))

    # ---------- status line ----------
    def status(self):
        now = time.time()
        left = Text()
        if not self.ready:
            left.append_text(self.spinner.render(time.monotonic()))
            left.append(" starting up – loading speech recognition and the agent…", style="dim")
        elif self.sstv is not None:
            mode, t0, secs = self.sstv
            done = min(1.0, (now - t0) / secs) if secs else 1.0
            left.append("▦ SSTV", style="bold cyan")
            left.append(f" {mode} ", style="cyan")
            left.append("█" * round(done * 20) + "░" * (20 - round(done * 20)), style="cyan")
            left.append(f" {now - t0:.0f}s / {secs:.0f}s", style="dim")
        elif self.tx_since is not None:
            left.append("TX", style="bold red")
            if self.calling_cq:
                left.append(" calling CQ", style="green")
            left.append(f" {now - self.tx_since:.0f}s", style="red")
            if self.tx_text:
                left.append(f" · {self.tx_text}", style="dim")
        elif self.think_since is not None:
            left.append_text(self.spinner.render(time.monotonic()))
            left.append(f" {self.name} is thinking… ", style=CLAY)
            left.append(f"{now - self.think_since:.0f}s", style="dim")
        elif self.rx_since is not None:
            left.append("● ", style="bold green")
            left.append(f"RX {now - self.rx_since:.1f}s ", style="green")
            left.append_text(level_meter(self.level(), self.squelch))
        elif self.wait_until is not None and now < self.wait_until:
            left.append(f"… waiting for 'over' ({self.wait_until - now:.1f}s)", style="yellow")
        elif self.flash and now < self.flash[1]:
            left.append(self.flash[0], style="dim italic")
        else:
            left.append("▸▸ ", style=AMBER)
            lvl = self.level()
            left.append("listening · ", style="dim")
            left.append("squelch open " if lvl > self.squelch else "squelch closed", style="dim")
            if lvl > self.squelch:
                left.append_text(level_meter(lvl, self.squelch))
            if self.last_reply_s is not None:
                left.append(f" · last reply {self.last_reply_s:.1f}s", style="dim")
            if self.cq_next is not None:
                left.append(" · CQ paused (QSO)" if self.cq_paused else f" · CQ in {max(0, self.cq_next - now):.0f}s",
                            style="green dim")
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(justify="right", no_wrap=True)
        grid.add_row(left, Text("ctrl+c to quit", style="dim"))
        return Group(Text(), grid)
