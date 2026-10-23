import numpy as np

from radio_agent import config, dtmf, speech
from radio_agent.agents import EchoAgent
from radio_agent.station import Station


def cfg(**listen):
    c = config.load(None)
    c["station"]["callsign"] = "N1CALL"
    c["listen"].update(listen)
    return c


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ---------------- DTMF ----------------
def test_dtmf_roundtrip():
    sr = 16000
    audio = np.concatenate([dtmf.tone(k, sr) for k in "*1234#"])
    assert dtmf.decode(audio, sr) == "*1234#"


def test_dtmf_ignores_speechlike_noise():
    sr = 16000
    rng = np.random.default_rng(0)
    t = np.arange(sr * 2) / sr
    voice = 0.3 * np.sin(2 * np.pi * 220 * t) * np.sin(2 * np.pi * 3 * t) + 0.05 * rng.standard_normal(len(t))
    assert dtmf.decode(voice.astype(np.float32), sr) == ""


def test_dtmf_with_noise():
    sr = 16000
    rng = np.random.default_rng(1)
    audio = np.concatenate([dtmf.tone(k, sr) for k in "19"])
    audio = audio + 0.05 * rng.standard_normal(len(audio)).astype(np.float32)
    assert dtmf.decode(audio, sr) == "19"


# ---------------- speech helpers ----------------
def test_phonetic():
    assert speech.phonetic("N1CALL") == "November One Charlie Alpha Lima Lima"


def test_strip_over_and_wake():
    assert speech.strip_over("check the tests, over.") == "check the tests"
    assert speech.strip_over("Overview of the code") == "Overview of the code"
    assert speech.ends_with_over("that's it, over and out.")
    assert not speech.ends_with_over("see if you can figure it out")
    assert speech.ends_with_over("Can you do that? Over?")
    assert speech.strip_wake_word("Hey Claude, run the tests", ["claude"]) == "run the tests"


def test_commands_only_match_whole_over():
    w = ["claude"]
    assert speech.command_of("Confirm, over.", w) == "confirm"
    assert speech.command_of("Claude, say again. Over", w) == "repeat"
    assert speech.command_of("can you confirm the tests pass", w) is None


def test_parse_agent_output():
    r = speech.parse_agent_output(
        "SAY: Found it. The webhook drops the tenant ID.\nDETAIL: ```ts\nfoo()\n```\nCONFIRM: patch route.ts")
    assert r.say == "Found it. The webhook drops the tenant ID."
    assert "foo()" in r.detail
    assert r.confirm == "patch route.ts"


def test_parse_agent_output_fallback():
    r = speech.parse_agent_output("Sure. Here is the code: ```x=1``` and see https://example.com for more. Third.")
    assert "```" not in r.say and "https" not in r.say
    assert r.confirm is None


def test_chunking():
    text = " ".join(f"Sentence number {i} is here." for i in range(30))
    chunks = speech.chunk_sentences(text, 120)
    assert all(len(c) <= 120 for c in chunks)
    assert " ".join(chunks) == text


# ---------------- station flow ----------------
def test_ignores_traffic_not_for_us():
    s = Station(cfg(), EchoAgent(), clock=Clock())
    assert s.handle("CQ CQ this is MM0ABC") is None


def test_wake_then_conversation_window():
    clk = Clock()
    s = Station(cfg(conversation_window_seconds=60), EchoAgent(), clock=clk)
    assert "I heard: what's in the repo" in s.handle("Claude, what's in the repo? Over")
    clk.t += 30
    assert s.handle("and the readme? over") is not None      # still in conversation
    clk.t += 120
    assert s.handle("and the readme? over") is None           # window closed


def test_confirm_flow_spoken_and_dtmf():
    clk = Clock()
    s = Station(cfg(), EchoAgent(), clock=clk)
    out = s.handle("Claude, fix the checkout bug. Over")
    assert "confirm" in out.lower() and s.pending
    out = s.handle("Confirm. Over")
    assert out.startswith("Done.") and s.pending is None
    s.handle("Claude, change the colour. Over")
    assert s.pending
    assert "Cancelled" in s.handle(dtmf="9")
    assert s.pending is None


def test_repeat():
    s = Station(cfg(), EchoAgent(), clock=Clock())
    first = s.handle("Claude, hello. Over")
    assert s.handle("Say again, over") == first


