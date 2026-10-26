"""Text to speech. Returns (float32 audio, sample_rate) or None for text mode."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import wave
from pathlib import Path

import numpy as np

from . import speech


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


# Lexicon output for DECtalk marks phoneme strings with these (private-use characters, never in a reply)
PH_OPEN, PH_CLOSE = "", ""


def dectalk_clean(text: str) -> str:
    """Keep DECtalk from reading the reply as commands: [ ] { } become ( ), control characters go.

    Then the lexicon's phoneme marks become real [phonemes]."""
    text = text.translate(str.maketrans("[]{}", "()()"))
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    return text.replace(PH_OPEN, "[").replace(PH_CLOSE, "]")


class DECtalk:
    """DECtalk (github.com/dectalk/dectalk): the classic 1980s synthesiser voices ("paul" = Perfect Paul).

    tts.dectalk_path is the `say` executable, or a folder holding `say` or (Windows vs2022 builds,
    which ship no `say`) DECtalk.dll. Blank = `say` on PATH, then ./dectalk, then ~/dectalk.
    """

    phonemes = True  # the lexicon may hand it [phoneme] strings
    VOICES = {"paul": "np", "harry": "nh", "frank": "nf", "dennis": "nd", "betty": "nb", "ursula": "nu",
              "wendy": "nw", "rita": "nr", "kit": "nk"}
    SAY = "say.exe" if os.name == "nt" else "say"

    def __init__(self, cfg: dict):
        t = cfg["tts"]
        voice = str(t.get("dectalk_voice") or "paul").lower()
        self.prefix = f"[:phoneme on] [:{self.VOICES.get(voice, voice)}] [:rate {int(t.get('dectalk_rate') or 180)}] "
        self.say, dll = self.find(t.get("dectalk_path") or "")
        if not self.say:
            self._start_dll(dll)

    @classmethod
    def find(cls, configured: str) -> tuple[str | None, str | None]:
        """(say executable, None) or (None, folder with DECtalk.dll). FileNotFoundError if neither."""
        if configured:
            p = Path(os.path.expanduser(configured))
            if p.is_file():
                return str(p), None
            folders = [p]
        else:
            # macOS has its own, different `say`, so only trust PATH elsewhere
            on_path = shutil.which("say") if sys.platform != "darwin" else None
            if on_path:
                return on_path, None
            folders = [Path("dectalk"), Path("~/dectalk").expanduser()]
        for d in folders:
            if (d / cls.SAY).is_file():
                return str((d / cls.SAY).resolve()), None
            if os.name == "nt" and (d / "DECtalk.dll").is_file():
                return None, str(d.resolve())
        where = configured or "PATH, ./dectalk or ~/dectalk"
        raise FileNotFoundError(f"DECtalk not found in {where} (no {cls.SAY} or DECtalk.dll)")

    def _start_dll(self, folder: str):
        import ctypes
        self.dir, self.ct = folder, ctypes
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
        text = self.prefix + dectalk_clean(text)
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            if self.say:
                # Windows say: -w file, then the words. Linux say: -fo file -a "text"
                args = ["-w", path, text] if os.name == "nt" else ["-fo", path, "-a", text]
                # run from its own folder so it finds dtalk_us.dic
                subprocess.run([self.say, *args], cwd=os.path.dirname(self.say) or None,
                               check=True, capture_output=True, timeout=120)
            else:
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


# How to say the NATO phonetic alphabet (callsign IDs, spelled words): (respelling, DECtalk).
# SAPI, Piper and the rest get the respelling. DECtalk gets its own column: [phonemes], or plain text
# where that's heard better. Each DECtalk entry was picked by `radio-agent say --check-pronunciation
# --compare` (DECtalk Paul at 160/180/200 wpm -> Whisper small.en): e.g. [d'ehltax] came out "Delga",
# "Dell-tah" is heard as Delta every time, and [m'ayk]
# became "mic" inside a callsign where plain "Mike" didn't. Quebec and Juliet aren't reliably heard in any form yet.
# A bad phoneme code makes DECtalk say "command error", so test any change with the check.
PHONETICS = {
    "Alpha": ("al-fah", "['aalfaa]"), "Bravo": ("brah-voh", "[br'aavow]"), "Charlie": ("char-lee", "[ch'arliy]"),
    "Delta": ("dell-tah", "Dell-tah"), "Echo": ("eck-oh", "['ehkow]"), "Foxtrot": ("foks-trot", "Fox trot"),
    "Golf": ("golf", "Golf"), "Hotel": ("hoh-tell", "[hxowt'ehl]"), "India": ("in-dee-ah", "['ihndiyax]"),
    "Juliet": ("joo-lee-ett", "Joo-lee-ett"), "Kilo": ("kee-loh", "[k'iy low]"), "Lima": ("lee-mah", "[l'iymax]"),
    "Mike": ("mike", "Mike"), "November": ("no-vem-ber", "[nowv'ehmbrr]"), "Oscar": ("oss-kah", "Oscar"),
    "Papa": ("pah-pah", "Papa"), "Quebec": ("keh-beck", "[kehb'ehk]"), "Romeo": ("roh-mee-oh", "[r'owmiy`ow]"),
    "Sierra": ("see-air-ah", "[siy'erax]"), "Tango": ("tang-go", "[t'aenxgow]"), "Uniform": ("you-nee-form", "['yuniyform]"),
    "Victor": ("vik-tah", "[v'ihktax]"), "Whiskey": ("wiss-kee", "[w'ihskiy]"), "X-ray": ("ecks-ray", "['ehksr`ey]"),
    "Yankee": ("yang-kee", "[yx'aenxkiy]"), "Zulu": ("zoo-loo", "[z'uwluw]"),
    "Zero": ("zeer-oh", "[z'irow]"), "Two": ("too", "[t'uw]"), "Seven": ("sev-en", "[s'ehvaxn]"), "Nine": ("nine", "[n'ayn]"),
}

