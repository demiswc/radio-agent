import pytest

from radio_agent import __main__ as app, config, qso, speech
from radio_agent.agents import EchoAgent
from radio_agent.events import Bus
from radio_agent.sidecar import Log
from radio_agent.station import Station

ME = "November Zero Charlie Alpha Lima Lima"       # N0CALL, the station (Stephen)
THEM = "November One Charlie Alpha Lima Lima"      # N1CALL, the operator calling in
ME_PLAIN, THEM_PLAIN = "N zero C A L L", "N one C A L L"
SHORT = "N one C A L L"                            # their call starting a middle over


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


def tx(reply="screen", operator="N1CALL", **station):
    cfg = config.load(None)
    cfg["station"].update(callsign="N0CALL", operator=operator, id_every_over=False, id_interval_seconds=600,
                          **station)
    cfg["radio"].update(reply=reply, mode="soundcard")
    bus, said, rows = Bus(), [], []
    bus.subscribe(lambda n, d: n == "tx_started" and (said.append(d["text"]), rows.append(d)))
    rad = Recorder()
    t = app.Transmitter(cfg, rad, None if reply == "screen" else type("V", (), {"synth": lambda s, x: None})(),
                        Log(bus=bus))
    return t, said, rad, rows


# ---------------- framing each over ----------------
def test_first_over_has_the_full_phonetic_id(clock):
    t, said, _, rows = tx()
    t.send("Good evening, thanks for the call.")
    assert said[-1] == (f"{THEM}, this is {ME}. Good evening, thanks for the call. "
                        f"Over to {THEM_PLAIN}, {ME_PLAIN}, over.")
    assert (rows[-1]["to"], rows[-1]["de"], rows[-1]["msg"]) == ("N1CALL", "N0CALL", "Good evening, thanks for the call.")


def test_middle_overs_use_the_short_form(clock):
    t, said, _, _ = tx()
    t.send("Hello.")
    for _ in range(5):
        clock.t += 60
        t.send("Roger, all good here.")
        assert said[-1] == f"{SHORT}. Roger, all good here. {ME_PLAIN}, over."


def test_full_id_again_after_ten_minutes(clock):
    t, said, _, _ = tx()
    t.send("Hello.")
    for _ in range(9):                                     # 9 minutes of quick overs
        clock.t += 60
        t.send("Still here.")
        assert ME not in said[-1]
    clock.t += 60                                          # 10 minutes since the full ID
    t.send("Still here on the rock.")
    assert said[-1].startswith(f"{THEM}, this is {ME}.")
    clock.t += 60
    t.send("And again.")
    assert said[-1] == f"{SHORT}. And again. {ME_PLAIN}, over."


def test_new_qso_after_a_quiet_spell(clock):
    t, said, _, _ = tx()
    t.send("Hello.")
    clock.t += 11 * 60                                     # nothing exchanged for longer than new_after_minutes
    t.send("Hello again.")
    assert said[-1].startswith(f"{THEM}, this is {ME}.")


def test_closing_signs_off_and_resets(clock):
    t, said, _, rows = tx()
    t.send("Hello.")
    clock.t += 30
    t.send("Lovely to chat. Mind how you go.", signoff=True)
    assert said[-1] == f"Lovely to chat. Mind how you go. Seventy-three, {THEM}, this is {ME}, clear."
    assert rows[-1]["msg"] == "Lovely to chat. Mind how you go. 73"
    clock.t += 30                                          # the QSO state was reset: next over opens a new one
    t.send("Hello again.")
    assert said[-1].startswith(f"{THEM}, this is {ME}.")


def test_long_replies_hand_over_only_on_the_last_over(clock, monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda s: None)
    t, said, rad, _ = tx(reply="radio")
    t.max_chars = 40
    t.send("The first sentence is here. The second one is too. And the third one ends it.")
    assert len(rad.lines) == 3
    assert rad.lines[0].startswith(f"{THEM}, this is {ME}. The first") and rad.lines[0].endswith(f"{ME_PLAIN}, continuing.")
    assert rad.lines[1] == f"The second one is too. {ME_PLAIN}, continuing."
    assert rad.lines[2] == f"And the third one ends it. Over to {THEM_PLAIN}, {ME_PLAIN}, over."


def test_stand_by_never_carries_callsigns(clock):
    t, said, _, _ = tx()
    t.send("Roger, stand by.", final_over=False)
    assert said[-1] == "Roger, stand by."