def test_pin_lock():
    clk = Clock()
    s = Station(cfg(pin="4321", lock_after_idle_seconds=100), EchoAgent(), clock=clk)
    assert "locked" in s.handle("Claude, hello").lower()
    assert s.handle(dtmf="*1111#") is None
    assert "Unlocked" in s.handle(dtmf="*4321#")
    assert "I heard" in s.handle("Claude, hello")
    clk.t += 500
    assert "locked" in s.handle("Claude, hello").lower()


def test_no_standby_when_agent_is_quick():
    sent = []
    s = Station(cfg(), EchoAgent(), ack=sent.append, clock=Clock())
    assert "I heard" in s.handle("Claude, hello")
    assert sent == []


def test_standby_when_agent_is_slow():
    import time

    class SlowAgent(EchoAgent):
        def ask(self, text):
            time.sleep(0.3)
            return super().ask(text)

    sent = []
    s = Station(cfg(standby_after_seconds=0.05), SlowAgent(), ack=sent.append, clock=Clock())
    assert "I heard" in s.handle("Claude, hello")
    assert sent == ["Roger, stand by."]


def test_standby_always_when_zero():
    sent = []
    s = Station(cfg(standby_after_seconds=0), EchoAgent(), ack=sent.append, clock=Clock())
    s.handle("Claude, hello")
    assert sent == ["Roger, stand by."]


def test_loose_confirm_only_when_pending():
    s = Station(cfg(), EchoAgent(), clock=Clock())
    s.handle("Claude, fix the header. Over")
    assert s.pending
    out = s.handle("Confirm, go ahead.")
    assert out.startswith("Done.") and s.pending is None
    s.handle("Claude, change the colour. Over")
    assert s.handle("Yeah you can go ahead, over").startswith("Done.")


def test_loose_confirm_ignored_without_pending():
    s = Station(cfg(), EchoAgent(), clock=Clock())
    out = s.handle("Claude, go ahead and tell me a joke. Over")
    assert "I heard" in out


def test_collect_over_squelch_gate():
    from radio_agent.radio import collect_over
    r = config.load(None)["radio"]
    r.update(squelch_threshold=0.05, open_ms=40, hang_ms=100, max_listen_seconds=10)
    silence = [np.zeros(320, np.float32)] * 20
    talk = [0.3 * np.ones(320, np.float32)] * 30
    blocks = iter(silence + talk + silence + [None])
    audio = collect_over(lambda: next(blocks), r)
    assert 30 * 320 <= len(audio) <= 75 * 320  # talk + up to ~0.5 s pre-roll + ~0.3 s tail


def test_roger_beep_ends_over_at_once():
    from radio_agent.radio import collect_over
    r = config.load(None)["radio"]
    r.update(squelch_threshold=0.05, open_ms=40, hang_ms=1500, max_listen_seconds=10)
    t = np.arange(320) / 16000
    silence = [np.zeros(320, np.float32)] * 20
    rng = np.random.default_rng(0)
    talk = [(0.3 * rng.standard_normal(320)).astype(np.float32) for _ in range(30)]
    beep = [(0.3 * np.sin(2 * np.pi * 1000 * (t + i * 0.02))).astype(np.float32) for i in range(7)]
    blocks = iter(silence + talk + beep + silence * 10 + [None])
    info = {}
    audio = collect_over(lambda: next(blocks), r, info=info)
    assert info.get("roger_beep")
    assert len(audio) <= (30 + 30) * 320          # stopped at the beep, not 1.5 s of hang later
    assert not np.any(np.isclose(audio[-320:], beep[-1]))  # beep itself trimmed off


def test_speech_is_not_a_roger_beep():
    from radio_agent.radio import collect_over
    r = config.load(None)["radio"]
    r.update(squelch_threshold=0.05, open_ms=40, hang_ms=100, max_listen_seconds=10)
    rng = np.random.default_rng(1)
    talk = [(0.3 * rng.standard_normal(320)).astype(np.float32) for _ in range(50)]
    blocks = iter([np.zeros(320, np.float32)] * 10 + talk + [np.zeros(320, np.float32)] * 20 + [None])
    info = {}
    collect_over(lambda: next(blocks), r, info=info)
    assert "roger_beep" not in info
