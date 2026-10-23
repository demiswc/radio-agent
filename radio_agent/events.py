"""One event source, two views: the terminal UI and the WSJT-X style web page both subscribe here.

Events, in the order one exchange produces them:
  over_started   squelch opened, someone is talking
  stt_done       one transmission transcribed            text, rx_s, stt_s, db, freq, dur
  over_waiting   more is expected before "over"          text, timeout
  over_finished  a whole over (maybe stitched) is ready  text, rx_s, stt_s, ended_by
  agent_started  the agent is working on it              request
  tx_started     we're speaking (ack=True: "stand by")   text, ack, db, freq, dur
  tx_done
  agent_done     the reply is decided                    say, detail, pending, stats
  ignored        noise / no wake word, no reply          text, reason
  error          something broke, the station carries on summary
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Callable

log = logging.getLogger("radio_agent")


class Bus:
    def __init__(self):
        self.subs: list[Callable[[str, dict], None]] = []
        self.lock = threading.Lock()

    def subscribe(self, fn: Callable[[str, dict], None]) -> None:
        with self.lock:
            self.subs.append(fn)

    def emit(self, name: str, **data) -> None:
        data.setdefault("t", time.time())
        log.debug("event %s %s", name, {k: v for k, v in data.items() if k != "t"})
        with self.lock:
            subs = list(self.subs)
        for fn in subs:
            try:
                fn(name, data)
            except Exception:  # one broken view mustn't take the station off air
                log.exception("event handler failed for %s", name)


def setup_logging(path: str = "logs/radio-agent.log", plain: bool = False) -> None:
    """Everything goes to the log file with timestamps; --plain also prints the bare lines to the screen."""
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.propagate = False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s"))
    log.addHandler(fh)
    if plain:
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.INFO)
        sh.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(sh)
