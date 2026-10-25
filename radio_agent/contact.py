"""Running a contact: what kind of QSO this is, what the agent should do in it, and the logbook.

  answering our CQ   Stephen called CQ to make a contact, so he leads it: signal report, name, QTH,
                     weather, then asks for theirs, confirms them (checked on HamQTH) and logs it.
  their CQ           the operator called CQ and Stephen answered, so the operator leads.
  a call             someone just called the station (e.g. to ask it something): no contact script.

Each over the agent gets short "station notes" ahead of what was said, and its LOG line is written to
the ADIF logbook.
"""
from __future__ import annotations

import re
import time
from typing import Callable

from . import speech
from .events import Bus, log
from .hamqth import HamQTH, describe
from .logbook import Logbook
from .qso import QSO
from .weather import Weather

CONTACT, THEIR_CQ, CALL = "answering our CQ", "their CQ", "a call"
_THEIR_CQ = re.compile(r"\b(c\.?\s?q|seek you)\b", re.I)
_WEATHER = re.compile(r"\b(weather|wx|forecast|wind|sea state|waves|temperature|conditions|raining|rain)\b", re.I)


class Contacts:
    def __init__(self, cfg: dict, qso: QSO, bus: Bus | None = None, weather: Weather | None = None,
                 lookup: HamQTH | None = None, logbook: Logbook | None = None, clock: Callable[[], float] = time.time):
        st = cfg["station"]
        self.cfg, self.qso, self.bus, self.clock = cfg, qso, bus, clock
        self.weather = weather or Weather(cfg)
        self.lookup = lookup or HamQTH()
        self.logbook = logbook or Logbook(cfg)
        self.me = st["callsign"].upper()
        self.qth, self.locator = st.get("qth") or "", (st.get("locator") or "").upper()
        self.cq_window = float(cfg["qso"].get("cq_answer_minutes", 5)) * 60
        self.mode = CALL
        self.partner: str | None = None
        self.info: dict | None = None
        self.logged: dict | None = None

    # ---------- before the agent answers ----------
    def for_us(self, heard: str) -> bool:
        """Is this over meant for the station even without its wake word? Someone giving our callsign,
        answering our CQ, or calling CQ themselves (we answer it)."""
        if not heard:
            return False
        recent_cq = self.clock() - self.qso.last_cq <= self.cq_window and not self.qso.active()
        return self.me in speech.callsigns_in(heard) or recent_cq or bool(_THEIR_CQ.search(heard))

    def notes(self, heard: str, meta: dict | None = None) -> str:
        """Station notes for the agent about this over ("" when there's nothing to add)."""
        heard = heard or ""
        new = not self.qso.active()
        if new:
            found = (meta or {}).get("callsign") or speech.find_callsign(heard, (self.me,))
            self.partner = (found or self.cfg["station"].get("operator") or "").upper() or None
            self.info = self.lookup.lookup(self.partner) if self.partner else None
            self.logged = None
            if _THEIR_CQ.search(heard):
                self.mode = THEIR_CQ
            elif self.clock() - self.qso.last_cq <= self.cq_window:
                self.mode = CONTACT
            else:
                self.mode = CALL
        who = self.partner or "a station whose callsign you didn't catch (ask for it)"
        lines = []
        if self.mode == CONTACT and new:
            lines.append(f"{who} is answering your CQ call. This is a new contact and you lead it. Don't offer to "
                         f"help with anything: you called CQ to make a contact. Give them a signal report, your name, "
                         f"your QTH ({self.qth}, locator {self.locator}) and your weather, then ask for their name, "
                         f"QTH and a report on your signal.")
        elif self.mode == THEIR_CQ and new:
            lines.append(f"{who} is calling CQ and you're answering, so they're in control of this contact: follow "
                         f"their lead. Reply with a signal report, your name and QTH ({self.qth}), and let them run it.")
        elif self.mode in (CONTACT, THEIR_CQ):
            lead = "you lead it (they answered your CQ)" if self.mode == CONTACT else "they lead it (you answered their CQ)"
            lines.append(f"You're in a contact with {who}; {lead}.")
        if self.mode in (CONTACT, THEIR_CQ):
            if self.logged:
                lines.append("This contact is logged. Chat on, or wrap up when they do.")
            else:
                lines.append("When you have their name, QTH and the report they gave you, read them back to confirm "
                             "and add a line 'LOG: name=…; qth=…; rst_sent=…; rst_rcvd=…'.")
            if self.partner and (new or not self.logged):
                lines.append(describe(self.info, self.partner) +
                             (" If what they tell you doesn't match, politely check it." if self.info else ""))
        if (self.mode == CONTACT and new) or _WEATHER.search(heard):
            report = self.weather.report()
            lines.append(report or "Your weather feed is down, so describe the weather from what you can see.")
        if not lines:
            return ""
        return "[Station notes, not said on air: " + " ".join(lines) + "]"

    # ---------- after it answers ----------
    def log(self, fields: dict | None, closing: bool = False) -> dict | None:
        """The agent's LOG line (or the end of a contact that never got one): write the logbook."""
        fields = dict(fields or {})
        call = (fields.pop("call", "") or self.partner or "").upper()
        if not call:
            return None
        if not fields and (self.logged or self.mode == CALL or not closing):
            return None  # nothing new to log
        info = {**(self.logged or {}), **fields}
        if self.info:
            info.setdefault("grid", self.info.get("grid", ""))
            if self.info.get("name") and info.get("name") and \
                    self.info["name"].split()[0].lower() != info["name"].split()[0].lower():
                info["comment"] = f"HamQTH name: {self.info['name']}"
        if not fields:
            info["comment"] = (info.get("comment", "") + " details incomplete").strip()
        update = self.logged is not None
        written = self.logbook.write(call, info, update=update)
        self.logged = info
        log.info(f"📒 logged {call} {info.get('name', '')} {info.get('qth', '')} "
                 f"{info.get('rst_sent', '?')}/{info.get('rst_rcvd', '?')} -> {self.logbook.path}")
        if self.bus:
            # "op_name": the event bus takes "name" itself
            self.bus.emit("qso_logged", call=call, update=update, path=self.logbook.path, op_name=written.get("name", ""),
                          **{k: written.get(k, "") for k in ("qth", "rst_sent", "rst_rcvd", "gridsquare")})
        return written
