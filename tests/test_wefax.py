import io
import threading

import numpy as np
import pytest
from PIL import Image

from radio_agent import __main__ as app, config, speech, sstv, wefax
from radio_agent.agents import EchoAgent, RADIO_PROMPT, with_persona
from radio_agent.events import Bus
from radio_agent.sidecar import Log
from radio_agent.station import Station


@pytest.mark.parametrize("text,region,chart", [
    ("send me a wefax of rockall", "ne_atlantic", "analysis"),
    ("48 hour forecast for the bay of biscay", "ne_atlantic", "48h"),
    ("48 hour wind forecast for the North Sea", "ne_atlantic", "wind48"),
    ("wind and waves around hawaii", "central_pacific", "wind"),
    ("weather fax for Japan", "nw_pacific", "analysis"),
    ("satellite picture of the caribbean", "tropical_atlantic", "satellite"),
    ("hurricane chart for florida", "gulf", "cyclone"),
    ("ne_atlantic 48h", "ne_atlantic", "48h"),                 # the agent's own tag
    ("alaska 500mb", "alaska", "500mb"),
])
def test_requests_resolve_to_a_chart(text, region, chart):
    assert wefax.resolve(text) == (region, chart)


def test_unknown_area_waits_unless_theres_a_default():
    assert wefax.resolve("weather chart for Australia") is None
    assert wefax.resolve("weather chart for Australia", "ne_atlantic") == ("ne_atlantic", "analysis")


def test_every_region_has_an_analysis_and_files_look_like_noaa_charts():
    for key, r in wefax.REGIONS.items():
        assert "analysis" in r["charts"], key
        for f in r["charts"].values():
            assert f.endswith((".gif", ".jpg")) and f[:2].isalpha()


@pytest.mark.parametrize("text", ["send me a weather fax", "any wefax tonight", "wee fax please",
                                  "latest surface analysis", "weather chart for biscay"])
def test_weather_fax_words(text):
    assert wefax.asks_for_wefax(text) and speech.wants_sstv(text)


def _gif(w=1440, h=1728):
    img = Image.new("L", (w, h), 255)
    img.paste(0, (100, 100, 600, 140))  # a "title" bar
    buf = io.BytesIO()
    img.save(buf, "GIF")
    return buf.getvalue()


def test_prepare_scales_and_trims_a_tall_chart():
    img = wefax.prepare(_gif())
    assert img.width == 720 and img.height <= 720 * 1.3 and img.mode == "RGB"


def test_fetch_builds_the_noaa_url_and_caption(monkeypatch):
    seen = []
    monkeypatch.setattr(wefax, "download", lambda url: (seen.append(url), (_gif(), "Fri, 23 Oct 2026 14:50:01 GMT"))[1])
    img, caption = wefax.fetch("48 hour forecast for biscay", config.load(None))
    assert seen == ["https://tgftp.nws.noaa.gov/fax/QDTM10.gif"]
    assert caption == "NOAA · North Atlantic, east (10°E–45°W) · 48 hour surface forecast · issued 23 Oct 14:50Z"
    img, caption = wefax.fetch("https://example.org/chart.png", config.load(None))
    assert seen[-1] == "https://example.org/chart.png"


def test_download_refuses_non_web_addresses():
    with pytest.raises(ValueError):
        wefax.download("file:///etc/passwd")


def test_wefax_signal_length():
    gray = np.full((900, 720), 255, np.uint8)
    audio = sstv.encode_wefax(gray, 16000, seconds=8)
    assert abs(len(audio) / 16000 - 8.0) < 0.1


# ---------------------------------------------------------------- the station deciding when to send
def _station(agent, ready):
    c = config.load(None)
    c["listen"]["require_wake_word"] = False
    sent = []
    return Station(c, agent, on_sstv=sent.append, sstv_ready=ready), sent


class Picks(EchoAgent):
    def __init__(self, tag):
        self.tag = tag

    def ask(self, text):
        return f"SAY: Here's the latest chart.\n{self.tag}"


