import os

import pytest

from radio_agent import config, contact, hamqth, logbook, qso, speech, weather
from radio_agent.agents import EchoAgent
from radio_agent.events import Bus
from radio_agent.station import Station


class Clock:
    def __init__(self):
        self.t = 100_000.0

    def __call__(self):
        return self.t


def cfg(tmp_path):
    c = config.load(None)
    c["station"].update(callsign="N0CALL", operator="N1CALL", qth="Rockall", locator="IO37do", frequency="145.660")
    c["logbook"]["file"] = str(tmp_path / "{callsign}.adi")
    return c


# ---------------- weather ----------------
def test_locator_to_latlon():
    assert weather.locator_to_latlon("IO37do") == (57.604, -13.708)   # Rockall
    assert weather.locator_to_latlon("IO86") == (56.5, -3.0)
    assert weather.locator_to_latlon("nonsense") is None


def test_weather_words():
    assert (weather.beaufort(16.7), weather.beaufort(17), weather.beaufort(70)) == (4, 5, 12)
    assert weather.compass(222) == "south-westerly" and weather.compass(355) == "northerly"
    assert weather.sea_state(4.32) == "very rough" and weather.visibility(43360) == "good"
    assert weather.tendency(-1.2) == "falling slowly" and weather.tendency(0.05) == "steady"


def test_weather_report_from_open_meteo(tmp_path):
    def get(url, params):
        if "marine" in url:
            return {"current": {"wave_height": 4.32, "sea_surface_temperature": 11.2}}
        assert (params["latitude"], params["longitude"]) == (57.604, -13.708)
        return {"current": {"time": "2026-10-23T21:45", "temperature_2m": 9.4, "wind_speed_10m": 16.7,
                            "wind_direction_10m": 222, "wind_gusts_10m": 21.0, "pressure_msl": 1001.4,
                            "weather_code": 51, "visibility": 43360},
                "hourly": {"pressure_msl": [1002.6, 1002.0, 1001.4]}}
    w = weather.Weather(cfg(tmp_path), get=get)
    assert w.report() == ("Weather at Rockall, 2145 UTC: wind south-westerly force 4, 17 knots, gusting 21 knots. "
                          "Light drizzle. Visibility good. Sea very rough, waves around 4.3 metres. Air 9 degrees, "
                          "sea 11 degrees. Pressure 1001 hectopascals, falling slowly.")


def test_weather_offline_is_none(tmp_path):
    def get(url, params):
        raise OSError("no internet")
    assert weather.Weather(cfg(tmp_path), get=get).report() is None


# ---------------- HamQTH ----------------
SESSION = b'<HamQTH xmlns="https://www.hamqth.com"><session><session_id>abc</session_id></session></HamQTH>'
FOUND = (b'<HamQTH xmlns="https://www.hamqth.com"><search><callsign>n1call</callsign><nick>Demis</nick>'
         b'<qth>Crieff</qth><country>Scotland</country><grid>IO86fi</grid></search></HamQTH>')
MISSING = b'<HamQTH xmlns="https://www.hamqth.com"><session><error>Callsign not found</error></session></HamQTH>'


def test_hamqth_lookup_logs_in_once_and_caches():
    calls = []

    def fetch(params):
        calls.append(params)
        if "u" in params:
            return SESSION
        return FOUND if params["callsign"] == "N1CALL" else MISSING
    h = hamqth.HamQTH("me", "pw", fetch=fetch)
    info = h.lookup("n1call")
    assert info == {"callsign": "n1call", "name": "Demis", "qth": "Crieff", "country": "Scotland", "grid": "IO86fi"}
    assert h.lookup("N1CALL") == info and h.lookup("G0NONE") is None
    assert [("u" in c) for c in calls] == [True, False, False]       # one login, two searches, one cached
    assert hamqth.describe(info, "N1CALL") == "HamQTH lookup for N1CALL: name Demis, QTH Crieff, Scotland, locator IO86fi."


def test_hamqth_without_an_account_does_nothing():
    h = hamqth.HamQTH("", "", fetch=lambda p: pytest.fail("no lookups without an account"))
    assert not h.enabled and h.lookup("N1CALL") is None


# ---------------- logbook ----------------
def test_adif_record_and_update(tmp_path):
    clock = Clock()
    lb = logbook.Logbook(cfg(tmp_path), clock=clock)
    lb.write("N1CALL", {"name": "Demis", "rst_sent": "59"})
    clock.t += 120
    lb.write("N1CALL", {"name": "Demis", "qth": "Crieff", "rst_sent": "59", "rst_rcvd": "57"}, update=True)
    text = open(lb.path, encoding="utf-8").read()
    assert text.startswith("radio-agent logbook") and text.count("<EOR>") == 1
    assert "<CALL:6>N1CALL" in text and "<QTH:6>Crieff" in text and "<BAND:2>2m <FREQ:7>145.660 <MODE:2>FM" in text
    assert "<TIME_ON:4>0346" in text                                  # the update keeps the start time
    lb._last = None
    lb.write("G4ABC", {"name": "Al"})
    assert open(lb.path, encoding="utf-8").read().count("<EOR>") == 2


