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


# Around phonetics the station generates, so the voice uses its NATO pronunciations there and only
# there ("Hotel" in an ordinary sentence is just a hotel). Private-use characters: never in real text.
PHON_OPEN, PHON_CLOSE = "", ""


def phonetic(callsign: str, mark: bool = False) -> str:
    """ "N0CALL" -> "November Zero Charlie Alpha Lima Lima". mark=True wraps it for the voice (see unmark)."""
    words = " ".join(NATO.get(ch, ch) for ch in callsign.upper())
    return f"{PHON_OPEN}{words}{PHON_CLOSE}" if mark else words


def unmark(text: str) -> str:
    """Text for the screens and logs: without the phonetic marks."""
    return text.replace(PHON_OPEN, "").replace(PHON_CLOSE, "")


_DIGITS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six",
           "7": "seven", "8": "eight", "9": "nine", "/": "stroke"}


def spell(callsign: str) -> str:
    """A callsign said plainly, letter by letter: "N0CALL" -> "N zero C A L L"."""
    return " ".join(_DIGITS.get(ch, ch) for ch in callsign.upper())


# A callsign as written: N0CALL, G4ABC, M0XYZ/P
CALLSIGN = r"(?:[A-Z]{1,2}|\d[A-Z]{1,2}|[A-Z]\d)\d{1,2}[A-Z]{1,4}(?:/[A-Z0-9]{1,4})?"
_CALL_RE = re.compile(CALLSIGN)
_SPOKEN = {**{v.lower().replace("-", ""): k for k, v in NATO.items() if k != "/"}, "alfa": "A", "juliett": "J",
           "niner": "9", **{v: k for k, v in _DIGITS.items() if k != "/"}}


def find_callsign(text: str, exclude: tuple[str, ...] = ()) -> str | None:
    """The first callsign in an over that isn't one of ours, written ("N1CALL", "N 1 C A L L") or
    phonetic ("November One Charlie Alpha Lima Lima"). None if there isn't one."""
    skip = {c.upper() for c in exclude if c}
    return next((c for c in callsigns_in(text) if c.upper() not in skip), None)


def callsigns_in(text: str) -> list[str]:
    """Every callsign in an over, written or phonetic, in order."""
    run: list[str] = []
    found: list[str] = []

    def flush():
        found.extend(m.group(0) for m in _CALL_RE.finditer("".join(run)))
        run.clear()

    for tok in re.findall(r"[A-Za-z0-9/]+(?:-[A-Za-z]+)?", text or ""):
        low = tok.lower().replace("-", "")
        if low in _SPOKEN:
            run.append(_SPOKEN[low])
        elif tok == "/" or (len(tok) == 1 and (tok.isdigit() or tok.isupper()) and tok != "I"):
            run.append(tok)
        else:
            flush()
            if tok.isupper() and _CALL_RE.fullmatch(tok):
                found.append(tok)
    flush()
    return [c.upper() for c in found]


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
    # no bare "clear": at the end of an over that means signing off (see sounds_like_goodbye)
    "close": ["close", "close it", "clear it", "close the picture", "clear the picture", "close sstv",
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
    r"catch you later|until next time|speak (to you )?soon|qrt)\b", re.I)
# "clear" ending the over: "clear", "Thanks Stephen, clear", "N1CALL clear" (not "is that clear?")
_CLEAR = re.compile(r"(^|[,.!;]\s*|\b\w*\d\w*\s+|\b(and|now|so)\s+)clear[\s.!]*$", re.I)


def sounds_like_goodbye(text: str) -> bool:
    """The operator is wrapping up: "73", "seventy-three", "QRT", "thanks for the contact", "…, clear"…"""
    text = strip_over(text or "")
    return bool(_GOODBYE.search(text) or _CLEAR.search(text))


_PROWORD = r"(?:this\s+is|de)"


def strip_framing(text: str, calls: tuple[str, ...] = ()) -> str:
    """Take out callsigns and procedure words the agent added itself: the station does all the framing.

    Callsigns (written, phonetic or spelled) at the start or end, and a trailing "over", "clear",
    "73" or "this is …"."""
    known = []
    for c in calls:
        if c:
            known += [re.escape(c), r"\s+".join(map(re.escape, phonetic(c).split())),
                      r"\s+".join(map(re.escape, spell(c).split()))]
    one = "(?:" + CALLSIGN + ("|(?i:" + "|".join(known) + ")" if known else "") + ")"
    run = rf"{one}(?:\s*(?:,|(?i:\b{_PROWORD}\b|\band\b))?\s*{one})*"
    lead = re.compile(rf"^\s*(?:(?i:{_PROWORD})\s+)?{run}\b(?:\s+(?i:here))?\s*[,.:;!-]*\s*")
    tail = re.compile(rf"(?:[\s,.;:-]*(?:(?i:{_PROWORD}|over\s+to)\s+)?\b{run})+(?:[\s,]+(?i:clear|out))?[\s,.!]*$")
    clear = re.compile(r"(?:^|[,.;:-]\s*)(?i:clear|out)[\s.!]*$")  # ", clear." – not "the coast is clear"
    seventy3 = re.compile(r"(?:^|[,.!;]\s*)(?i:73s?|seventy[\s-]three)[\s.!]*$")
    original = text = (text or "").strip()
    while True:
        before = text
        text = strip_over(text)
        for rx in (clear, seventy3, tail):
            text = rx.sub("", text).rstrip(" ,;:-")
        text = lead.sub("", text)
        if text == before:
            break
    if text and text != original and text[-1] not in ".!?":
        text += "."
    return text


def wants_sstv(text: str) -> bool:
    """'Claude, SSTV me a picture' / 'send me a slow scan of your location' / 'weather fax for Biscay'."""
    from .wefax import asks_for_wefax
    return bool(_SSTV.search(text) or _PICTURE.search(text) or asks_for_wefax(text))


@dataclass
class Reply:
    say: str                 # spoken over the air
    detail: str = ""         # sent to the sidecar page
    confirm: str | None = None  # action awaiting the operator's OK
    sstv: str | None = None     # "SSTV: picture" – send the station's picture by slow-scan TV
    signoff: bool = False       # "SIGNOFF: yes" – the QSO is ending, sign off with the callsign
    log: dict | None = None     # "LOG: name=Demis; qth=Crieff; rst_sent=59; rst_rcvd=57" – for the logbook


_SECTION = re.compile(r"^\s*(SAY|DETAIL|CONFIRM|SSTV|SIGNOFF|LOG)\s*:\s*", re.I | re.M)


def parse_log(text: str) -> dict | None:
    """ "name=Demis; qth=Crieff; rst_sent=59" -> {"name": "Demis", "qth": "Crieff", "rst_sent": "59"}."""
    fields = {}
    for part in re.split(r"[;\n]", text or ""):
        if "=" in part:
            k, v = part.split("=", 1)
            k = re.sub(r"[^a-z_]", "", k.strip().lower())
            if k and v.strip():
                fields[k] = v.strip()
    return fields or None


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
    return Reply(say=speakable(say), detail=detail.strip(), confirm=confirm, sstv=sstv, signoff=signoff,
                 log=parse_log(parts.get("LOG", "")))


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