def test_area_named_sends_at_once_and_the_tag_doesnt_repeat_it():
    st, sent = _station(Picks("SSTV: nw_pacific 48h"), wefax.names_region)
    st.handle("weather fax for Biscay, over")
    assert sent == ["weather fax for Biscay"]


def test_no_area_waits_for_the_agent_to_pick_one():
    st, sent = _station(Picks("SSTV: tropical_atlantic analysis"), wefax.names_region)
    st.handle("send me a weather fax for Barbuda, over")      # not in the catalogue: the agent knows where it is
    assert sent == ["tropical_atlantic analysis"]


def test_no_area_and_no_tag_sends_the_default():
    st, sent = _station(EchoAgent(), wefax.names_region)
    st.handle("send me a weather fax, over")
    assert sent == ["send me a weather fax"]


def test_the_agent_is_told_how_to_pick_charts():
    c = config.load(None)
    c["sstv"]["source"] = "wefax"
    p = with_persona(RADIO_PROMPT, c)
    assert "SSTV: <region> <chart>" in p and "ne_atlantic" in p and "SSTV: https://" in p
    c["sstv"]["source"] = "picture"
    assert "SSTV: picture" in with_persona(RADIO_PROMPT, c)


# ---------------------------------------------------------------- the transmitter sending it
def _tx(monkeypatch, tmp_path, source="wefax"):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app.time, "sleep", lambda s: None)
    c = config.load(None)
    c["sstv"].update(source=source, folder="")
    c["radio"]["reply"] = "screen"
    bus, seen, done = Bus(), [], threading.Event()
    bus.subscribe(lambda n, d: (seen.append((n, d)), n == "sstv_done" and done.set()))
    return app.Transmitter(c, None, None, Log(bus=bus)), seen, done


def test_sends_the_chart_as_wefax(monkeypatch, tmp_path):
    monkeypatch.setattr(wefax, "download", lambda url: (_gif(), "Fri, 23 Oct 2026 14:50:01 GMT"))
    tx, seen, done = _tx(monkeypatch, tmp_path)
    tx.send_sstv("wind and waves around hawaii")
    assert done.wait(10)
    pic = dict(seen)["sstv"]
    assert pic["label"] == "WEFAX RX" and pic["mode"] == "WEFAX 120"
    assert "Hawaii" in pic["caption"] and "Wind & wave" in pic["caption"]
    assert pic["width"] == 720 and abs(pic["seconds"] - 8) < 0.2
    assert (tmp_path / pic["path"]).exists() and "wefax-" in pic["path"]


def test_falls_back_to_the_picture_if_the_chart_wont_download(monkeypatch, tmp_path):
    def offline(url):
        raise OSError("no internet")
    monkeypatch.setattr(wefax, "download", offline)
    tx, seen, done = _tx(monkeypatch, tmp_path)
    tx.send_sstv("weather fax for biscay")
    assert done.wait(10)
    names = [n for n, _ in seen]
    assert "error" in names and dict(seen)["sstv"]["label"] == "SSTV RX"
    assert "no internet" in dict(seen)["error"]["summary"]


def test_picture_source_only_fetches_charts_when_asked(monkeypatch, tmp_path):
    tx, _, _ = _tx(monkeypatch, tmp_path, source="picture")
    assert not tx._wants_wefax("SSTV me a picture of Rockall")
    assert tx._wants_wefax("weather fax for Rockall") and tx._wants_wefax("gulf 48h")
    assert tx.sstv_ready("send me an SSTV") and not tx.sstv_ready("send me a weather fax")


@pytest.mark.parametrize("source", ["picture", "wefax"])
def test_qsl_card_is_always_the_stations_picture(monkeypatch, tmp_path, source):
    tx, _, _ = _tx(monkeypatch, tmp_path, source=source)
    for ask in ("send me your SSTV QSL card", "SSTV me your QSL", "can I have a photo of the station by SSTV"):
        assert not tx._wants_wefax(ask), ask
    assert tx._wants_wefax("SSTV me the 48 hour forecast for Biscay")
    assert tx._wants_wefax("weather fax picture for Biscay")          # asking for a fax wins over "picture"
    assert not tx._wants_wefax("send me an SSTV of Rockall") or source == "wefax"
