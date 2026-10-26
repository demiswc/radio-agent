"""WEFAX: the latest radiofax weather chart for the area the operator asks about.

The charts come from NOAA's radiofax archive (the same charts US Coast Guard stations broadcast
on HF), which is updated as each new chart is issued. A request like "send me the 48 hour
forecast for Biscay" is matched to a region and a chart; anything not in the catalogue can be
fetched by URL (the agent can find one with a web search and pass it as "SSTV: https://…").
"""
from __future__ import annotations

import io
import re
import time
import urllib.request

BASE = "https://tgftp.nws.noaa.gov/fax/"

# region -> name, chart files (NOAA "most current" products), and the places that mean it
REGIONS: dict[str, dict] = {
    "ne_atlantic": {
        "name": "North Atlantic, east (10°E–45°W)",
        "charts": {"analysis": "PYAA11.gif", "24h": "PPAE10.gif", "48h": "QDTM10.gif", "wind": "PWAE10.gif",
                   "wind48": "PJAI10.gif", "500mb": "PPAA10.gif", "satellite": "evnt99.jpg"},
        "places": ["rockall", "uk", "united kingdom", "britain", "england", "scotland", "wales", "ireland", "iceland",
                   "faroe", "faroes", "shetland", "orkney", "hebrides", "north sea", "irish sea", "english channel",
                   "channel", "biscay", "bay of biscay", "france", "spain", "portugal", "norway", "azores", "canaries",
                   "madeira", "europe", "western europe", "east atlantic", "eastern atlantic", "northeast atlantic",
                   "north east atlantic", "fastnet", "sole", "malin", "bailey"],
    },
    "nw_atlantic": {
        "name": "North Atlantic, west (40°W–95°W)",
        "charts": {"analysis": "PYAA12.gif", "24h": "PPAE10.gif", "48h": "QDTM10.gif", "wind": "PWAA90.gif",
                   "wind48": "PJAI10.gif", "500mb": "PPAA10.gif", "satellite": "evnt99.jpg"},
        "places": ["new england", "boston", "new york", "maine", "nova scotia", "newfoundland", "canada", "labrador",
                   "greenland", "bermuda", "gulf stream", "grand banks", "east coast", "eastern seaboard",
                   "west atlantic", "western atlantic", "northwest atlantic", "north west atlantic", "atlantic"],
    },
    "tropical_atlantic": {
        "name": "Tropical Atlantic & Caribbean (5°S–50°N, 0–70°W)",
        "charts": {"analysis": "PYEA11.gif", "24h": "PYEE10.gif", "48h": "PYEI10.gif", "72h": "PYEK10.gif",
                   "wind": "PWEE11.gif", "wind48": "PWEI11.gif", "cyclone": "PWEK11.gif", "satellite": "evnt99.jpg"},
        "places": ["caribbean", "tropical atlantic", "cape verde", "west africa", "puerto rico", "bahamas",
                   "jamaica", "cuba", "antilles", "barbados", "trinidad"],
    },
    "gulf": {
        "name": "Gulf of Mexico & US tropical (west half)",
        "charts": {"analysis": "PYEB11.gif", "24h": "PYEE10.gif", "48h": "PYEI10.gif", "72h": "PYEK10.gif",
                   "wind": "PWEE11.gif", "wind48": "PWEI11.gif", "cyclone": "PWEK11.gif", "satellite": "evst99.jpg"},
        "places": ["gulf of mexico", "gulf", "florida", "texas", "louisiana", "new orleans", "houston", "yucatan"],
    },
    "ne_pacific": {
        "name": "NE Pacific (115°W–175°W)",
        "charts": {"analysis": "PYBA90.gif", "24h": "PPBE10.gif", "48h": "PWBI10.gif", "wind": "PWBA90.gif",
                   "wind48": "PJBI10.gif", "500mb": "PPBA10.gif", "satellite": "evpn99.jpg"},
        "places": ["california", "oregon", "washington", "seattle", "san francisco", "los angeles", "west coast",
                   "british columbia", "vancouver", "gulf of alaska", "northeast pacific", "north east pacific",
                   "east pacific", "eastern pacific", "pacific"],
    },
    "nw_pacific": {
        "name": "NW Pacific (175°W–135°E)",
        "charts": {"analysis": "PYBA91.gif", "24h": "PPBE10.gif", "48h": "PWBI10.gif", "500mb": "PPBA10.gif",
                   "satellite": "evpn99.jpg"},
        "places": ["japan", "kamchatka", "kuril", "aleutian", "aleutians", "korea", "sea of japan",
                   "northwest pacific", "north west pacific", "west pacific", "western pacific"],
    },
    "tropical_pacific": {
        "name": "Tropical east Pacific (20°S–30°N, east of 145°W)",
        "charts": {"analysis": "PYFA90.gif", "24h": "PYFE10.gif", "48h": "PYFI10.gif", "72h": "PYFK10.gif",
                   "wind": "PWFE10.gif", "wind48": "PWFI10.gif", "satellite": "evpn10.jpg"},
        "places": ["galapagos", "panama", "central america", "costa rica", "baja", "mexico", "acapulco",
                   "tropical pacific", "tropical east pacific", "equator", "equatorial pacific"],
    },
    "central_pacific": {
        "name": "Central & South Pacific (Hawaii)",
        "charts": {"analysis": "PPBA11.gif", "24h": "PYFE11.gif", "48h": "PYFI11.gif", "72h": "PYFK11.gif",
                   "wind": "PJFB10.gif", "wind48": "PJFI10.gif", "satellite": "evpn99.jpg"},
        "places": ["hawaii", "honolulu", "fiji", "samoa", "tahiti", "polynesia", "south pacific", "central pacific",
                   "marshall islands", "guam"],
    },
    "alaska": {
        "name": "Arctic & Alaska",
        "charts": {"analysis": "PYCA10.gif", "24h": "PPBE10.gif", "48h": "PWBI10.gif", "wind": "PWBE10.gif",
                   "wind48": "PJBI10.gif", "500mb": "PPBA10.gif", "satellite": "evpn99.jpg"},
        "places": ["alaska", "arctic", "bering", "bering sea", "chukchi", "beaufort", "north pole", "anchorage"],
    },
}

