"""The station calls CQ on its own every so often, like a real operator looking for contacts.

It stops while a QSO is going on (someone talking, the agent thinking or replying, a picture
being sent) and only starts calling again once things have been quiet for quiet_after_seconds.
Driven by the station's events, so its own CQ calls never count as a QSO.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Callable

from . import speech
from .events import Bus, log

# events that mean a conversation is happening (an ignored blip or noise doesn't count)
QSO_EVENTS = {"stt_done", "over_waiting", "over_finished", "agent_started", "agent_done", "tx_started", "tx_done",
              "sstv", "sstv_done"}


class CQCaller:
    def __init__(self, cfg: dict, bus: Bus, send: Callable[[str, str], None], level: Callable[[], float] = lambda: 0.0,
                 clock: Callable[[], float] = time.time, rng: random.Random | None = None):
        c = cfg["cq"]
        st = cfg["station"]
        self.bus, self.send, self.level, self.clock = bus, send, level, clock
        self.rng = rng or random.Random()
        self.squelch = float(cfg["radio"]["squelch_threshold"])
        self.lo, self.hi = (float(x) for x in c["interval_seconds"])
        self.quiet_after = float(c["quiet_after_seconds"])
        fields = {"callsign": st["callsign"], "phonetic": speech.phonetic(st["callsign"], mark=True),
                  "name": st.get("name", ""), "qth": st.get("qth", "") or "the station",
                  "locator": (st.get("locator") or "").upper()}
        self.spoken = c["text"].format(**fields)
        self.display = c["display"].format(**fields).strip()
        self.busy_flags: set[str] = set()
        self.last_qso = -1e9
        self.next_at = self.clock() + float(c["first_after_seconds"])
        self.paused = False
        self._stop = threading.Event()
        bus.subscribe(self._on)

    # ---------- what's going on ----------
    def _on(self, name: str, d: dict) -> None:
        if d.get("cq"):
            return  # our own call
        if name == "over_started":
            self.busy_flags.add("rx")
        elif name in ("stt_done", "ignored", "over_finished"):
            self.busy_flags.discard("rx")
        flag = {"over_waiting": "waiting", "agent_started": "thinking", "tx_started": "talking", "sstv": "sstv"}.get(name)
        if flag:
            self.busy_flags.add(flag)
        done = {"over_finished": "waiting", "agent_done": "thinking", "tx_done": "talking", "sstv_done": "sstv"}.get(name)
        if done:
            self.busy_flags.discard(done)
        if name in QSO_EVENTS:
            self.last_qso = self.clock()

    def busy(self) -> bool:
        return bool(self.busy_flags) or self.level() > self.squelch  # someone's on frequency

    def _schedule(self, at: float) -> None:
        if abs(at - self.next_at) > 0.5:
            self.next_at = at
            self.bus.emit("cq_next", at=at)

    # ---------- the decision, once a tick ----------
    def tick(self) -> bool:
        """Call CQ if it's time and the frequency is ours. Returns True if it called."""
        now = self.clock()
        in_qso = self.busy() or now - self.last_qso <= self.quiet_after
        if in_qso:
            if not self.paused:
                self.paused = True
                self.bus.emit("cq_paused")
            # resume a little after the QSO has gone quiet, not a whole interval later
            self._schedule(max(self.next_at, self.last_qso + self.quiet_after + self.rng.uniform(2, 8)))
            return False
        self.paused = False
        if now < self.next_at:
            return False
        log.info(f"📣 calling CQ: {self.display}")
        try:
            self.send(self.spoken, self.display)
        except Exception as e:
            log.exception("calling CQ failed")
            self.bus.emit("error", summary=f"CQ call failed: {e}")
        self._schedule(self.clock() + self.rng.uniform(self.lo, self.hi))
        return True

    def start(self) -> None:
        self.bus.emit("cq_next", at=self.next_at)

        def loop():
            while not self._stop.wait(0.5):
                self.tick()

        threading.Thread(target=loop, daemon=True, name="cq").start()

    def stop(self) -> None:
        self._stop.set()


def allowed(cfg: dict) -> bool:
    """Automatic calls only go out over the air if you've explicitly said so (cq.on_air)."""
    c = cfg["cq"]
    if not c.get("enabled"):
        return False
    if cfg["radio"].get("reply", "radio") == "radio" and not c.get("on_air"):
        log.warning("CQ calling is on but reply is 'radio': not transmitting automatic CQs unless cq.on_air is true")
        return False
    return True