def test_cq_then_the_answer_opens_a_qso_with_their_heard_callsign(clock):
    t, said, _, _ = tx(operator="")
    t.send_cq(f"C Q, C Q, C Q, this is {speech.phonetic('N0CALL', mark=True)}.", "CQ CQ CQ N0CALL")
    assert said[-1] == f"C Q, C Q, C Q, this is {ME}."    # the screens never see the phonetic marks
    clock.t += 20
    t.send("Good evening, you're five and nine.", heard="N0CALL this is G4ABC, how copy?")
    assert said[-1].startswith("Golf Four Alpha Bravo Charlie, this is ")
    assert said[-1].endswith("Over to G four A B C, N zero C A L L, over.")


def test_cq_ends_the_qso_so_the_answer_gets_the_full_id(clock):
    t, said, _, _ = tx()
    t.send("Hello.")
    clock.t += 50
    t.send_cq("C Q.", "CQ CQ CQ N0CALL")
    clock.t += 10
    t.send("Hello, who's that?")                           # nobody's call heard: station.operator
    assert said[-1].startswith(f"{THEM}, this is {ME}.")


def test_unknown_other_station_drops_their_part():
    q = qso.QSO({"station": {"callsign": "N0CALL", "operator": ""}, "qso": config.DEFAULTS["qso"]}, clock=lambda: 0)
    over = q.frame(["Hello."])[0]
    assert speech.unmark(over.spoken) == f"This is {ME}. Hello. {ME_PLAIN}, over."


def test_templates_come_from_the_config(clock):
    t, said, _, _ = tx()
    t.qso.q = {**t.qso.q, "opening": "{them_phonetic} from {me_phonetic}.", "handover": "{me}, K."}
    t.send("Hello.")
    assert said[-1] == f"{THEM} from {ME}. Hello. {ME_PLAIN}, K."


# ---------------- what the agent says ----------------
@pytest.mark.parametrize("raw,clean", [
    ("N1CALL, this is N0CALL. The tests pass. N0CALL, over.", "The tests pass."),
    (f"{THEM}, the build is green, over", "the build is green."),
    ("All good. Over to N1CALL, N0CALL, over", "All good."),
    ("Lovely to chat, 73, this is N0CALL, clear.", "Lovely to chat."),
    ("The coast is clear.", "The coast is clear."),
    ("Fix line 73", "Fix line 73"),
])
def test_agent_added_callsigns_and_over_are_stripped(raw, clean):
    assert speech.strip_framing(raw, ("N0CALL", "N1CALL")) == clean


def test_station_strips_framing_from_the_agent():
    class Chatty(EchoAgent):
        def ask(self, text):
            return "SAY: N1CALL this is N0CALL, all tests pass. Over."
    c = _cfg()
    c["station"].update(callsign="N0CALL", operator="N1CALL")
    assert Station(c, Chatty()).handle("how are the tests") == "all tests pass."


# ---------------- signing off ----------------
@pytest.mark.parametrize("text", ["Thanks for the contact, 73, over", "cheerio Stephen", "seventy three",
                                  "seventy-three", "QRT", "thanks, clear", "N1CALL clear", "clear"])
def test_station_signs_off_when_the_operator_says_goodbye(text):
    st = Station(_cfg(), EchoAgent())
    st.handle(text)
    assert st.signoff


def test_close_and_clear_the_picture_still_close_it():
    for text in ("close", "clear the picture"):
        assert speech.command_of(text, ["claude"]) == "close"
    assert speech.command_of("clear", ["claude"]) is None


def test_station_signs_off_when_the_agent_says_so():
    class Bye(EchoAgent):
        def ask(self, text):
            return "SAY: Lovely to chat. Mind how you go.\nSIGNOFF: yes"
    st = Station(_cfg(), Bye())
    assert st.handle("anyway I'd better get the dinner on") == "Lovely to chat. Mind how you go."
    assert st.signoff


@pytest.mark.parametrize("text", ["fix line 73 in the readme please", "is that clear?", "clear the screen"])
def test_ordinary_overs_dont_sign_off(text):
    st = Station(_cfg(), EchoAgent())
    st.handle(text)
    assert not st.signoff


def _cfg():
    c = config.load(None)
    c["listen"]["require_wake_word"] = False
    return c


def test_goodbye_phrases():
    assert speech.sounds_like_goodbye("nice talking to you")
    assert not speech.sounds_like_goodbye("clear the screen")


def test_callsign_heard_in_an_over():
    assert speech.find_callsign(f"Stephen, this is {THEM}, over", ("N0CALL",)) == "N1CALL"
    assert speech.find_callsign("N0CALL this is N1CALL", ("N0CALL",)) == "N1CALL"
    assert speech.find_callsign("N0CALL this is N0CALL", ("N0CALL",)) is None   # same call as ours: station.operator
    assert speech.find_callsign("N 1 C A L L calling", ()) == "N1CALL"
    assert speech.find_callsign("I need a hotel in Lima", ()) is None
