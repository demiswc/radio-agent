"""Turning radio talk into commands, and agent output into radio talk."""
from __future__ import annotations

import re
from dataclasses import dataclass

NATO = {
    "A": "Alpha", "B": "Bravo", "C": "Charlie", "D": "Delta", "E": "Echo", "F": "Foxtrot",
    "G": "Golf", "H": "Hotel", "I": "India", "J": "Juliet", "K": "Kilo", "L": "Lima",
    "M": "Mike", "N": "November", "O": "Oscar", "P": "Papa", "Q": "Quebec", "R": "Romeo",
    "S": "Sierra", "T": "Tango", "U": "Uniform", "V": "Victor", "W": "Whiskey", "X": "X-ray",
    "Y": "Yankee", "Z": "Zulu", "0": "Zero", "1": "One", "2": "Two", "3": "Three", "4": "Four",
    "5": "Five", "6": "Six", "7": "Seven", "8": "Eight", "9": "Nine", "/": "stroke",
}


def phonetic(callsign: str) -> str:
    return " ".join(NATO.get(ch, ch) for ch in callsign.upper())


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


# Bare "out" isn't accepted: "figure it out" or "check it out" would end a long instruction early
_OVER_RE = re.compile(r"[\s,.]*\bover(\s+and\s+out)?\b[\s.!?]*$", re.I)


def ends_with_over(text: str) -> bool:
    return bool(_OVER_RE.search(text.strip()))


def strip_over(text: str) -> str:
    return _OVER_RE.sub("", text.strip()).strip()


def has_wake_word(text: str, wake_words: list[str]) -> bool:
    t = f" {norm(text)} "
    return any(f" {w.lower()} " in t for w in wake_words)


def strip_wake_word(text: str, wake_words: list[str]) -> str:
    for w in wake_words:
        text = re.sub(rf"^\s*(hey\s+|ok\s+|okay\s+)?{re.escape(w)}\b[\s,.:]*", "", text, flags=re.I)
    return text.strip()


# Spoken control words. Matched on the whole (normalised) over, so
# "confirm" works but "can you confirm the tests pass" goes to the agent.
COMMANDS = {
    "confirm": ["confirm", "confirmed", "go ahead", "affirmative", "do it", "yes do it", "yes", "yeah do it", "roger go ahead"],
    "cancel": ["cancel", "negative", "abort", "no", "scrap that", "stop", "belay that"],
    "repeat": ["repeat", "say again", "say again please", "repeat last", "what was that"],
    "reset": ["new session", "reset", "start again", "fresh start"],
    "status": ["status", "status report", "what are you doing"],
    "close": ["close", "clear", "close it", "clear it", "close the picture", "clear the picture", "close sstv",
              "clear the screen", "close the window", "close the image", "clear the image"],
}


def command_of(text: str, wake_words: list[str]) -> str | None:
    t = norm(strip_wake_word(strip_over(text), wake_words))
    for cmd, phrases in COMMANDS.items():
        if t in phrases:
            return cmd
    return None


_LOOSE = {
    "confirm": ["confirm", "confirmed", "go ahead", "affirmative", "do it", "go for it", "yes please", "proceed", "crack on"],
    "cancel": ["cancel", "negative", "abort", "don't", "do not", "scrap", "belay"],
}


def loose_command(text: str, wake_words: list[str], max_words: int = 8) -> str | None:
    """Used only while a change is awaiting confirmation: a short over containing a
    confirm/cancel phrase counts ("Confirm, go ahead", "Yeah you can go ahead")."""
    t = norm(strip_wake_word(strip_over(text), wake_words))
    if not t or len(t.split()) > max_words:
        return None
    padded = f" {t} "
    hits = [c for c, phrases in _LOOSE.items() if any(f" {norm(ph)} " in padded for ph in phrases)]
    return hits[0] if len(hits) == 1 else None


