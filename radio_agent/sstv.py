"""Slow-scan TV: send the operator a picture of the station's location.

encode(..., seconds=5) squeezes a mode into a few seconds: it looks and sounds like SSTV for a demo,
but it's too fast for a decoder. At full length "robot36" (36 s) and "martin1" (115 s) are the real
modes, with a standard VIS header: a phone app such as "Robot36" or MMSSTV/QSSTV decodes those.
The picture is a random photo from sstv.folder if there are any, otherwise a built-in one
(sstv.picture: a colour-bar test card, or the Rockall drawing used by the demo profile), with the
callsign stamped on it like any SSTV picture.
"""
from __future__ import annotations

import io
import os
import random
import time

import numpy as np

HEADER_S = 0.910  # leader 300 + break 10 + leader 300 + start, 7 bits, parity, stop at 30 ms each

# width, height, VIS code, seconds per line (after the VIS header)
MODES = {
    "robot36": {"name": "Robot 36", "size": (320, 240), "vis": 8, "line_s": 0.150},
    "martin1": {"name": "Martin M1", "size": (320, 256), "vis": 44, "line_s": 0.446446},
}


def duration(mode: str, seconds: float | None = None) -> float:
    """Real length of a mode, or `seconds` if it's being squeezed."""
    m = MODES[mode]
    return float(seconds) if seconds else HEADER_S + m["size"][1] * m["line_s"]


def line_seconds(mode: str, seconds: float | None = None) -> float:
    m = MODES[mode]
    return (float(seconds) - HEADER_S) / m["size"][1] if seconds else m["line_s"]


def _level(v: np.ndarray) -> np.ndarray:
    """Pixel value 0–255 → tone: black 1500 Hz, white 2300 Hz."""
    return 1500.0 + np.asarray(v, np.float64) * (800.0 / 255.0)


