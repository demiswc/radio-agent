"""The radio layer: hear an over, transmit a reply.

Two implementations share one interface:
  TextRadio      – keyboard in, terminal out. Tests the whole loop with no hardware.
  SoundcardRadio – USB sound card (AIOC/Digirig/CM108) + PTT (serial DTR/RTS or VOX).
"""
from __future__ import annotations

import queue
import time
from dataclasses import dataclass, field

import numpy as np

from . import dtmf
from .events import log


@dataclass
class Heard:
    audio: np.ndarray | None = None   # float32 mono at sample_rate
    text: str | None = None           # TextRadio gives text directly
    dtmf: str = ""
    sample_rate: int = 16000
    meta: dict = field(default_factory=dict)


class TextRadio:
    """Type what you'd say on the radio. Prefix with 'dtmf ' to send keypad digits, e.g. 'dtmf *1234#'."""

    sample_rate = 16000

    def listen(self, timeout: float | None = None) -> Heard | None:
        try:
            line = input("\n📻 you> ").strip()
        except EOFError:
            return None
        if not line:
            return Heard(text="")
        if line.lower().startswith("dtmf "):
            return Heard(dtmf=line[5:].strip())
        return Heard(text=line)

    def transmit_text(self, text: str) -> None:
        print(f"📡 agent> {text}")

    def transmit_audio(self, audio: np.ndarray, sr: int) -> None:  # pragma: no cover - text mode never synthesises
        print(f"📡 [{len(audio) / sr:.1f}s of audio]")

    def close(self) -> None:
        pass


class _SerialPTT:
    def __init__(self, port: str, assert_dtr: bool, assert_rts: bool):
        import serial  # pyserial

        # Serial(port) opens straight away with DTR and RTS asserted, which keys the radio for a
        # moment. Set the lines to "off" first: pyserial applies them as the port opens.
        self.ser = serial.Serial()
        self.ser.port = port
        self.on_dtr, self.on_rts = assert_dtr, assert_rts
        self.off()
        self.ser.open()

    def on(self) -> None:
        self.ser.dtr, self.ser.rts = self.on_dtr, self.on_rts

    def off(self) -> None:
        self.ser.dtr, self.ser.rts = (not self.on_dtr), (not self.on_rts)

    def close(self) -> None:
        self.off()
        self.ser.close()


class _NoPTT:
    def on(self) -> None: ...
    def off(self) -> None: ...
    def close(self) -> None: ...