DTMF_COMMANDS = {"1": "confirm", "9": "cancel", "0": "repeat", "5": "status"}

# "SSTV" comes out of Whisper as "SSTV", "S.S.T.V.", "s s t v", "SSTB"…
_SSTV = re.compile(r"\b(s\.?\s?s\.?\s?t\.?\s?[vb]\b\.?|slow[\s-]?scan)", re.I)
_PICTURE = re.compile(r"\b(send|show|give|beam|transmit|share)\b.{0,40}\b(picture|image|photo|photograph|pic|snapshot|view)s?\b", re.I)


_GOODBYE = re.compile(
    r"\b((?<!line )(?<!port )(?<!issue )(?<!number )(?<!version )(?<!page )(?<!step )(?<!#)73s?|seventy[\s-]?three|seven three|good\s?bye|bye(\s?bye)?|cheerio|signing off|sign off|i'?m clear|"
    r"going clear|thanks for the (qso|contact|chat)|(nice|good|great) (talking|chatting|speaking) (to|with) you|"
    r"catch you later|until next time|speak (to you )?soon)\b", re.I)


def sounds_like_goodbye(text: str) -> bool:
    """The operator is wrapping up: "73", "thanks for the contact", "cheerio"…"""
    return bool(_GOODBYE.search(text or ""))


def wants_sstv(text: str) -> bool:
    """'Claude, SSTV me a picture' / 'send me a slow scan of your location'."""
    return bool(_SSTV.search(text) or _PICTURE.search(text))


@dataclass
class Reply:
    say: str                 # spoken over the air
    detail: str = ""         # sent to the sidecar page
    confirm: str | None = None  # action awaiting the operator's OK
    sstv: str | None = None     # "SSTV: picture" – send the station's picture by slow-scan TV
    signoff: bool = False       # "SIGNOFF: yes" – the QSO is ending, sign off with the callsign


_SECTION = re.compile(r"^\s*(SAY|DETAIL|CONFIRM|SSTV|SIGNOFF)\s*:\s*", re.I | re.M)


def parse_agent_output(text: str) -> Reply:
    """Split agent output into SAY / DETAIL / CONFIRM sections.

    Falls back gracefully when the model ignores the format: first couple of
    sentences become SAY, everything goes to DETAIL.
    """
    parts: dict[str, str] = {}
    matches = list(_SECTION.finditer(text))
    if matches:
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            key = m.group(1).upper()
            parts[key] = (parts.get(key, "") + "\n" + text[m.end() : end].strip()).strip()
    say = parts.get("SAY") or fallback_say(text)
    detail = parts.get("DETAIL", "" if matches else text)
    confirm = parts.get("CONFIRM") or None
    sstv = (parts.get("SSTV") or "").strip().lower() or None
    signoff = (parts.get("SIGNOFF") or "").strip().lower().rstrip(".") in ("yes", "y", "true", "1")
    return Reply(say=speakable(say), detail=detail.strip(), confirm=confirm, sstv=sstv, signoff=signoff)


def fallback_say(text: str) -> str:
    text = re.sub(r"```.*?```", " (code sent to your screen) ", text, flags=re.S)
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(sentences[:2])


def speakable(text: str) -> str:
    """Make text safe to read aloud: no markdown, URLs or code."""
    text = re.sub(r"```.*?```", " (code on your screen) ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"https?://\S+", "a link on your screen", text)
    text = re.sub(r"[*_#>|]+", "", text)
    text = text.replace("->", " to ").replace("&", " and ")
    return re.sub(r"\s+", " ", text).strip()


def chunk_sentences(text: str, max_chars: int) -> list[str]:
    """Split speech into overs no longer than max_chars (roughly max_tx_seconds)."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks: list[str] = []
    cur = ""
    for s in sentences:
        if cur and len(cur) + len(s) + 1 > max_chars:
            chunks.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        chunks.append(cur)
    return chunks or [""]
