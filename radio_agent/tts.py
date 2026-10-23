"""Text to speech. Returns (float32 audio, sample_rate) or None for text mode."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import urllib.request
import wave
from pathlib import Path

import numpy as np


def _wav_to_np(data: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(data)) as w:
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
        ch = w.getnchannels()
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


class Piper:
    def __init__(self, cfg: dict):
        self.model = cfg["tts"]["piper_model"]
        self.exe = shutil.which("piper") or "piper"
        meta = Path(self.model + ".json")
        self.sr = json.loads(meta.read_text())["audio"]["sample_rate"] if meta.exists() else 22050

    def synth(self, text: str):
        out = subprocess.run([self.exe, "--model", self.model, "--output_raw"], input=text.encode(),
                             capture_output=True, check=True).stdout
        return np.frombuffer(out, dtype=np.int16).astype(np.float32) / 32768, self.sr


class Espeak:
    def __init__(self, cfg: dict):
        self.exe = shutil.which("espeak-ng") or shutil.which("espeak") or "espeak-ng"

    def synth(self, text: str):
        out = subprocess.run([self.exe, "-v", "en-gb", "-s", "160", "--stdout", text],
                             capture_output=True, check=True).stdout
        return _wav_to_np(out)


class OpenAITTS:
    def __init__(self, cfg: dict):
        self.key = os.environ["OPENAI_API_KEY"]
        self.voice = cfg["tts"]["openai_voice"]

    def synth(self, text: str):
        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/speech",
            data=json.dumps({"model": "tts-1", "voice": self.voice, "input": text, "response_format": "wav"}).encode(),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return _wav_to_np(r.read())


class WindowsSAPI:
    """Windows' built-in voices (System.Speech) – nothing to install."""

    PS = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "if ($env:RA_VOICE) { try { $s.SelectVoice($env:RA_VOICE) } catch {} }"
        "else { $gb = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'en-GB' } | Select-Object -First 1;"
        "       if ($gb) { $s.SelectVoice($gb.VoiceInfo.Name) } }"
        "$s.Rate = [int]$env:RA_RATE;"
        "$s.SetOutputToWaveFile($env:RA_OUT); $s.Speak($env:RA_TEXT); $s.Dispose()"
    )

    def __init__(self, cfg: dict):
        self.voice = cfg["tts"].get("sapi_voice") or ""
        self.rate = int(cfg["tts"].get("sapi_rate", 1))

    def synth(self, text: str):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            env = {**os.environ, "RA_TEXT": text, "RA_OUT": path, "RA_VOICE": self.voice, "RA_RATE": str(self.rate)}
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", self.PS],
                           env=env, check=True, capture_output=True)
            with open(path, "rb") as f:
                return _wav_to_np(f.read())
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


class DECtalk:
    """DECtalk (github.com/dectalk/dectalk), Windows: the classic 1980s synthesiser voices ("paul" = Perfect Paul).

    Unzip the release's AMD64 folder to tts.dectalk_path. The DLL stays loaded, so each phrase is quick.
    """

    VOICES = {"paul": "np", "harry": "nh", "frank": "nf", "dennis": "nd", "betty": "nb", "ursula": "nu",
              "wendy": "nw", "rita": "nr", "kit": "nk"}

    def __init__(self, cfg: dict):
        import ctypes
        t = cfg["tts"]
        self.dir = os.path.expanduser(t.get("dectalk_path", "~/dectalk"))
        voice = str(t.get("dectalk_voice", "paul")).lower()
        self.prefix = f"[:{self.VOICES.get(voice, voice)}] [:rate {int(t.get('dectalk_rate', 180))}] "
        self.ct = ctypes
        os.add_dll_directory(self.dir)
        cwd = os.getcwd()
        os.chdir(self.dir)  # it looks for dtalk_us.dic next to itself
        try:
            self.dll = ctypes.WinDLL(os.path.join(self.dir, "DECtalk.dll"))
            self.h = ctypes.c_void_p()
            # WAVE_MAPPER, DO_NOT_USE_AUDIO_DEVICE: render to files only
            if self.dll.TextToSpeechStartup(None, ctypes.byref(self.h), ctypes.c_uint(0xFFFFFFFF),
                                            ctypes.c_uint(0x80000000)):
                raise RuntimeError(f"DECtalk failed to start from {self.dir}")
        finally:
            os.chdir(cwd)

    def synth(self, text: str):
        import tempfile
        # [ ] are DECtalk commands, so keep them out of what the agent says
        text = self.prefix + text.replace("[", "(").replace("]", ")")
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            c = self.ct
            self.dll.TextToSpeechOpenWaveOutFile(self.h, path.encode("mbcs"), c.c_uint(0x4))  # 11 kHz mono 16-bit
            self.dll.TextToSpeechSpeak(self.h, text.encode("cp1252", "replace"), c.c_uint(1))  # TTS_FORCE
            self.dll.TextToSpeechSync(self.h)
            self.dll.TextToSpeechCloseWaveOutFile(self.h)
            with open(path, "rb") as f:
                return _wav_to_np(f.read())
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


class PrintTTS:
    def synth(self, text: str):
        return None


class Cached:
    """Remember short repeated phrases so acknowledgements play instantly."""

    def __init__(self, inner):
        self.inner, self.cache = inner, {}

    def synth(self, text: str):
        if text in self.cache:
            return self.cache[text]
        out = self.inner.synth(text)
        if len(text) < 60:
            self.cache[text] = out
        return out


def make(cfg: dict):
    return Cached(_make(cfg))


def _make(cfg: dict):
    if cfg["radio"]["mode"] == "text":
        return PrintTTS()
    return {"piper": Piper, "espeak": Espeak, "openai": OpenAITTS, "sapi": WindowsSAPI, "dectalk": DECtalk}.get(cfg["tts"]["backend"], lambda c: PrintTTS())(cfg)
