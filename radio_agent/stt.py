"""Speech to text."""
from __future__ import annotations

import io
import json
import os
import urllib.request
import uuid
import wave

import numpy as np

from .events import log


def to_wav_bytes(audio: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def _add_nvidia_dlls() -> None:
    """Windows: make cuBLAS/cuDNN from the nvidia-* pip packages (or a ./cuda folder) loadable."""
    if os.name != "nt":
        return
    import glob
    import site
    roots = site.getsitepackages() + [site.getusersitepackages()]
    roots += glob.glob(os.path.join(os.environ.get("APPDATA", ""), "Python", "Python3*", "site-packages"))
    dirs = [d for r in roots for d in glob.glob(os.path.join(r, "nvidia", "*", "bin"))]
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cuda")
    if os.path.isdir(here):
        dirs.append(here)
    for d in dirs:
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        try:
            os.add_dll_directory(d)
        except (OSError, AttributeError):
            pass


class FasterWhisper:
    def __init__(self, cfg: dict):
        device = cfg["stt"].get("device", "cpu")
        if device == "cuda":
            _add_nvidia_dlls()
        from faster_whisper import WhisperModel

        # "cpu" by default: "cuda" needs cuBLAS (CUDA 12) + cuDNN 9 – see README / pip install nvidia-cublas-cu12
        compute = cfg["stt"].get("compute_type") or ("float16" if device == "cuda" else "int8")
        self.model = WhisperModel(cfg["stt"]["model"], device=device, compute_type=compute)
        log.info(f"Whisper {cfg['stt']['model']} on {device} ({compute})")
        self.hint = cfg["stt"].get("hint") or None
        # Whisper's own voice filter mistakes FM radio audio for noise and drops whole phrases.
        # The squelch gate already trims silence, so it's off unless you turn it on.
        self.vad = bool(cfg["stt"].get("vad_filter", False))

    def transcribe(self, audio: np.ndarray, sr: int, context: str = "") -> str:
        from .radio import resample

        audio = resample(audio, sr, 16000)
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        if 0 < peak < 0.5:
            audio = audio * (0.9 / peak)  # quiet acoustic pickup – normalise before Whisper
        prompt = " ".join(x for x in (self.hint, context) if x)[-800:] or None  # Whisper reads ~224 tokens
        segs, _ = self.model.transcribe(audio, language="en", initial_prompt=prompt, vad_filter=self.vad, beam_size=5)
        return " ".join(s.text.strip() for s in segs).strip()


class OpenAIWhisper:
    """Cloud STT – the sensible choice on a Pi Zero."""

    def __init__(self, cfg: dict):
        self.key = os.environ["OPENAI_API_KEY"]
        self.model = cfg["stt"]["openai_model"]
        self.hint = cfg["stt"].get("hint") or ""

    def transcribe(self, audio: np.ndarray, sr: int, context: str = "") -> str:
        boundary = uuid.uuid4().hex
        fields = {"model": self.model, "language": "en", "prompt": f"{self.hint} {context}".strip()[-800:]}
        body = b""
        for k, v in fields.items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"over.wav\"\r\n"
                 "Content-Type: audio/wav\r\n\r\n").encode() + to_wav_bytes(audio, sr) + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/transcriptions", data=body,
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())["text"].strip()


class NoSTT:
    def transcribe(self, audio: np.ndarray, sr: int, context: str = "") -> str:
        return ""


def make(cfg: dict):
    if cfg["radio"]["mode"] == "text":
        return NoSTT()
    b = cfg["stt"]["backend"]
    if b == "faster-whisper":
        return FasterWhisper(cfg)
    if b == "openai":
        return OpenAIWhisper(cfg)
    return NoSTT()
