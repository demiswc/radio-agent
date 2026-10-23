import pytest

from radio_agent import __main__ as app, config, speech
from radio_agent.agents import EchoAgent
from radio_agent.events import Bus
from radio_agent.sidecar import Log
from radio_agent.station import Station

PHON = "November Zero Charlie Alpha Lima Lima"


class Clock:
    t = 10_000.0


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(app.time, "time", lambda: c.t)
    return c


class Recorder:
    def __init__(self):
        self.lines = []

    def transmit_text(self, line):
        self.lines.append(line)


def tx(reply="screen"):
    cfg = config.load(None)
    cfg["station"].update(callsign="N0CALL", id_every_over=False, id_interval_seconds=600)
    cfg["radio"].update(reply=reply, mode="soundcard")
    bus, said = Bus(), []
    bus.subscribe(lambda n, d: n == "tx_started" and said.append(d["text"]))
    rad = Recorder()
    t = app.Transmitter(cfg, rad, None if reply == "screen" else type("V", (), {"synth": lambda s, x: None})(),
                        Log(bus=bus))
    return t, said, rad


def test_id_on_first_reply_then_only_occasionally(clock):
    t, said, _ = tx()
    t.send("Good evening, thanks for the call.")
    assert said[-1].endswith(f"This is {PHON}.")           # start of the QSO
    for _ in range(5):
        clock.t += 60
        t.send("Roger, all good here.")
        assert PHON not in said[-1]                        # not after every over
    clock.t += 400                                         # 10 minutes since the last ID
    t.send("Still here on the rock.")
    assert said[-1].endswith(f"This is {PHON}.")


def test_signoff_says_the_callsign_and_clear(clock):
    t, said, _ = tx()
    t.send("Hello.")
    clock.t += 30
    t.send("Thanks for the contact, 73.", signoff=True)
    assert said[-1] == f"Thanks for the contact, 73. This is {PHON}, N0CALL, clear."


def test_stand_by_never_carries_the_id(clock):
    t, said, _ = tx()
    t.send("Roger, stand by.", final_over=False)
    assert said[-1] == "Roger, stand by."


def test_new_qso_after_a_long_gap_ids_again(clock):
    t, said, _ = tx()
    t.send("Hello.")
    clock.t += 200                                         # longer than the conversation window
    t.send("Hello again.")
    assert said[-1].endswith(f"This is {PHON}.")


def test_on_the_radio_other_overs_just_end_with_over(clock):
    t, said, rad = tx(reply="radio")
    t.send("Good evening.")
    assert rad.lines[-1] == f"Good evening. This is {PHON}, over."
    clock.t += 30
    t.send("Fine thanks.")
    assert rad.lines[-1] == "Fine thanks. Over."


def test_cq_counts_as_an_id_but_answering_still_gives_the_call(clock):
    t, said, _ = tx()
    t.send_cq(f"C Q, C Q, C Q. This is {PHON}.", "CQ CQ CQ N0CALL")
    clock.t += 20
    t.send("N1CALL, thanks for coming back.")              # first reply of the QSO: give the call
    assert said[-1].endswith(f"This is {PHON}.")


@pytest.mark.parametrize("text", ["Thanks for the contact, 73, over", "cheerio Stephen", "seventy three"])
def test_station_signs_off_when_the_operator_says_goodbye(text):
    st = Station(_cfg(), EchoAgent())
    st.handle(text)
    assert st.signoff


def test_station_signs_off_when_the_agent_says_so():
    class Bye(EchoAgent):
        def ask(self, text):
            return "SAY: Lovely to chat. Mind how you go.\nSIGNOFF: yes"
    st = Station(_cfg(), Bye())
    assert st.handle("anyway I'd better get the dinner on") == "Lovely to chat. Mind how you go."
    assert st.signoff


def test_ordinary_overs_dont_sign_off():
    st = Station(_cfg(), EchoAgent())
    st.handle("fix line 73 in the readme please")
    assert not st.signoff


def _cfg():
    c = config.load(None)
    c["listen"]["require_wake_word"] = False
    return c


def test_goodbye_phrases():
    assert speech.sounds_like_goodbye("nice talking to you")
    assert not speech.sounds_like_goodbye("clear the screen")
