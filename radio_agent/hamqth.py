"""Callsign lookups on HamQTH.com (free: make an account at hamqth.com, then put HAMQTH_USER and
HAMQTH_PASS in .env). The station checks the other station's call so it can confirm their name and QTH.
"""
from __future__ import annotations

import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable

from .events import log

URL = "https://www.hamqth.com/xml.php"
NS = "{https://www.hamqth.com}"
FIELDS = {"callsign": "callsign", "nick": "name", "adr_name": "full_name", "qth": "qth", "country": "country",
          "grid": "grid", "itu": "itu", "cq": "cq_zone"}


def _fetch(params: dict, timeout: float = 10) -> bytes:
    with urllib.request.urlopen(f"{URL}?{urllib.parse.urlencode(params)}", timeout=timeout) as r:
        return r.read()


class HamQTH:
    def __init__(self, user: str | None = None, password: str | None = None,
                 fetch: Callable[[dict], bytes] = _fetch, clock: Callable[[], float] = time.time):
        self.user = user if user is not None else os.environ.get("HAMQTH_USER", "")
        self.password = password if password is not None else os.environ.get("HAMQTH_PASS", "")
        self.fetch, self.clock = fetch, clock
        self.session: str | None = None
        self.session_at = 0.0
        self.cache: dict[str, dict | None] = {}
        self._warned = False

    @property
    def enabled(self) -> bool:
        return bool(self.user and self.password)

    def _xml(self, params: dict) -> ET.Element:
        return ET.fromstring(self.fetch(params))

    def _login(self) -> str:
        root = self._xml({"u": self.user, "p": self.password})
        err = root.findtext(f"{NS}session/{NS}error")
        if err:
            raise RuntimeError(f"HamQTH login: {err}")
        self.session, self.session_at = root.findtext(f"{NS}session/{NS}session_id"), self.clock()
        return self.session

    def lookup(self, callsign: str) -> dict | None:
        """{"callsign", "name", "qth", "country", "grid", …} for a call, or None (not found, no account,
        no internet). Answers are cached for the session."""
        call = (callsign or "").upper().strip()
        if not call or not self.enabled:
            if call and not self._warned:
                log.info("HamQTH lookups off: put HAMQTH_USER and HAMQTH_PASS in .env")
                self._warned = True
            return None
        if call in self.cache:
            return self.cache[call]
        try:
            result = self._search(call)
        except Exception as e:
            log.warning(f"HamQTH lookup of {call} failed: {e}")
            return None  # not cached: try again next time
        self.cache[call] = result
        return result

    def _search(self, call: str, retry: bool = True) -> dict | None:
        if not self.session or self.clock() - self.session_at > 55 * 60:  # sessions last an hour
            self._login()
        root = self._xml({"id": self.session, "callsign": call, "prg": "radio-agent"})
        err = root.findtext(f"{NS}session/{NS}error")
        if err:
            if "not found" in err.lower():
                return None
            if retry and "session" in err.lower():  # expired early: log in again once
                self.session = None
                return self._search(call, retry=False)
            raise RuntimeError(err)
        found = root.find(f"{NS}search")
        if found is None:
            return None
        out = {key: (found.findtext(f"{NS}{tag}") or "").strip() for tag, key in FIELDS.items()}
        return {k: v for k, v in out.items() if v}


def describe(info: dict | None, callsign: str) -> str:
    """One line for the agent: what HamQTH says about a call."""
    if not info:
        return f"{callsign} isn't on HamQTH (or the lookup is off), so take their details as given."
    bits = [f"name {info['name']}" if info.get("name") else "",
            f"QTH {info['qth']}" if info.get("qth") else "",
            info.get("country", ""), f"locator {info['grid']}" if info.get("grid") else ""]
    return f"HamQTH lookup for {callsign}: " + ", ".join(b for b in bits if b) + "."