# Said before synthesis, whole words, any case. tts.pronunciations in config adds to or overrides these
PRONUNCIATIONS = {
    "WAV": "wave", "SSTV": "S S T V", "QSO": "Q S O", "QRM": "Q R M", "UTC": "U T C",
    "MHz": "megahertz", "kHz": "kilohertz", "dB": "D B", "CTCSS": "C T C S S", "73": "seventy three",
    "Rockall": "Rock all", "Crieff": "Creef", "Perthshire": "Perth-sure", "README": "read me",
    "npm": "N P M", "div": "div",
}


class _Words:
    """One table of whole-word, case-insensitive replacements."""

    def __init__(self, words: dict, phonemes: bool):
        self.words = {k: _for_dectalk(dt) if phonemes and dt else say for k, (say, dt) in words.items()}
        keys = sorted(words, key=len, reverse=True)
        # not inside a word, and not part of a number like 145.73
        self.rx = re.compile(r"(?<!\w)(?<!\d[.,:])(?:" + "|".join(map(re.escape, keys)) + r")(?!\w)(?![.,:]\d)",
                             re.I) if keys else None

    def __call__(self, text: str) -> str:
        return self.rx.sub(lambda m: self.words[m.group(0).lower()], text) if self.rx else text


def _for_dectalk(entry: str) -> str:
    """A DECtalk column entry: "[phonemes]" gets marked to pass through dectalk_clean, text stays text."""
    entry = entry.strip()
    if entry.startswith("[") and entry.endswith("]"):
        return f"{PH_OPEN}{entry[1:-1]}{PH_CLOSE}"
    return entry


_MARKED = re.compile(f"{speech.PHON_OPEN}(.*?){speech.PHON_CLOSE}", re.S)


class Lexicon:
    """Whole-word, case-insensitive replacements. "WAV" -> "wave", but "wave" is left alone.

    PRONUNCIATIONS apply everywhere. PHONETICS only apply inside the phonetics the station generates
    (speech.phonetic(..., mark=True): IDs, CQ calls), so "Hotel" in an ordinary sentence stays a hotel.
    phonemes=True (DECtalk): words use the DECtalk column, "[phonemes]" or text. A config entry is a
    respelling for every voice ("Delta": "dell-tuh"), or both: {"say": "dell-tuh", "dectalk": "[d'ehltaa]"}
    ("phonemes": "d'ehltaa" works too). An entry for a phonetic word changes the phonetics table.
    """

    def __init__(self, extra: dict | None = None, phonemes: bool = False):
        nato = {k.lower(): v for k, v in PHONETICS.items()}
        general = {k.lower(): (v, None) for k, v in PRONUNCIATIONS.items()}
        for k, v in (extra or {}).items():
            key = str(k).lower()
            table = nato if key in nato else general
            if v is None or v == "":
                table.pop(key, None)  # `73: null` in config drops a default
            elif isinstance(v, dict):
                dt = v.get("dectalk") or (f"[{v['phonemes']}]" if v.get("phonemes") else None)
                table[key] = (str(v.get("say") or k), dt)
            else:
                table[key] = (str(v), None)
        self.nato, self.general = _Words(nato, phonemes), _Words(general, phonemes)

    def __call__(self, text: str) -> str:
        parts = _MARKED.split(text)  # odd parts: inside the station's phonetic marks
        out = "".join(self.nato(p) if i % 2 else self.general(p) for i, p in enumerate(parts))
        return speech.unmark(out)


class Pronounced:
    """Run the lexicon over the text just before any backend speaks it."""

    def __init__(self, inner, extra: dict | None = None):
        self.inner, self.lexicon = inner, Lexicon(extra, phonemes=getattr(inner, "phonemes", False))

    def synth(self, text: str):
        return self.inner.synth(self.lexicon(text))


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


BACKENDS = {"piper": Piper, "espeak": Espeak, "openai": OpenAITTS, "sapi": WindowsSAPI, "dectalk": DECtalk}


def make(cfg: dict):
    return Cached(Pronounced(_make(cfg), cfg["tts"].get("pronunciations")))


def _make(cfg: dict):
    if cfg["radio"]["mode"] == "text":
        return PrintTTS()
    backend = cfg["tts"]["backend"]
    try:
        return BACKENDS.get(backend, lambda c: PrintTTS())(cfg)
    except FileNotFoundError as e:
        fallback = cfg["tts"].get("fallback")
        if not fallback or fallback == backend:
            raise
        print(f"WARNING: {backend} voice unavailable ({e}); using tts.fallback: {fallback}", file=sys.stderr)
        return BACKENDS.get(fallback, lambda c: PrintTTS())(cfg)
