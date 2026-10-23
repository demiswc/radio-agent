"""RTL-SDR receiver: an SDR dongle listens for your handheld instead of a base radio.

No K1 cable (so no accidental transmit), no microphone, no room noise – rtl_fm hands us
demodulated FM audio. Receive only: pair it with radio.reply: screen or speaker.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import queue

import numpy as np

from . import dtmf
from .events import log
from .radio import Heard, collect_over

SR = 16000
BLOCK = SR // 50  # 20 ms


class RtlFmRadio:
    sample_rate = SR

    def __init__(self, cfg: dict):
        self.r = cfg["radio"]
        c = cfg["rtlsdr"]
        exe = shutil.which(c["rtl_fm"]) or c["rtl_fm"]
        cmd = [exe, "-d", str(c["device_index"]), "-f", str(c["frequency"]), "-M", "fm",
               "-s", str(SR), "-E", "deemp", "-p", str(c["ppm"])]
        if str(c["gain"]) != "auto":
            cmd += ["-g", str(c["gain"])]
        if int(c["squelch"]) > 0:
            # rtl_fm squelch; pad keeps a steady stream of zeros while it's closed
            cmd += ["-l", str(c["squelch"]), "-E", "pad"]
        cmd += ["-"]
        self.cmd = cmd
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.q: queue.Queue = queue.Queue(maxsize=500)
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        need = BLOCK * 2
        buf = b""
        while self.alive:
            chunk = self.proc.stdout.read(4096)
            if not chunk:
                self.q.put(None)
                return
            buf += chunk
            while len(buf) >= need:
                block, buf = buf[:need], buf[need:]
                a = np.frombuffer(block, dtype="<i2").astype(np.float32) / 32768
                try:
                    self.q.put_nowait(a)
                except queue.Full:  # we were busy thinking – drop old audio
                    try:
                        self.q.get_nowait()
                    except queue.Empty:
                        pass
                    self.q.put_nowait(a)

    def _drain(self) -> None:
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break

    def levels(self, seconds: float = 10.0):
        self._drain()
        for _ in range(int(seconds * 50)):
            a = self.q.get()
            if a is None:
                return
            yield float(np.sqrt(np.mean(a * a)))

    def listen(self, timeout: float | None = None) -> Heard | None:
        if timeout is None:
            self._drain()  # fresh over: ignore anything heard while the agent was busy
        info: dict = {}
        audio = collect_over(self.q.get, {**self.r, "sample_rate": SR}, timeout, info,
                             on_open=getattr(self, "on_open", None))
        if self.proc.poll() is not None and not len(audio):
            raise RuntimeError(f"rtl_fm stopped – is the dongle plugged in? ({' '.join(self.cmd)})")
        return Heard(audio=audio, dtmf=dtmf.decode(audio, SR), sample_rate=SR, meta=info)

    def transmit_text(self, text: str) -> None:
        log.info(f"🖥  {text}")

    def transmit_audio(self, audio, sr) -> None:
        raise RuntimeError("RTL-SDR is receive-only – set radio.reply to screen or speaker")

    def close(self) -> None:
        self.alive = False
        self.proc.terminate()
