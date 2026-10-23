"""Tiny web page for what doesn't speak well: code, diffs, paths. Open it on your phone.

Also serves /demo: a WSJT-X style screen (waterfall + band activity) with the operator
character, for showing the station off. It polls /api for new waterfall rows and events.
"""
from __future__ import annotations

import base64
import html
import json
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

from .config import dial_mhz
from .events import Bus


class Spectrum:
    """Waterfall rows (bytes 0–255) of what the station hears and says, 0–3 kHz like WSJT-X's Wide Graph."""

    N = 1024           # samples per row: 64 ms at 16 kHz
    BINS = 200
    FMAX = 3000.0
    FLOOR_DB, TOP_DB = -30.0, 40.0

    def __init__(self, keep: int = 400):
        self.rows: deque = deque(maxlen=keep)  # (seq, source, bytes)
        self.seq = 0
        self.level = 0.0
        self.tx_until = 0.0
        self._buf = np.zeros(0, np.float32)
        self._win: dict[int, np.ndarray] = {}
        self.lock = threading.Lock()

    def row(self, x: np.ndarray, sr: int) -> bytes:
        n = len(x)
        if n not in self._win:
            self._win[n] = np.hanning(n).astype(np.float32)
        mag = np.abs(np.fft.rfft(x * self._win[n]))
        freqs = np.fft.rfftfreq(n, 1 / sr)
        vals = np.interp(np.linspace(0, self.FMAX, self.BINS), freqs, mag)
        db = 20 * np.log10(vals + 1e-9)
        scaled = (db - self.FLOOR_DB) / (self.TOP_DB - self.FLOOR_DB) * 255
        return np.clip(scaled, 0, 255).astype(np.uint8).tobytes()

    def _push(self, source: str, x: np.ndarray, sr: int) -> None:
        r = self.row(x, sr)
        with self.lock:
            self.seq += 1
            self.rows.append((self.seq, source, r))

    def feed(self, block: np.ndarray, sr: int) -> None:
        """Receive audio, a few ms at a time, from the sound card callback."""
        self.level = float(np.sqrt(np.mean(block * block))) if len(block) else 0.0
        if time.time() < self.tx_until:
            return  # we're talking: show our own signal, not the line-in hiss
        self._buf = np.concatenate([self._buf, block.astype(np.float32)])
        while len(self._buf) >= self.N:
            self._push("rx", self._buf[:self.N], sr)
            self._buf = self._buf[self.N:]

    def play(self, audio: np.ndarray, sr: int) -> None:
        """Show transmitted audio on the waterfall in real time, alongside it being played."""
        audio = np.asarray(audio, np.float32)
        self.tx_until = time.time() + len(audio) / sr + 0.3

        def run():
            step = self.N
            t0 = time.time()
            for i in range(0, len(audio) - step + 1, step):
                self._push("tx", audio[i:i + step], sr)
                wait = t0 + (i + step) / sr - time.time()
                if wait > 0:
                    time.sleep(wait)

        threading.Thread(target=run, daemon=True).start()

    def since(self, seq: int) -> list[dict]:
        with self.lock:
            return [{"s": s, "src": src, "r": base64.b64encode(r).decode()} for s, src, r in self.rows if s > seq]


def over_stats(audio: np.ndarray, sr: int, threshold: float) -> dict:
    """WSJT-X style numbers for one over: dB above squelch, length, and its strongest audio frequency."""
    if audio is None or not len(audio):
        return {}
    rms = float(np.sqrt(np.mean(audio * audio)))
    mag = np.abs(np.fft.rfft(audio))
    freqs = np.fft.rfftfreq(len(audio), 1 / sr)
    band = (freqs > 200) & (freqs < 3000)
    peak = float(freqs[band][np.argmax(mag[band])]) if band.any() else 0.0
    return {"db": round(20 * np.log10(max(rms, 1e-6) / max(threshold, 1e-6))),
            "dur": round(len(audio) / sr, 1), "freq": int(round(peak))}


