"""DTMF tone detection (Goertzel) so the handheld keypad can confirm/cancel/unlock."""
from __future__ import annotations

import numpy as np

LOW = [697, 770, 852, 941]
HIGH = [1209, 1336, 1477, 1633]
KEYS = [
    ["1", "2", "3", "A"],
    ["4", "5", "6", "B"],
    ["7", "8", "9", "C"],
    ["*", "0", "#", "D"],
]


def _goertzel_power(block: np.ndarray, freq: float, sr: int) -> float:
    """Power at a single frequency (vectorised single-bin DFT, same result as Goertzel)."""
    n = np.arange(len(block))
    z = np.dot(block, np.exp(-2j * np.pi * freq * n / sr))
    return float(z.real * z.real + z.imag * z.imag)


def _detect_block(block: np.ndarray, sr: int) -> str | None:
    energy = float(np.sum(block * block))
    if energy < 1e-3 * len(block) * 0.01:
        return None
    lows = [_goertzel_power(block, f, sr) for f in LOW]
    highs = [_goertzel_power(block, f, sr) for f in HIGH]
    li, hi = int(np.argmax(lows)), int(np.argmax(highs))
    lp, hp = lows[li], highs[hi]
    # Each dominant tone must clearly beat the others in its group
    if sorted(lows)[-2] * 4 > lp or sorted(highs)[-2] * 4 > hp:
        return None
    # ...and together hold most of the block's energy (rejects speech)
    n = len(block)
    tone_energy = 2 * (lp + hp) / n
    if tone_energy < 0.5 * energy:
        return None
    return KEYS[li][hi]


def decode(audio: np.ndarray, sr: int, block_ms: int = 40) -> str:
    """Return the DTMF digits found in `audio` (debounced: a key must persist 2 blocks)."""
    n = int(sr * block_ms / 1000)
    out: list[str] = []
    prev = None
    run = 0
    emitted = False
    for i in range(0, len(audio) - n + 1, n):
        key = _detect_block(audio[i : i + n].astype(np.float64), sr)
        if key == prev and key is not None:
            run += 1
            if run >= 1 and not emitted:
                out.append(key)
                emitted = True
        else:
            prev, run, emitted = key, 0, False
    return "".join(out)


def tone(key: str, sr: int, ms: int = 120, gap_ms: int = 80) -> np.ndarray:
    """Generate a DTMF key (used by tests and by `radio-agent selftest`)."""
    for r, row in enumerate(KEYS):
        if key in row:
            c = row.index(key)
            t = np.arange(int(sr * ms / 1000)) / sr
            sig = 0.4 * np.sin(2 * np.pi * LOW[r] * t) + 0.4 * np.sin(2 * np.pi * HIGH[c] * t)
            return np.concatenate([sig, np.zeros(int(sr * gap_ms / 1000))]).astype(np.float32)
    raise ValueError(key)
