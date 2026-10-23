import threading

import numpy as np
import pytest

from radio_agent import __main__ as app, config, speech, sstv
from radio_agent.agents import EchoAgent
from radio_agent.events import Bus
from radio_agent.radio import Heard
from radio_agent.sidecar import Log
from radio_agent.station import Station


@pytest.mark.parametrize("text", ["Stephen, SSTV me a picture of your location", "can you s s t v me something",
                                  "send an SSTB please", "slow scan me Rockall", "send me a picture of the island",
                                  "send me an sstv, over"])
def test_sstv_requests(text):
    assert speech.wants_sstv(text)


@pytest.mark.parametrize("text", ["build the docker image", "what is the TV schedule", "ssh into the box", "run the tests"])
def test_not_sstv_requests(text):
    assert not speech.wants_sstv(text)


@pytest.mark.parametrize("mode", list(sstv.MODES))
def test_encode_length_matches_mode(mode):
    w, h = sstv.MODES[mode]["size"]
    audio = sstv.encode(np.zeros((h, w, 3), np.uint8), mode, 16000)
    assert abs(len(audio) / 16000 - sstv.duration(mode) - 0.05) < 0.02
    assert np.max(np.abs(audio)) <= 0.61


def test_squeezed_to_five_seconds():
    audio = sstv.encode(np.zeros((240, 320, 3), np.uint8), "robot36", 16000, seconds=5)
    assert abs(len(audio) / 16000 - 5.05) < 0.02
    assert abs(sstv.line_seconds("robot36", 5) * 240 + sstv.HEADER_S - 5) < 1e-9


def _tone_at(audio, sr, t0, ms):
    seg = audio[int(t0 * sr):int((t0 + ms / 1000) * sr)]
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    return np.fft.rfftfreq(len(seg), 1 / sr)[np.argmax(spec)]


def test_vis_header_codes_robot36():
    sr = 16000
    audio = sstv.encode(np.zeros((240, 320, 3), np.uint8), "robot36", sr)
    assert abs(_tone_at(audio, sr, 0.05, 200) - 1900) < 20             # leader
    bits = [_tone_at(audio, sr, 0.640 + 0.030 * i + 0.005, 20) for i in range(8)]  # after leaders + start bit
    code = sum((1 if abs(f - 1100) < 60 else 0) << i for i, f in enumerate(bits[:7]))
    assert code == 8 and abs(bits[7] - 1100) < 60                     # VIS 8, odd number of ones -> parity 1


def test_picture_is_stamped_and_sized(tmp_path):
    cfg = config.load(None)
    cfg["station"].update(callsign="N1CALL", qth="Rockall", locator="IO37do")
    cfg["sstv"]["folder"] = str(tmp_path)  # empty: falls back to the drawing
    img = sstv.picture(cfg, "robot36")
    assert img.size == (320, 240)
    assert sstv.png_bytes(img)[:8] == b"\x89PNG\r\n\x1a\n"


def test_builtin_pictures(tmp_path):
    cfg = config.load(None)
    cfg["sstv"]["folder"] = str(tmp_path)
    assert cfg["sstv"]["picture"] == "testcard"           # the base package: a plain test card
    a = np.asarray(sstv.picture(cfg, "robot36"))
    cfg["sstv"]["picture"] = "rockall"                    # the demo profile
    b = np.asarray(sstv.picture(cfg, "robot36"))
    assert a.shape == b.shape == (240, 320, 3) and not np.array_equal(a, b)


def test_dial_frequency():
    cfg = config.load(None)
    assert config.dial_mhz(cfg) == "145.425"              # rtlsdr.frequency default
    cfg["station"]["frequency"] = "145.66"
    assert config.dial_mhz(cfg) == "145.660"


# ---------------------------------------------------------------- the SSTV: tag from the agent
class TagAgent(EchoAgent):
    def ask(self, text):
        return "SAY: Here comes a picture of the rock.\nSSTV: rockall"


def test_sstv_tag_is_stripped_and_sent():
    sent = []
    st = Station(_cfg(), TagAgent(), on_sstv=sent.append)
    say = st.handle("what does your island look like, over")
    assert say == "Here comes a picture of the rock."
    assert "SSTV" not in say
    assert sent == ["rockall"]


def test_keyword_sends_at_once_and_only_once():
    sent = []
    st = Station(_cfg(), TagAgent(), on_sstv=sent.append)
    st.handle("send me an SSTV, over")      # keyword already sent it; the agent's tag mustn't send it again
    assert sent == ["picture"]


def test_close_command():
    bus, seen = Bus(), []
    bus.subscribe(lambda name, d: seen.append(name))
    st = Station(_cfg(), EchoAgent(), Log(bus=bus))
    assert st.handle("close, over") == "Picture closed."
    assert "sstv_close" in seen


def _cfg(tmp_path=None):
    c = config.load(None)
    c["station"].update(callsign="N1CALL", qth="Rockall")
    c["radio"]["reply"] = "screen"
    c["listen"]["require_wake_word"] = False
    c["sidecar"]["enabled"] = False
    c["sstv"]["folder"] = ""
    if tmp_path is not None:
        c["agent"]["workdir"] = str(tmp_path)
    return c


# ---------------------------------------------------------------- a whole over, through the run loop
class OneOver:
    def __init__(self):
        self.overs = [Heard(text="send me an SSTV, over")]

    def listen(self, timeout=None):
        return self.overs.pop(0) if self.overs else None

    def close(self):
        pass


def test_send_me_an_sstv_emits_the_sstv_event(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app.radio, "make", lambda c: OneOver())
    monkeypatch.setattr(app.stt, "make", lambda c: None)
    monkeypatch.setattr(app.tts, "make", lambda c: None)
    monkeypatch.setattr(app.agents, "make", lambda c: EchoAgent())
    monkeypatch.setattr(app.time, "sleep", lambda s: None)
    bus, seen, done = Bus(), [], threading.Event()
    bus.subscribe(lambda name, d: (seen.append((name, d)), name == "sstv_done" and done.set()))

    app.run(cfg, plain=True, bus=bus)
    assert done.wait(10), "the picture never finished"

    names = [n for n, _ in seen]
    assert names.index("over_finished") < names.index("sstv") < names.index("sstv_done")
    assert "agent_done" in names                          # the agent still answers as normal
    pic = dict(seen)["sstv"]
    assert pic["mode"] == "Robot 36" and (pic["width"], pic["height"]) == (320, 240)
    assert abs(pic["seconds"] - 5.05) < 0.05 and pic["audio"] is False
    assert (tmp_path / pic["path"]).exists()
