"""QSO procedure: the station puts the callsigns and procedure words round every over; the agent never does.

  first over of a QSO   "<them phonetic>, this is <me phonetic>. <message> Over to <them>, <me>, over."
  quick back-and-forth  "<them>. <message> <me>, over."
  every id_interval     the full form again, to stay inside the licence's ID rules
  signing off           "<message> Seventy-three, <them phonetic>, this is <me phonetic>, clear."

The phrases are templates in the qso section of the config (a profile can set its own style). They can
use {me} {them} (said plainly: "two M zero D D X") and {me_phonetic} {them_phonetic}. A part in
[square brackets] is left out when a callsign in it isn't known, e.g. "[Over to {them}, ]{me}, over."
"""
from __future__ import annotations

import re
import string
import time
from dataclasses import dataclass
from typing import Callable

from . import speech


@dataclass
class Over:
    spoken: str     # what the voice says (phonetics marked, see speech.phonetic)
    to: str         # the WSJT-X style row on the demo screen: "2M0DDX MM7DDX <msg>"
    de: str
    msg: str


def fill(template: str, fields: dict) -> str:
    """Fill a template; drop any [optional part] whose fields are empty."""
    def part(m: re.Match) -> str:
        names = [f for _, f, _, _ in string.Formatter().parse(m.group(1)) if f]
        return m.group(1).format(**fields) if all(fields.get(n) for n in names) else ""
    return re.sub(r"\[([^\]]*)\]", part, template).format(**fields)


class QSO:
    def __init__(self, cfg: dict, clock: Callable[[], float] = time.time):
        st, q = cfg["station"], cfg["qso"]
        self.cfg, self.q, self.clock = cfg, q, clock
        self.me = (st["callsign"] or "").upper()
        self.default_them = (st.get("operator") or "").upper()
        self.new_after = float(q["new_after_minutes"]) * 60
        self.id_every = float(st.get("id_interval_seconds", 600))
        self.every_over = bool(st.get("id_every_over"))
        self.last_cq = -1e9  # when we last called CQ: a reply soon after is someone answering it
        self.end()

    def end(self) -> None:
        """The QSO is over (signed off, or we're calling CQ): the next reply opens a new one."""
        self.them: str | None = None
        self.last_exchange = -1e9
        self.last_full_id = -1e9

    def active(self) -> bool:
        return self.clock() - self.last_exchange <= self.new_after

    def _fields(self) -> dict:
        them = self.them or self.default_them
        return {"me": speech.spell(self.me), "me_phonetic": speech.phonetic(self.me, mark=True),
                "them": speech.spell(them) if them else "",
                "them_phonetic": speech.phonetic(them, mark=True) if them else ""}

    def frame(self, chunks: list[str], closing: bool = False, heard: str = "", caller: str = "") -> list[Over]:
        """Put the callsigns round a reply (already split into overs). The last one hands over (or signs
        off); earlier ones end "<me>, continuing". heard/caller: the over we're answering, which names
        the other station when a new QSO starts (else station.operator)."""
        now = self.clock()
        new = not self.active()
        if new:
            self.them = (caller or speech.find_callsign(heard, (self.me,)) or "").upper() or None
        full = new or self.every_over or now - self.last_full_id >= self.id_every
        f = self._fields()
        q = self.q
        out = []
        for i, chunk in enumerate(chunks):
            first, last = i == 0, i == len(chunks) - 1
            chunk = chunk.strip()
            if chunk and chunk[-1] not in ".!?":
                chunk += "."  # so the handover doesn't run on from the message
            head = "" if closing or not first else fill(q["opening"] if full else q["short_opening"], f)
            if not last:
                tail = fill(q["continuing"], f)
            elif closing:
                tail = fill(q["closing"], f)
            else:
                tail = fill(q["handover"] if full else q["short_handover"], f)
            spoken = re.sub(r"\s+", " ", f"{head} {chunk} {tail}").strip()
            # a capital to start, even when an [optional part] was dropped ("this is …" -> "This is …")
            spoken = re.sub(rf"^([{speech.PHON_OPEN}]?)(\w)", lambda m: m.group(1) + m.group(2).upper(), spoken)
            msg = chunk.strip() + (" 73" if closing and last else "")
            out.append(Over(spoken, (self.them or self.default_them), self.me, msg.strip()))
        if closing:
            self.end()
        else:
            self.last_exchange = now
            if full:
                self.last_full_id = now
        return out
