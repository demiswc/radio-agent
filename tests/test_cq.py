import random

from radio_agent import config, cq, speech
from radio_agent.events import Bus


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(level=0.0):
    cfg = config.load(None)
    cfg["station"].update(callsign="N0CALL", qth="Rockall", locator="IO37do")
    cfg["cq"].update(enabled=True, interval_seconds=[60, 60], first_after_seconds=20, quiet_after_seconds=45)
    bus, clock, calls = Bus(), Clock(), []
    caller = cq.CQCaller(cfg, bus, lambda spoken, shown: calls.append(shown), level=lambda: level,
                         clock=clock, rng=random.Random(1))
    return caller, bus, clock, calls


def run(caller, clock, seconds, step=0.5):
    for _ in range(int(seconds / step)):
        clock.t += step
        caller.tick()


def test_calls_cq_on_its_own_at_intervals():
    caller, bus, clock, calls = make()
    run(caller, clock, 19)
    assert calls == []                                    # first call after 20 s
    run(caller, clock, 2)
    assert calls == ["CQ CQ CQ N0CALL IO37DO"]
    run(caller, clock, 120)
    assert len(calls) == 3                                # then every 60 s
    assert speech.unmark(caller.spoken) == ("C Q, C Q, C Q, this is November Zero Charlie Alpha Lima Lima, "
                                            "November Zero Charlie Alpha Lima Lima, calling C Q and listening.")


def test_stops_during_a_qso_and_resumes_after_45_quiet_seconds():
    caller, bus, clock, calls = make()
    run(caller, clock, 21)
    assert len(calls) == 1
    # the operator calls in: a few overs back and forth over ~2 minutes
    for _ in range(4):
        bus.emit("over_started")
        run(caller, clock, 5)
        bus.emit("stt_done", text="hello")
        bus.emit("over_finished", text="hello")
        bus.emit("agent_started")
        run(caller, clock, 8)
        bus.emit("agent_done", say="hi")
        bus.emit("tx_started", text="hi")
        run(caller, clock, 4)
        bus.emit("tx_done")
        run(caller, clock, 15)
    assert len(calls) == 1                                # silent all through the QSO
    run(caller, clock, 29)                                # 15 + 29 = 44 s since the last reply ended
    assert len(calls) == 1                                # not before 45 s of quiet
    run(caller, clock, 10)
    assert len(calls) == 2                                # back to calling soon after


def test_its_own_cq_calls_dont_count_as_a_qso():
    caller, bus, clock, calls = make()
    run(caller, clock, 21)
    bus.emit("tx_started", text="CQ", cq=True)
    bus.emit("tx_done", cq=True)
    run(caller, clock, 61)
    assert len(calls) == 2


def test_noise_doesnt_hold_it_off_but_a_busy_channel_does():
    caller, bus, clock, calls = make()
    bus.emit("over_started")
    bus.emit("ignored", text="js", reason="noise")        # a blip, not a QSO
    run(caller, clock, 21)
    assert len(calls) == 1
    busy, _, bclock, bcalls = make(level=1.0)             # squelch open: someone's on frequency
    run(busy, bclock, 200)
    assert bcalls == []


def test_skipped_if_something_else_is_on_air():
    caller, bus, clock, calls = make()
    caller.send = lambda spoken, shown: False             # transmitter busy: send_cq declines
    run(caller, clock, 21)
    assert caller.next_at > clock.t                       # rescheduled, not stuck retrying every tick


def test_never_automatic_on_air_unless_allowed():
    cfg = config.load(None)
    cfg["cq"]["enabled"] = True
    cfg["radio"]["reply"] = "speaker"
    assert cq.allowed(cfg)
    cfg["radio"]["reply"] = "radio"
    assert not cq.allowed(cfg)
    cfg["cq"]["on_air"] = True
    assert cq.allowed(cfg)