class Log:
    """The phone page's history, plus the demo screen's feed – built from the station's events."""

    def __init__(self, limit: int = 100, bus: Bus | None = None):
        self.entries: list[dict] = []
        self.events: list[dict] = []
        self.limit = limit
        self.lock = threading.Lock()
        self.seq = 0
        self.thinking = self.talking = False
        self.sstv_until = 0.0
        self.last_latency: float | None = None
        self.cq_next: float | None = None  # when the station will next call CQ (None = not calling)
        self.cq_paused = False
        self.state_text = ""
        self.spectrum = Spectrum()
        self.images: dict[int, bytes] = {}  # SSTV pictures sent, for the demo screen to paint
        self.bus = bus or Bus()
        self.bus.subscribe(self._on)

    def add_image(self, png: bytes) -> int:
        with self.lock:
            image_id = max(self.images, default=0) + 1
            self.images[image_id] = png
            for old in sorted(self.images)[:-5]:
                del self.images[old]
        return image_id

    @property
    def state(self) -> str:
        if self.talking:
            return "tx"
        if time.time() < self.sstv_until:
            return "sstv"
        return "thinking" if self.thinking else "idle"

    def _on(self, name: str, d: dict) -> None:
        stats = {k: d[k] for k in ("db", "dur", "freq") if k in d}
        if name == "stt_done":
            self.event("rx", d["text"], **stats)
        elif name == "ignored" and d.get("reason") == "noise":
            self.event("rx", d.get("text", ""), ignored=True, **stats)
        elif name == "agent_started":
            self.thinking = True
        elif name == "agent_done":
            self.thinking = False
            if (d.get("stats") or {}).get("seconds") is not None:
                self.last_latency = d["stats"]["seconds"]
            if d.get("detail"):
                self.event("detail", d["detail"])
        elif name == "tx_started":
            self.talking, self.state_text = True, d.get("display") or d["text"]
            self.event("tx", d.get("display") or d["text"], ack=d.get("ack", False), cq=d.get("cq", False), **stats)
        elif name == "tx_done":
            self.talking, self.state_text = False, ""
        elif name == "sstv":
            self.sstv_until = time.time() + float(d["seconds"])
            self.event("sstv", f"[SSTV {d['mode']} · {d['width']}×{d['height']}]",
                       **{k: d[k] for k in ("id", "mode", "seconds", "header_s", "line_s", "width", "height")})
        elif name == "cq_next":
            self.cq_next, self.cq_paused = d["at"], False
        elif name == "cq_paused":
            self.cq_paused = True
        elif name == "sstv_close":
            self.event("sstv_close", "")
        elif name == "error":
            self.event("note", "⚠ " + d["summary"])

    def add(self, heard: str, said: str, detail: str = "", pending: str | None = None) -> None:
        with self.lock:
            self.entries.append({"t": time.strftime("%H:%M:%S"), "heard": heard, "said": said,
                                 "detail": detail, "pending": pending})
            self.entries = self.entries[-self.limit:]

    def event(self, kind: str, text: str, **extra) -> None:
        """Something for the demo screen: rx (operator's over), tx (our reply), detail, note."""
        with self.lock:
            self.seq += 1
            self.events.append({"seq": self.seq, "ts": time.time(), "utc": time.strftime("%H%M%S", time.gmtime()),
                                "kind": kind, "text": text, **extra})
            self.events = self.events[-self.limit * 2:]

    def snapshot(self, since: int = 0, rows_since: int = 0) -> dict:
        with self.lock:
            events = [e for e in self.events if e["seq"] > since]
        return {"state": self.state, "state_text": self.state_text, "level": self.spectrum.level,
                "latency": self.last_latency, "cq_in": None if self.cq_next is None else self.cq_next - time.time(),
                "cq_paused": self.cq_paused,
                "events": events, "rows": self.spectrum.since(rows_since)}


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="4">
<title>radio-agent</title><style>
body{{font:15px system-ui;margin:0;padding:12px;background:#0d1117;color:#e6edf3}}
h1{{font-size:16px;margin:0 0 12px;color:#7ee787}}
.e{{border:1px solid #30363d;border-radius:8px;padding:10px;margin-bottom:10px;background:#161b22}}
.t{{color:#8b949e;font-size:12px}}.h{{color:#79c0ff}}.s{{color:#e6edf3;margin-top:4px}}
.p{{color:#f0883e;margin-top:6px}}pre{{white-space:pre-wrap;word-break:break-word;background:#0d1117;
padding:8px;border-radius:6px;font-size:12px;margin:8px 0 0}}</style></head><body>
<h1>📻 radio-agent · {callsign}</h1>{body}</body></html>"""


def render(log: Log, callsign: str) -> str:
    parts = []
    with log.lock:
        entries = list(reversed(log.entries))
    for e in entries:
        p = f'<div class="p">⏳ awaiting confirm: {html.escape(e["pending"])}</div>' if e["pending"] else ""
        d = f"<pre>{html.escape(e['detail'])}</pre>" if e["detail"] else ""
        parts.append(f'<div class="e"><div class="t">{e["t"]}</div><div class="h">🎙 {html.escape(e["heard"])}</div>'
                     f'<div class="s">🔊 {html.escape(e["said"])}</div>{p}{d}</div>')
    return PAGE.format(callsign=html.escape(callsign), body="".join(parts) or "<p>Nothing heard yet.</p>")


def demo_page(cfg: dict) -> str:
    st = cfg["station"]
    settings = {"token": str(cfg["sidecar"]["token"]), "callsign": st["callsign"],
                "operator": st.get("operator") or st["callsign"],
                "title": cfg["sidecar"].get("title") or "RADIO-AGENT",
                "name": st.get("name", "Agent"), "qth": st.get("qth", ""), "locator": st.get("locator", ""),
                "frequency": dial_mhz(cfg), "squelch": float(cfg["radio"]["squelch_threshold"]),
                "qso_gap": float(cfg["listen"]["conversation_window_seconds"]),
                "bins": Spectrum.BINS, "fmax": Spectrum.FMAX}
    page = (Path(__file__).parent / "demo.html").read_text(encoding="utf-8")
    return page.replace("/*SETTINGS*/{}", json.dumps(settings))


def serve(log: Log, cfg: dict) -> ThreadingHTTPServer:
    sc = cfg["sidecar"]
    token, callsign = str(sc["token"]), cfg["station"]["callsign"]

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if q.get("t", [""])[0] != token:
                self.send_response(403)
                self.end_headers()
                return
            if url.path == "/api":
                body = json.dumps(log.snapshot(int(q.get("since", ["0"])[0]), int(q.get("rows", ["0"])[0]))).encode()
                kind = "application/json"
            elif url.path == "/sstv":
                with log.lock:
                    png = log.images.get(int(q.get("id", ["0"])[0] or 0))
                if png is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body, kind = png, "image/png"
            elif url.path == "/demo":
                body, kind = demo_page(cfg).encode(), "text/html; charset=utf-8"
            else:
                body, kind = render(log, callsign).encode(), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    class Server(ThreadingHTTPServer):
        # On Windows SO_REUSEADDR lets a second program share the port silently; fail loudly instead
        allow_reuse_address = os.name != "nt"

    srv = Server((sc["host"], int(sc["port"])), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
