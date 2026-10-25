"""The station's logbook: one ADIF record per contact (logs/<callsign>.adi), the format loggers, LoTW,
QRZ and HamQTH all import."""
from __future__ import annotations

import os
import time

from .config import dial_mhz

HEADER = "radio-agent logbook\n<ADIF_VER:5>3.1.4 <PROGRAMID:11>radio-agent <EOH>\n"


def band(mhz: float) -> str:
    for lo, hi, name in ((1.8, 2, "160m"), (3.5, 4, "80m"), (7, 7.3, "40m"), (14, 14.35, "20m"), (21, 21.45, "15m"),
                         (28, 29.7, "10m"), (50, 54, "6m"), (70, 71, "4m"), (144, 148, "2m"), (430, 440, "70cm")):
        if lo <= mhz <= hi:
            return name
    return ""


def record(fields: dict) -> str:
    """One ADIF record: <CALL:6>2M0DDX <NAME:5>Demis … <EOR>"""
    return " ".join(f"<{k.upper()}:{len(str(v))}>{v}" for k, v in fields.items() if v not in (None, "")) + " <EOR>\n"


class Logbook:
    def __init__(self, cfg: dict, clock=time.time):
        st, lb = cfg["station"], cfg.get("logbook", {})
        self.enabled = bool(lb.get("enabled", True))
        self.path = os.path.expanduser(str(lb.get("file") or "logs/{callsign}.adi").format(
            callsign=(st["callsign"] or "station").upper().replace("/", "-")))
        self.cfg, self.clock = cfg, clock
        self._last: tuple[str, str, dict] | None = None  # (call, record text, fields) of the contact in progress

    def fields(self, call: str, info: dict) -> dict:
        st = self.cfg["station"]
        t = time.gmtime(self.clock())
        mhz = dial_mhz(self.cfg)
        dmr = self.cfg["radio"]["mode"] == "dmr"
        return {"call": call.upper(), "qso_date": time.strftime("%Y%m%d", t), "time_on": time.strftime("%H%M", t),
                "band": band(float(mhz)), "freq": mhz, "mode": "DIGITALVOICE" if dmr else "FM",
                "submode": "DMR" if dmr else "", "rst_sent": info.get("rst_sent", ""), "rst_rcvd": info.get("rst_rcvd", ""),
                "name": info.get("name", ""), "qth": info.get("qth", ""), "gridsquare": info.get("grid", ""),
                "comment": info.get("comment", ""), "station_callsign": st["callsign"].upper(),
                "my_gridsquare": st.get("locator", "")}

    def write(self, call: str, info: dict, update: bool = False) -> dict:
        """Log a contact. update=True replaces this contact's record (more details came in) instead of
        adding another."""
        fields = self.fields(call, info)
        if update and self._last and self._last[0] == call:
            fields.update(qso_date=self._last[2]["qso_date"], time_on=self._last[2]["time_on"])  # when it started
        if not self.enabled:
            return fields
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        rec = record(fields)
        if update and self._last and self._last[0] == call and os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                text = f.read()
            if self._last[1] in text:
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write(text.replace(self._last[1], rec))
                self._last = (call, rec, fields)
                return fields
        new = not os.path.exists(self.path)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write((HEADER if new else "") + rec)
        self._last = (call, rec, fields)
        return fields