CHART_NAMES = {"analysis": "Surface analysis", "24h": "24 hour surface forecast", "48h": "48 hour surface forecast",
               "72h": "72 hour surface forecast", "wind": "Wind & wave", "wind48": "48 hour wind & wave forecast",
               "500mb": "500 mb upper air analysis", "satellite": "Satellite image", "cyclone": "Tropical cyclone danger area"}

_WEFAX_WORDS = re.compile(r"\b(w[ei]e?[\s-]?fax|weather[\s-]?fax|radio[\s-]?fax|fax (chart|map)|"
                          r"weather (chart|map)|surface (analysis|chart)|synoptic|isobars?|pressure chart)\b", re.I)


def asks_for_wefax(text: str) -> bool:
    """'send me a weather fax', 'WEFAX for Biscay', 'latest surface analysis'…"""
    return bool(_WEFAX_WORDS.search(text or ""))


def _chart_of(t: str) -> str:
    wind = re.search(r"\b(wind|winds|wave|waves|wind48|sea state|swell)\b", t)
    if re.search(r"\b(72|72h|72hr|seventy two|three day)\b", t):
        return "72h"
    if re.search(r"\b(48|48h|48hr|wind48|forty eight|two day|day after tomorrow)\b", t):
        return "wind48" if wind else "48h"
    if wind:
        return "wind"
    if re.search(r"\b(500|500mb|upper air|upper level|jet stream)\b", t):
        return "500mb"
    if re.search(r"\b(satellite|cloud|clouds|infra red|infrared)\b", t):
        return "satellite"
    if re.search(r"\b(hurricane|hurricanes|cyclone|cyclones|tropical storm)\b", t):
        return "cyclone"
    if re.search(r"\b(24|24h|24hr|twenty four|tomorrow|forecast|prognosis|prog)\b", t):
        return "24h"
    return "analysis"


def _words(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9_]+", " ", (text or "").lower()) + " "


def names_region(text: str) -> bool:
    """Does the request name somewhere in the catalogue?"""
    return resolve(text) is not None