class _Tones:
    """Builds a phase-continuous FM tone sequence with sample-exact timing (no drift over 240 lines)."""

    def __init__(self, sr: int):
        self.sr, self.t, self.parts = sr, 0.0, []
        self.scale = 1.0  # time stretch applied to everything added after the header

    def add(self, freq, ms: float) -> None:
        start = round(self.t * self.sr)
        self.t += ms * self.scale / 1000.0
        n = round(self.t * self.sr) - start
        if n <= 0:
            return
        if np.isscalar(freq):
            self.parts.append(np.full(n, float(freq)))
        else:  # a scan line: spread the pixels evenly across the scan time
            freq = np.asarray(freq, np.float64)
            self.parts.append(freq[(np.arange(n) * len(freq)) // n])

    def audio(self, amp: float = 0.6) -> np.ndarray:
        f = np.concatenate(self.parts)
        return (amp * np.sin(2 * np.pi * np.cumsum(f) / self.sr)).astype(np.float32)


def _vis(tones: _Tones, code: int) -> None:
    tones.add(1900, 300)
    tones.add(1200, 10)
    tones.add(1900, 300)
    tones.add(1200, 30)                       # start bit
    ones = 0
    for i in range(7):                        # LSB first; 1 = 1100 Hz, 0 = 1300 Hz
        bit = (code >> i) & 1
        ones += bit
        tones.add(1100 if bit else 1300, 30)
    tones.add(1100 if ones % 2 else 1300, 30)  # even parity
    tones.add(1200, 30)                       # stop bit


def encode(rgb: np.ndarray, mode: str = "robot36", sr: int = 22050, seconds: float | None = None) -> np.ndarray:
    """rgb: (height, width, 3) uint8 at the mode's size. Returns float32 mono audio at sr.

    seconds squeezes (or stretches) the picture lines to that total length; None = the real mode.
    """
    m = MODES[mode]
    w, h = m["size"]
    assert rgb.shape[:2] == (h, w), f"{m['name']} needs a {w}x{h} picture"
    px = rgb.astype(np.float64)
    tones = _Tones(sr)
    _vis(tones, m["vis"])
    tones.scale = line_seconds(mode, seconds) / m["line_s"]  # 1 for the real thing
    if mode == "robot36":
        r, g, b = px[..., 0], px[..., 1], px[..., 2]
        y = 16 + 0.257 * r + 0.504 * g + 0.098 * b
        cr = 128 + 0.439 * r - 0.368 * g - 0.071 * b
        cb = 128 - 0.148 * r - 0.291 * g + 0.439 * b
        for line in range(h):
            pair = slice(line - line % 2, line - line % 2 + 2)  # chroma is shared by each pair of lines
            tones.add(1200, 9)
            tones.add(1500, 3)
            tones.add(_level(y[line]), 88)
            if line % 2 == 0:
                tones.add(1500, 4.5)
                tones.add(1900, 1.5)
                tones.add(_level(cr[pair].mean(axis=0)), 44)
            else:
                tones.add(2300, 4.5)
                tones.add(1900, 1.5)
                tones.add(_level(cb[pair].mean(axis=0)), 44)
    else:  # Martin M1: green, blue, red scans
        for line in range(h):
            tones.add(1200, 4.862)
            tones.add(1500, 0.572)
            for ch in (1, 2, 0):
                tones.add(_level(px[line, :, ch]), 146.432)
                tones.add(1500, 0.572)
    tones.scale = 1.0
    tones.add(1500, 50)
    return tones.audio()


WEFAX_HEADER_S = 1.0  # start tone + phasing, squeezed


def encode_wefax(gray: np.ndarray, sr: int = 22050, seconds: float = 8.0) -> np.ndarray:
    """A radiofax (IOC 576) style signal squeezed into `seconds`: 300 Hz start tone, phasing lines,
    the chart line by line (black 1500 Hz, white 2300 Hz), 450 Hz stop tone. For the waterfall:
    a real 120 lpm fax takes ten minutes or more."""
    h = gray.shape[0]
    tones = _Tones(sr)
    tones.add(300, 600)                      # start tone
    for _ in range(4):                       # phasing: white with a short black pulse
        tones.add(2300, 92.5)
        tones.add(1500, 7.5)
    line_ms = max(1.0, (seconds - WEFAX_HEADER_S - 0.3) * 1000 / h)
    for row in gray:
        tones.add(_level(row), line_ms)
    tones.add(450, 300)                      # stop tone
    return tones.audio()


def wefax_line_seconds(height: int, seconds: float) -> float:
    return max(0.001, seconds - WEFAX_HEADER_S - 0.3) / height


# ---------------------------------------------------------------- the picture
def _font(size: int, bold: bool = True):
    from PIL import ImageFont
    for name in (("arialbd.ttf", "DejaVuSans-Bold.ttf") if bold else ("arial.ttf", "DejaVuSans.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def draw_rockall(size: tuple[int, int]):
    """A sunset over Rockall: the rock, its navigation light, the station's Yagi and the operator."""
    from PIL import Image, ImageDraw
    w, h = size
    img = Image.new("RGB", size)
    d = ImageDraw.Draw(img)
    horizon = int(h * 0.64)
    sky = [(18, 28, 68), (70, 60, 120), (214, 110, 90), (252, 190, 110)]
    for yy in range(horizon):
        t = yy / horizon * (len(sky) - 1)
        a, b = sky[int(t)], sky[min(int(t) + 1, len(sky) - 1)]
        f = t - int(t)
        d.line([(0, yy), (w, yy)], fill=tuple(int(a[i] + (b[i] - a[i]) * f) for i in range(3)))
    d.ellipse([w * .12, horizon - 26, w * .12 + 52, horizon + 26], fill=(255, 214, 140))
    for yy in range(horizon, h):
        f = (yy - horizon) / (h - horizon)
        d.line([(0, yy), (w, yy)], fill=(int(40 - 25 * f), int(70 - 40 * f), int(110 - 60 * f)))
    rng = random.Random(7)
    for _ in range(40):  # sun glitter on the sea
        x, yy = rng.uniform(0, w * .4), rng.uniform(horizon + 2, h)
        d.line([(x, yy), (x + rng.uniform(4, 14), yy)], fill=(250, 200, 140))
    cx, top = int(w * .62), int(h * .30)
    rock = [(cx - 95, h), (cx - 70, horizon + 10), (cx - 40, top + 28), (cx - 20, top + 4), (cx + 22, top),
            (cx + 46, top + 22), (cx + 70, horizon), (cx + 105, h)]
    d.polygon(rock, fill=(92, 94, 102))
    d.polygon([(cx - 40, top + 28), (cx - 20, top + 4), (cx + 22, top), (cx + 46, top + 22), (cx + 70, horizon),
               (cx + 105, h), (cx + 40, h)], fill=(70, 72, 80))
    d.polygon([(cx - 24, top + 8), (cx - 18, top + 3), (cx + 22, top), (cx + 34, top + 10), (cx + 10, top + 12)],
              fill=(236, 232, 220))  # the seabirds' contribution
    d.rectangle([cx + 26, top - 10, cx + 31, top + 2], fill=(220, 220, 220))
    d.ellipse([cx + 24, top - 16, cx + 33, top - 8], fill=(255, 250, 190))
    mx = cx - 30
    d.line([(mx, top + 14), (mx, top - 52)], fill=(205, 210, 218), width=2)
    d.line([(mx - 22, top - 50), (mx + 22, top - 50)], fill=(205, 210, 218), width=2)
    for i, half in enumerate((7, 9, 10, 8, 6)):
        ex = mx - 20 + i * 10
        d.line([(ex, top - 50 - half), (ex, top - 50 + half)], fill=(205, 210, 218), width=1)
    wx, wy = cx - 6, top - 1  # the operator in his wheelchair, in silhouette
    d.ellipse([wx - 9, wy - 18, wx + 9, wy], outline=(25, 25, 30), width=3)
    d.ellipse([wx + 12, wy - 6, wx + 18, wy], fill=(25, 25, 30))
    d.polygon([(wx - 6, wy - 30), (wx + 4, wy - 30), (wx + 6, wy - 12), (wx - 5, wy - 12)], fill=(25, 25, 30))
    d.line([(wx + 4, wy - 14), (wx + 16, wy - 14), (wx + 17, wy - 4)], fill=(25, 25, 30), width=3)
    d.ellipse([wx - 5, wy - 41, wx + 6, wy - 30], fill=(25, 25, 30))
    for bx, by in ((60, 40), (82, 52), (250, 30)):
        d.line([(bx - 6, by), (bx, by - 3), (bx + 6, by)], fill=(30, 30, 45), width=1)
    return img


def draw_testcard(size: tuple[int, int]):
    """Colour bars, a grey step wedge and a centre circle – the classic SSTV test picture."""
    from PIL import Image, ImageDraw
    w, h = size
    img = Image.new("RGB", size, (16, 16, 16))
    d = ImageDraw.Draw(img)
    bars = [(235, 235, 235), (235, 235, 16), (16, 235, 235), (16, 235, 16), (235, 16, 235), (235, 16, 16), (16, 16, 235)]
    top, bottom = int(h * .22), int(h * .70)
    for i, c in enumerate(bars):
        d.rectangle([i * w // len(bars), top, (i + 1) * w // len(bars), bottom], fill=c)
    for i in range(8):  # grey wedge
        v = 16 + i * 31
        d.rectangle([i * w // 8, bottom, (i + 1) * w // 8, int(h * .80)], fill=(v, v, v))
    r = int(h * .2)
    d.ellipse([w // 2 - r, (top + bottom) // 2 - r, w // 2 + r, (top + bottom) // 2 + r], outline=(255, 255, 255), width=3)
    d.line([(w // 2, top), (w // 2, bottom)], fill=(255, 255, 255), width=1)
    d.line([(0, (top + bottom) // 2), (w, (top + bottom) // 2)], fill=(255, 255, 255), width=1)
    return img


PICTURES = {"testcard": draw_testcard, "rockall": draw_rockall}


def _cover(img, size: tuple[int, int]):
    """Scale and centre-crop a photo to exactly fill the SSTV frame."""
    from PIL import Image
    w, h = size
    s = max(w / img.width, h / img.height)
    img = img.convert("RGB").resize((max(w, round(img.width * s)), max(h, round(img.height * s))), Image.LANCZOS)
    x, y = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def stamp(img, cfg: dict):
    """Callsign, location and time across the picture, outlined so it reads on any background."""
    from PIL import ImageDraw
    st = cfg["station"]
    d = ImageDraw.Draw(img)
    w, h = img.size
    d.text((10, 6), st["callsign"], font=_font(int(h * .14)), fill=(255, 255, 255), stroke_width=3, stroke_fill=(0, 0, 0))
    where = " · ".join(x for x in (st.get("qth", ""), st.get("locator", "").upper()) if x)
    line = f"{where}   {time.strftime('%Y-%m-%d %H:%MZ', time.gmtime())}".strip()
    d.text((10, h - int(h * .09) - 6), line, font=_font(int(h * .07)), fill=(255, 240, 120), stroke_width=2,
           stroke_fill=(0, 0, 0))
    return img


def picture(cfg: dict, mode: str):
    """A photo from sstv.folder (jpg/png/webp) if there are any, otherwise the built-in sstv.picture."""
    from PIL import Image
    size = MODES[mode]["size"]
    folder = os.path.expanduser(cfg.get("sstv", {}).get("folder") or "")
    photos = []
    if folder and os.path.isdir(folder):
        photos = [os.path.join(folder, f) for f in os.listdir(folder)
                  if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp"))]
    img = None
    if photos:
        try:
            img = _cover(Image.open(random.choice(photos)), size)
        except OSError:
            img = None
    builtin = PICTURES.get(cfg.get("sstv", {}).get("picture", "testcard"), draw_testcard)
    return stamp(img or builtin(size), cfg)


def png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
