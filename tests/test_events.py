import io

import numpy as np
from rich.console import Console

from radio_agent import __main__ as app, config
from radio_agent.agents import EchoAgent
from radio_agent.console import ConsoleUI
from radio_agent.events import Bus
from radio_agent.radio import Heard
from radio_agent.sidecar import Log, Spectrum, over_stats


class FakeRadio:
    """Two transmissions of one stitched over (the second ends with a roger beep), then the end."""

    def __init__(self):
        rng = np.random.default_rng(0)
        talk = (0.3 * rng.standard_normal(16000)).astype(np.float32)
        self.overs = [Heard(audio=talk, sample_rate=16000),
                      Heard(audio=talk, sample_rate=16000, meta={"roger_beep": True})]
        self.on_open = None
        self.monitor = None

    def listen(self, timeout=None):
        if not self.overs:
            return None
        if self.on_open:
            self.on_open()
        return self.overs.pop(0)

    def close(self):
        pass


class FakeEars:
    def __init__(self):
        self.said = iter(["Stephen, what is the weather", "on Rockall today"])

    def transcribe(self, audio, sr, context=""):
        return next(self.said)


class NoVoice:
    def synth(self, text):
        return None


def plain(renderable) -> str:
    c = Console(file=io.StringIO(), width=100, color_system=None)
    c.print(renderable)
    return c.file.getvalue()


def exchange_cfg(tmp_path):
    c = config.load(None)
    c["station"]["callsign"] = "N1CALL"
    c["radio"]["reply"] = "screen"
    c["listen"].update(require_wake_word=False, wait_for_over=True)
    c["agent"]["workdir"] = str(tmp_path)
    c["sidecar"]["enabled"] = False
    return c


def test_one_full_exchange_event_sequence(tmp_path, monkeypatch):
    cfg = exchange_cfg(tmp_path)
    monkeypatch.setattr(app.radio, "make", lambda c: FakeRadio())
    monkeypatch.setattr(app.stt, "make", lambda c: FakeEars())
    monkeypatch.setattr(app.tts, "make", lambda c: NoVoice())
    monkeypatch.setattr(app.agents, "make", lambda c: EchoAgent())
    bus, seen = Bus(), []
    bus.subscribe(lambda name, d: seen.append((name, d)))

    app.run(cfg, plain=True, bus=bus)

    assert [n for n, _ in seen] == [
        "over_started", "stt_done", "over_waiting",      # first transmission, no "over" yet
        "over_started", "stt_done", "over_finished",     # second one ends with the roger beep
        "agent_started", "agent_done", "tx_started", "tx_done",
    ]
    finished = dict(seen)["over_finished"]
    assert finished["text"] == "Stephen, what is the weather on Rockall today"
    assert finished["ended_by"] == "roger beep"
    assert finished["rx_s"] == 2.0
    done = dict(seen)["agent_done"]
    assert "weather" in done["say"] and done["stats"]["name"] == "Echo"
    assert dict(seen)["tx_started"]["ack"] is False


def test_both_views_follow_the_same_events(tmp_path):
    cfg = exchange_cfg(tmp_path)
    bus = Bus()
    web = Log(bus=bus)
    out = io.StringIO()
    ui = ConsoleUI(cfg, bus, console=Console(file=out, width=80, color_system=None))
    ui.ready = True
    bus.emit("over_started")
    assert "● RX" in plain(ui.status()) and "ctrl+c to quit" in plain(ui.status())
    bus.emit("stt_done", text="radio check", rx_s=2.0, stt_s=0.4, db=12, dur=2.0, freq=900)
    bus.emit("over_finished", text="radio check", rx_s=2.0, stt_s=0.4, ended_by="over")
    bus.emit("agent_started", request="radio check")
    assert web.state == "thinking"
    bus.emit("tx_started", text="Roger, stand by.", ack=True)
    assert web.state == "tx"
    bus.emit("tx_done")
    assert web.state == "thinking"
    bus.emit("agent_done", say="Loud and clear from Rockall.", detail="", pending=None,
             stats={"name": "Claude Code", "seconds": 7.7, "thinking": 7.6, "turns": 1})
    bus.emit("ignored", text="js", reason="noise")
    text = out.getvalue()
    assert "› radio check" in text and "RX 2.0s · STT 0.4s · ended by over" in text
    assert "● Loud and clear from Rockall." in text
    assert "Claude Code 7.7s (7.6s thinking) · 1 turn" in text
    assert "Roger, stand by" not in text                     # the ack is a status, not a block
    assert "(ignored – noise)" in plain(ui.status())
    kinds = [e["kind"] for e in web.snapshot()["events"]]
    assert kinds == ["rx", "tx", "rx"]                          # heard, stand-by, the ignored noise


def test_waterfall_row_and_over_stats():
    sr = 16000
    t = np.arange(Spectrum.N) / sr
    row = np.frombuffer(Spectrum().row(np.sin(2 * np.pi * 1500 * t).astype(np.float32), sr), np.uint8)
    assert len(row) == Spectrum.BINS
    assert abs(int(np.argmax(row)) - Spectrum.BINS // 2) <= 1   # 1500 Hz sits mid-way across 0–3 kHz
    tone = 0.3 * np.sin(2 * np.pi * 800 * np.arange(sr) / sr).astype(np.float32)
    s = over_stats(tone, sr, threshold=0.05)
    assert s["dur"] == 1.0 and abs(s["freq"] - 800) <= 2 and s["db"] > 0


def test_profile_layers_over_config(tmp_path):
    base = tmp_path / "config.yaml"
    base.write_text("station:\n  callsign: N0CALL\nradio:\n  mode: soundcard\n", encoding="utf-8")
    prof = tmp_path / "persona.yaml"
    prof.write_text("station:\n  name: Stephen\n"
                    "agent:\n  persona: You are {name}, station {callsign}, talking to {operator}.\n", encoding="utf-8")
    c = config.load(str(base), [str(prof)])
    assert (c["station"]["callsign"], c["station"]["name"], c["radio"]["mode"]) == ("N0CALL", "Stephen", "soundcard")
    from radio_agent.agents import RADIO_PROMPT, with_persona
    c["station"]["operator"] = "N1CALL"
    assert with_persona(RADIO_PROMPT, c).endswith("You are Stephen, station N0CALL, talking to N1CALL.")


def test_persona_is_added_to_the_prompt():
    from radio_agent.agents import RADIO_PROMPT, with_persona
    c = config.load(None)
    plain = with_persona(RADIO_PROMPT, c)
    assert plain.startswith(RADIO_PROMPT) and "SSTV: picture" in plain   # it's told it can send the picture
    c["agent"]["persona"] = "Your name is Stephen."
    assert with_persona(RADIO_PROMPT, c).endswith("\n\nYour name is Stephen.")