def resolve(text: str, default_region: str | None = None) -> tuple[str, str] | None:
    """(region, chart) for a request, or None if it names no area we know and there's no default.

    A region key in the text ("ne_atlantic") wins, then the longest place name mentioned.
    """
    t = _words(text)
    region = next((k for k in REGIONS if f" {k} " in t or f" {k.replace('_', ' ')} " in t), None)
    if region is None:
        best = 0
        for key, r in REGIONS.items():
            for place in r["places"]:
                if f" {place} " in t and len(place) > best:
                    region, best = key, len(place)
    region = region or default_region
    if region not in REGIONS:
        return None
    chart = _chart_of(t)
    if chart not in REGIONS[region]["charts"]:
        chart = "analysis"
    return region, chart


_cache: dict[str, tuple[float, bytes, str]] = {}


def download(url: str, timeout: float = 15, max_bytes: int = 8_000_000) -> tuple[bytes, str]:
    """(image bytes, Last-Modified). Cached for two minutes so repeated requests don't hammer the server."""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"not a web address: {url}")
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < 120:
        return hit[1], hit[2]
    req = urllib.request.Request(url, headers={"User-Agent": "radio-agent (amateur radio WEFAX viewer)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # pragma: no cover - network
        kind = r.headers.get("Content-Type", "")
        if kind and not kind.startswith("image/"):
            raise ValueError(f"{url} is {kind}, not an image")
        data = r.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"{url} is too big")
        modified = r.headers.get("Last-Modified", "")
    _cache[url] = (time.time(), data, modified)
    return data, modified


def prepare(data: bytes, max_w: int = 720, max_ratio: float = 1.3):
    """Decode a chart (GIF/TIFF/JPEG/PNG) and size it for the receiver window, keeping it legible."""
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    img.seek(0)
    img = img.convert("RGB")
    if img.width > max_w:
        img = img.resize((max_w, round(img.height * max_w / img.width)), Image.LANCZOS)
    if img.height > img.width * max_ratio:  # very tall: trim the bottom (charts keep their title at the top)
        img = img.crop((0, 0, img.width, round(img.width * max_ratio)))
    return img


def fetch(subject: str, cfg: dict):
    """(image, caption) for "Biscay 48 hour", "ne_atlantic wind", or a direct image URL."""
    w = cfg.get("wefax", {})
    subject = (subject or "").strip()
    url = re.search(r"https?://\S+", subject)
    if url:
        link = url.group(0).rstrip(").,'\"")
        data, modified = download(link)
        return prepare(data), f"{link}{_when(modified)}"
    hit = resolve(subject, w.get("default_region", "ne_atlantic"))
    if hit is None:
        raise LookupError(f"no WEFAX chart for {subject!r}")
    region, chart = hit
    data, modified = download(w.get("base_url", BASE) + REGIONS[region]["charts"][chart])
    return prepare(data), f"NOAA · {REGIONS[region]['name']} · {CHART_NAMES[chart]}{_when(modified)}"


def _when(modified: str) -> str:
    try:
        t = time.strptime(modified, "%a, %d %b %Y %H:%M:%S GMT")
        return time.strftime(" · issued %d %b %H:%MZ", t)
    except (TypeError, ValueError):
        return ""


def prompt_help(picture_first: bool = False) -> str:
    """What the agent is told, so it can pick the right chart for anywhere in the world.

    picture_first: the station normally sends its own picture, and charts only when asked for weather.
    """
    regions = "; ".join(f"{k} = {r['name']}" for k, r in REGIONS.items())
    if picture_first:
        lead = ("SSTV: (optional) the station can send the operator a slow-scan TV picture of its location: if they "
                "ask for a picture or an SSTV, add the line 'SSTV: picture'. If they ask for a weather fax or a "
                "weather chart instead, add the line 'SSTV: <region> <chart>'. ")
    else:
        lead = ("SSTV: (optional) the station shows the operator the latest WEFAX weather chart. When they ask for "
                "a picture, an SSTV, a weather fax or a weather chart, add the line 'SSTV: <region> <chart>'. ")
    return (lead +
            f"Regions: {regions}. Charts: analysis, 24h, 48h, 72h, wind, wind48, 500mb, satellite, cyclone. "
            "Pick the region that covers the place they name. If none covers it (e.g. Australia, the "
            "Mediterranean or the Indian Ocean), search the web for the latest radiofax or weather chart image "
            "for that area and give its direct image address instead: 'SSTV: https://…'. Say in the SAY line "
            "which chart you're sending.")