# ---------------- running a contact ----------------
class FakeLookup:
    def lookup(self, call):
        return {"name": "Demis", "qth": "Crieff", "grid": "IO86fi"} if call == "N1CALL" else None


class FakeWeather:
    def report(self):
        return "Weather at Rockall: wind south-westerly force 4."


def desk(tmp_path, bus=None):
    c = cfg(tmp_path)
    clock = Clock()
    q = qso.QSO(c, clock=clock)
    return contact.Contacts(c, q, bus, weather=FakeWeather(), lookup=FakeLookup(),
                            logbook=logbook.Logbook(c, clock=clock), clock=clock), q, clock


def test_answering_our_cq_is_a_contact_we_lead(tmp_path):
    d, q, clock = desk(tmp_path)
    q.last_cq = clock() - 30
    notes = d.notes("N0CALL this is November One Charlie Alpha Lima Lima")
    assert d.mode == contact.CONTACT and d.partner == "N1CALL"
    assert "answering your CQ" in notes and "Don't offer to help" in notes
    assert "Weather at Rockall" in notes and "HamQTH lookup for N1CALL: name Demis" in notes
    q.frame(["Hello."])                                              # the reply goes out: the QSO is on
    later = d.notes("my name is Demis, QTH Crieff, you're 5 and 7")
    assert "you lead it" in later and "LOG:" in later and "Weather" not in later


def test_their_cq_puts_the_operator_in_control(tmp_path):
    d, q, clock = desk(tmp_path)
    notes = d.notes("CQ CQ CQ this is N1CALL calling CQ")
    assert d.mode == contact.THEIR_CQ and "they're in control" in notes


def test_a_plain_call_gets_no_contact_script(tmp_path):
    d, q, clock = desk(tmp_path)
    assert d.notes("Stephen, what files are in the project?") == ""
    assert "Weather at Rockall" in d.notes("what's the weather like out there?")


def test_for_us_without_the_wake_word(tmp_path):
    d, q, clock = desk(tmp_path)
    assert d.for_us("November Zero Charlie Alpha Lima Lima this is G4ABC")
    assert not d.for_us("anyone about this evening")
    q.last_cq = clock() - 20
    assert d.for_us("anyone about this evening")                     # just after our CQ: someone answering it


def test_log_line_writes_the_logbook(tmp_path):
    seen = []
    bus = Bus()
    bus.subscribe(lambda n, data: n == "qso_logged" and seen.append(data))
    d, q, clock = desk(tmp_path, bus)
    q.last_cq = clock() - 30
    d.notes("N0CALL this is N1CALL")
    d.log({"name": "Demis", "qth": "Crieff", "rst_sent": "59", "rst_rcvd": "57"})
    assert seen[-1]["call"] == "N1CALL" and seen[-1]["gridsquare"] == "IO86fi"   # grid from HamQTH
    assert d.log(None, closing=True) is None                         # already logged: nothing new
    assert "<NAME:5>Demis" in open(d.logbook.path, encoding="utf-8").read()


def test_contact_ending_unlogged_still_goes_in(tmp_path):
    d, q, clock = desk(tmp_path)
    q.last_cq = clock() - 30
    d.notes("N0CALL this is N1CALL")
    d.log(None, closing=True)
    assert "details incomplete" in open(d.logbook.path, encoding="utf-8").read()


def test_station_passes_notes_and_logs(tmp_path):
    class Contacter(EchoAgent):
        def ask(self, text):
            self.asked = text
            return "SAY: Thanks Demis, Crieff, five seven. Logged.\nLOG: name=Demis; qth=Crieff; rst_sent=59; rst_rcvd=57"
    d, q, clock = desk(tmp_path)
    q.last_cq = clock() - 30
    agent = Contacter()
    st = Station(cfg(tmp_path), agent, contacts=d)                   # wake word required: our call wakes it
    assert st.handle("N0CALL this is N1CALL, name Demis, QTH Crieff, you're 5 7") == \
        "Thanks Demis, Crieff, five seven. Logged."
    assert agent.asked.startswith("[Station notes") and "answering your CQ" in agent.asked
    assert d.logged["name"] == "Demis"


def test_parse_log_line():
    r = speech.parse_agent_output("SAY: Logged.\nLOG: name=Demis; qth=Crieff; rst_sent=59")
    assert r.log == {"name": "Demis", "qth": "Crieff", "rst_sent": "59"} and r.say == "Logged."


def test_env_file_is_read(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("HAMQTH_USER=me\n# comment\nHAMQTH_PASS='secret'\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("{}", encoding="utf-8")
    monkeypatch.delenv("HAMQTH_USER", raising=False)
    monkeypatch.delenv("HAMQTH_PASS", raising=False)
    config.load(str(tmp_path / "config.yaml"))
    assert os.environ["HAMQTH_USER"] == "me" and os.environ["HAMQTH_PASS"] == "secret"