class SoundcardRadio:
    def __init__(self, cfg: dict):
        import sounddevice as sd

        self.sd = sd
        r = cfg["radio"]
        self.r = r
        self.sample_rate = int(r["sample_rate"])
        self.block = int(self.sample_rate * 0.02)  # 20 ms
        p = r["ptt"]
        self.ptt_type = p["type"]
        self.ptt = _SerialPTT(p["port"], p["assert_dtr"], p["assert_rts"]) if p["type"] == "serial" else _NoPTT()
        self.stream = None
        self.q: queue.Queue = queue.Queue(maxsize=50 * 120)  # up to 2 minutes of 20 ms blocks
        self.monitor = None  # optional fn(block, sample_rate): the demo screen's waterfall
        self.on_open = None  # optional fn(): squelch opened, an over is starting

    # ---------- receive ----------
    def levels(self, seconds: float = 10.0):
        """Yield RMS levels for calibration."""
        with self.sd.InputStream(samplerate=self.sample_rate, channels=1, device=self.r["input_device"],
                                 blocksize=self.block, dtype="float32") as s:
            end = time.time() + seconds
            while time.time() < end:
                data, _ = s.read(self.block)
                yield float(np.sqrt(np.mean(data * data)))

    def _capture(self, indata, frames, t, status) -> None:
        block = indata[:, 0].copy()
        if self.monitor is not None:
            try:
                self.monitor(block, self.sample_rate)
            except Exception:
                pass  # a display problem must never stop us hearing
        try:
            self.q.put_nowait(block)
        except queue.Full:
            pass

    def listen(self, timeout: float | None = None) -> Heard | None:
        """Block until an over is heard. Squelch-gated: Baofeng audio is silent when squelch is closed.

        The input stream stays open, so nothing is lost while the last piece of a long over is being
        transcribed. A fresh over (timeout=None) skips audio that arrived while we were busy replying;
        a continuation (timeout set) keeps it, since that's you carrying on talking.
        """
        if self.stream is None:
            self.stream = self.sd.InputStream(samplerate=self.sample_rate, channels=1, device=self.r["input_device"],
                                              blocksize=self.block, dtype="float32", callback=self._capture)
            self.stream.start()
        if timeout is None:
            _drain(self.q)
        info: dict = {}
        audio = collect_over(self.q.get, self.r, timeout, info, on_open=self.on_open)
        return Heard(audio=audio, dtmf=dtmf.decode(audio, self.sample_rate), sample_rate=self.sample_rate, meta=info)

    # ---------- transmit ----------
    def transmit_text(self, text: str) -> None:
        raise RuntimeError("SoundcardRadio needs audio – configure a TTS backend")

    def transmit_audio(self, audio: np.ndarray, sr: int) -> None:
        if self.ptt_type == "none":
            log.info(f"[ptt none] would transmit {len(audio) / sr:.1f}s")
            return
        audio = resample(audio, sr, self.sample_rate)
        cap = int(self.r["max_tx_seconds"] * self.sample_rate)
        audio = tx_level(audio[:cap], float(self.r.get("tx_gain", 1.0)))
        if self.ptt_type == "vox":
            n = int(self.sample_rate * self.r["vox_lead_in_ms"] / 1000)
            t = np.arange(n) / self.sample_rate
            lead = (0.15 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
            audio = np.concatenate([lead, audio])
        p = self.r["ptt"]
        self.ptt.on()
        try:
            time.sleep(p["tx_delay_ms"] / 1000)
            self.sd.play(audio, self.sample_rate, device=self.r["output_device"], blocking=True)
            time.sleep(p["tx_tail_ms"] / 1000)
        finally:
            self.ptt.off()

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()
        self.ptt.close()


def _drain(q: queue.Queue) -> None:
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


def tone_share(block: np.ndarray, hz: float, sr: int) -> tuple[float, float]:
    """(fraction of the block's energy at `hz`, block level). A roger beep is ~1, speech is low."""
    d = block - block.mean()  # the squelch thump is a DC step – ignore it
    total = float((d * d).sum())
    if total <= 1e-12:
        return 0.0, 0.0
    k = np.exp(-2j * np.pi * hz * np.arange(len(d)) / sr)
    return abs(d @ k) ** 2 / (total * len(d) / 2), float(np.sqrt(total / len(d)))


def collect_over(read_block, r: dict, start_timeout: float | None = None, info: dict | None = None,
                 on_open=None) -> np.ndarray:
    """Squelch-gated recorder shared by sound-card and SDR radios.

    read_block() returns one 20 ms float32 block. Recording starts once audio stays above
    squelch_threshold for open_ms, and ends after hang_ms below it (or max_listen_seconds).
    If the handheld sends a roger beep (a pure tone at roger_beep_hz) when you let go, the over
    ends right there and info["roger_beep"] is set, so the station can answer without waiting.
    """
    thr = float(r["squelch_threshold"])
    beep_hz = r.get("roger_beep_hz") or 0
    sr = int(r.get("sample_rate", 16000))
    beep = 0
    open_blocks = max(1, int(r["open_ms"] / 20))
    hang_blocks = max(1, int(r["hang_ms"] / 20))
    max_blocks = int(r["max_listen_seconds"] * 50)
    pre: list[np.ndarray] = []
    rec: list[np.ndarray] = []
    loud = quiet = 0
    recording = False
    waited = 0
    max_wait = int(start_timeout * 50) if start_timeout else None
    while True:
        data = read_block()
        if data is None:
            break
        if not recording and max_wait is not None:
            waited += 1
            if waited > max_wait:
                return np.zeros(0, np.float32)  # nobody keyed up in time
        rms = float(np.sqrt(np.mean(data * data))) if len(data) else 0.0
        if beep_hz and len(data):
            share, level = tone_share(data, beep_hz, sr)
            beep = beep + 1 if share > 0.5 and level > thr / 4 else 0
            if beep >= 3:  # 60 ms of pure tone: that's PTT released
                if info is not None:
                    info["roger_beep"] = True
                keep = rec[: max(0, len(rec) - beep)] if recording else []
                return np.concatenate(keep) if keep else np.zeros(0, np.float32)
        if not recording:
            pre = (pre + [data])[-(open_blocks + 25):]  # ~0.5 s pre-roll so the first word isn't clipped
            loud = loud + 1 if rms > thr else 0
            if loud >= open_blocks:
                recording, rec, quiet = True, list(pre), 0
                if on_open is not None:
                    on_open()
            continue
        rec.append(data)
        quiet = quiet + 1 if rms <= thr else 0
        if quiet >= hang_blocks or len(rec) >= max_blocks:
            break
    # keep ~0.3 s after the last loud block: the soft end of "over" is often below the threshold
    return np.concatenate(rec[: len(rec) - quiet + 15]) if rec else np.zeros(0, np.float32)


def tx_level(audio: np.ndarray, gain: float, peak: float = 0.9) -> np.ndarray:
    """Set the transmit level: normalise to `peak`, then apply radio.tx_gain.

    Every voice and picture then goes out at the same level, and tx_gain alone sets how hard the
    radio's mic input is driven. Above 1, quiet parts get the full gain and peaks are rounded off,
    so it never clips.
    """
    top = float(np.abs(audio).max()) if len(audio) else 0.0
    y = audio * (peak / top) if top > 1e-6 else audio.copy()
    y = y * gain
    if gain <= 1.0:
        return y.astype(np.float32)
    knee = 0.6
    over = np.abs(y) > knee
    y[over] = np.sign(y[over]) * (knee + (1 - knee) * np.tanh((np.abs(y[over]) - knee) / (1 - knee)))
    return y.astype(np.float32)


def resample(audio: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """Simple linear resampler – fine for narrowband FM voice."""
    if sr_in == sr_out or len(audio) == 0:
        return audio.astype(np.float32)
    n_out = int(len(audio) * sr_out / sr_in)
    x_old = np.linspace(0, 1, len(audio), endpoint=False)
    x_new = np.linspace(0, 1, n_out, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def make(cfg: dict):
    mode = cfg["radio"]["mode"]
    if mode == "soundcard":
        return SoundcardRadio(cfg)
    if mode == "dmr":
        from .dmr import DMRRadio

        return DMRRadio(cfg)
    if mode == "rtlsdr":
        from .rtlsdr import RtlFmRadio

        return RtlFmRadio(cfg)
    return TextRadio()
