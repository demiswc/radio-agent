"""Does the voice say the phonetics so a listener gets them? Round-trip each one: TTS -> Whisper -> compare.

`radio-agent say --check-pronunciation` runs the NATO alphabet and the test callsigns through the station's
own voice (with tts.pronunciations applied) and its own Whisper model, with no hint prompt, and prints a
pass/fail table. --compare also tries each word plain, respelled and (DECtalk) as phonemes, to show
which to put in tts.pronunciations.
"""
from __future__ import annotations

import copy
import re

from . import speech, stt, tts

TEST_CALLS = ["N0CALL"]
# what Whisper writes for words that sound the same: "2 mic zero" is Two Mike Zero
_ALIASES = {"mic": "M", "alfa": "A", "juliett": "J", "whisky": "W", "xray": "X", "to": "2", "too": "2", "for": "4",
            "oh": "0", "nought": "0", "niner": "9", "won": "1", "ate": "8"}


def heard_chars(text: str) -> str:
    """What Whisper wrote, as callsign characters: "November 0, Charlie Alpha Lima Lima." -> "N0CALL"."""
    out = []
    for tok in re.findall(r"[a-z0-9]+", (text or "").lower().replace("x-ray", "xray").replace("x ray", "xray").replace("fox trot", "foxtrot")):
        if tok.isdigit():
            out.append(tok)
        elif tok in speech._SPOKEN:
            out.append(speech._SPOKEN[tok])
        elif tok in _ALIASES:
            out.append(_ALIASES[tok])
        else:
            out.append("?")
    return "".join(out)


def make_ears(cfg: dict):
    """The station's Whisper model, without the hint prompt (it would give the answers away)."""
    c = copy.deepcopy(cfg)
    c["stt"]["hint"] = ""
    try:
        return stt.FasterWhisper(c)
    except Exception:
        if c["stt"].get("device") == "cpu":
            raise
        c["stt"]["device"] = "cpu"  # no GPU libraries: a few short clips are fine on the CPU
        return stt.FasterWhisper(c)


def _backend(voice):
    while hasattr(voice, "inner"):
        voice = voice.inner
    return voice


def sent_text(cfg: dict, voice, text: str) -> str:
    """What the backend is actually handed for `text` (DECtalk sees its [phonemes])."""
    dectalk = getattr(_backend(voice), "phonemes", False)
    out = tts.Lexicon(cfg["tts"].get("pronunciations"), phonemes=dectalk)(text)
    return tts.dectalk_clean(out) if dectalk else out


def items() -> list[tuple[str, str]]:
    """(label, characters): the alphabet, the digits, then the test callsigns."""
    return [(speech.NATO[ch], ch) for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"] + [(c, c) for c in TEST_CALLS]


def check(cfg: dict, voice, ears, extra_calls: list[str] = ()) -> list[dict]:
    """One row per item: what was sent, what Whisper heard, pass/fail."""
    rows = []
    todo = items() + [(c.upper(), c.upper()) for c in extra_calls if c and c.upper() not in TEST_CALLS]
    for label, chars in todo:
        text = speech.phonetic(chars, mark=True)
        out = voice.synth(text)
        heard = ears.transcribe(*out) if out is not None else ""
        rows.append({"item": label, "sent": sent_text(cfg, voice, text), "heard": heard,
                     "ok": heard_chars(heard) == chars.upper()})
    return rows


def compare(cfg: dict, voice, ears) -> list[dict]:
    """For each phonetic word: is it heard right said plain, respelled, or (DECtalk) as phonemes?"""
    backend = _backend(voice)
    rows = []
    for word, (respell, ph) in tts.PHONETICS.items():
        chars = next(k for k, v in speech.NATO.items() if v == word)
        row = {"item": word}
        variants = {"plain": word, "respelling": respell}
        if getattr(backend, "phonemes", False) and ph:
            variants["phonemes"] = f"{tts.PH_OPEN}{ph}{tts.PH_CLOSE}"
        for name, text in variants.items():
            out = backend.synth(text)
            heard = ears.transcribe(*out) if out is not None else ""
            row[name] = (heard_chars(heard) == chars, heard)
        rows.append(row)
    return rows


def table(rows: list[dict]) -> str:
    width = max(len(r["item"]) for r in rows)
    lines = [f"  {'':4} {'':<{width}}  {'heard by Whisper':<34} sent to the voice"]
    for r in rows:
        lines.append(f"  {'PASS' if r['ok'] else 'FAIL':4} {r['item']:<{width}}  {r['heard'][:34]:<34} {r['sent']}")
    passed = sum(r["ok"] for r in rows)
    lines.append(f"\n  {passed}/{len(rows)} heard correctly")
    return "\n".join(lines)


def compare_table(rows: list[dict]) -> str:
    names = [n for n in ("plain", "respelling", "phonemes") if any(n in r for r in rows)]
    lines = ["  " + f"{'':<9}" + "".join(f" {n:<22}" for n in names)]
    for r in rows:
        cells = "".join(f" {('ok   ' if r[n][0] else 'MISS ') + repr(r[n][1])[:16]:<22}" if n in r else f" {'':<22}"
                        for n in names)
        lines.append(f"  {r['item']:<9}{cells}")
    return "\n".join(lines)
