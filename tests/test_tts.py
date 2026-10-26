import os

import pytest

from radio_agent import config, speech, tts


# ---------------- pronunciation lexicon ----------------
def test_lexicon_defaults():
    say = tts.Lexicon()
    assert say("Saved the WAV, SSTV on 145.500 MHz, QSO at 1200 UTC") == \
        "Saved the wave, S S T V on 145.500 megahertz, Q S O at 1200 U T C"
    assert say("QRM is 3 dB up, CTCSS 94.8, 7 kHz off") == "Q R M is 3 D B up, C T C S S 94.8, 7 kilohertz off"
    assert say("Rockall, Crieff and Perthshire") == "Rock all, Creef and Perth-sure"
    assert say("Read the README, run npm test.") == "Read the read me, run N P M test."
    assert say("73 and good DX") == "seventy three and good DX"


def test_lexicon_whole_words_only():
    say = tts.Lexicon()
    assert say("a wave and a wavelength") == "a wave and a wavelength"
    assert say("wavs WAVE rewav") == "wavs WAVE rewav"
    assert say("SSTVs QSOs npmjs") == "SSTVs QSOs npmjs"
    assert say("173 or 730 or 145.73 or 73.5") == "173 or 730 or 145.73 or 73.5"


def test_lexicon_ignores_case():
    say = tts.Lexicon()
    assert say("wav Wav WAV") == "wave wave wave"
    assert say("mhz MHZ") == "megahertz megahertz"
    assert say("DIV") == "div"


def test_lexicon_punctuation_around_words():
    say = tts.Lexicon()
    assert say("(WAV) \"QSO\", SSTV's 73!") == "(wave) \"Q S O\", S S T V's seventy three!"


def test_lexicon_config_adds_and_overrides():
    say = tts.Lexicon({"Strathearn": "Strath-ern", "rockall": "Rokall", "73": ""})
    assert say("Strathearn via Rockall, 73") == "Strath-ern via Rokall, 73"
    assert say("strathearns") == "strathearns"


def test_lexicon_non_string_keys_from_yaml():
    assert tts.Lexicon({88: "eighty eight"})("88 to all") == "eighty eight to all"


def test_make_applies_lexicon_to_every_backend(monkeypatch):
    heard = []

    class Fake:
        def synth(self, text):
            heard.append(text)
            return None

    monkeypatch.setitem(tts.BACKENDS, "piper", lambda c: Fake())
    c = config.load(None)
    c["radio"]["mode"] = "soundcard"
    c["tts"]["pronunciations"] = {"Strathearn": "Strath-ern"}
    tts.make(c).synth("SSTV from Strathearn")
    assert heard == ["S S T V from Strath-ern"]


# ---------------- DECtalk ----------------
def test_dectalk_clean_strips_commands():
    assert tts.dectalk_clean("[:np] hi {x}\x0bthere\n") == "(:np) hi (x) there "


def test_dectalk_missing_falls_back(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tts.shutil, "which", lambda name: None)
    monkeypatch.setitem(tts.BACKENDS, "sapi", lambda c: "sapi voice")
    c = config.load(None)
    c["radio"]["mode"] = "soundcard"
    c["tts"].update(backend="dectalk", dectalk_path=str(tmp_path / "nowhere"), fallback="sapi")
    assert tts._make(c) == "sapi voice"
    assert capsys.readouterr().err.count("WARNING") == 1

    c["tts"]["fallback"] = ""
    with pytest.raises(FileNotFoundError):
        tts._make(c)


def test_dectalk_say_cli(monkeypatch, tmp_path):
    say = tmp_path / tts.DECtalk.SAY
    say.write_bytes(b"")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = cmd[cmd.index("-w" if os.name == "nt" else "-fo") + 1]
        import wave
        with wave.open(out, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(11025), w.writeframes(b"\0\0" * 10)

    monkeypatch.setattr(tts.subprocess, "run", fake_run)
    c = config.load(None)
    c["tts"].update(dectalk_path=str(tmp_path), dectalk_voice="Betty", dectalk_rate=200)
    audio, sr = tts.DECtalk(c).synth("hello [world]")
    assert sr == 11025 and len(audio) == 10
    assert calls[0][0] == str(say.resolve())
    assert calls[0][-1] == "[:phoneme on] [:nb] [:rate 200] hello (world)"
    out = calls[0][calls[0].index("-w" if os.name == "nt" else "-fo") + 1]
    assert not os.path.exists(out)  # temp WAV is deleted


# ---------------- NATO phonetics ----------------
def mark(call):
    return speech.phonetic(call, mark=True)


def test_phonetic_id_goes_through_the_table():
    say = tts.Lexicon()
    assert say(f"This is {mark('N0CALL')}.") == "This is no-vem-ber zeer-oh char-lee al-fah lee-mah lee-mah."
    assert say(mark("N0CALL")) == "no-vem-ber zeer-oh char-lee al-fah lee-mah lee-mah"


def test_phonetic_table_only_in_the_stations_phonetics():
    say = tts.Lexicon()
    plain = "Mike booked a hotel in Lima for two nights, near the river delta."
    assert say(plain) == plain                                       # ordinary words are spoken normally
    assert say(f"{plain} This is {mark('N0CALL')}.") == f"{plain} This is no-vem-ber zeer-oh char-lee al-fah lee-mah lee-mah."
    assert say(f"{mark('N0CALL')}, 73 from Crieff") == "no-vem-ber zeer-oh char-lee al-fah lee-mah lee-mah, seventy three from Creef"


def test_phonetic_table_only_takes_whole_words():
    assert tts.Lexicon()(f"{tts.speech.PHON_OPEN}Deltawing Delta{tts.speech.PHON_CLOSE}") == "Deltawing dell-tah"


def test_dectalk_column_is_phonemes_or_text():
    say = tts.Lexicon(phonemes=True)
    # X-ray: [phonemes]; Delta: plain "Dell-tah", which Whisper hears as Delta where [d'ehltax] was "Delga"
    assert tts.dectalk_clean(say(f"{mark('XD')} [ok] Delta")) == "['ehksr`ey] Dell-tah (ok) Delta"
    assert tts.dectalk_clean(say("WAV")) == "wave"                   # not a phonetic: the general table


def test_phonetic_overrides_from_config():
    plain = tts.Lexicon({"Delta": "dell-tuh"})
    assert plain(mark("D")) == "dell-tuh"
    assert plain("Delta") == "Delta"                                 # a phonetic entry: only in phonetics
    dt = tts.Lexicon({"Delta": "dell-tuh", "Sierra": {"say": "see-err-ah", "phonemes": "siy'ehrax"},
                      "Kilo": {"say": "kee-loh", "dectalk": "Key-low"}}, phonemes=True)
    assert tts.dectalk_clean(dt(mark("DSK"))) == "dell-tuh [siy'ehrax] Key-low"
    assert tts.Lexicon({"Sierra": {"say": "see-err-ah", "phonemes": "x"}})(mark("S")) == "see-err-ah"


def test_make_gives_dectalk_phonemes_and_others_respellings(monkeypatch):
    heard = []

    class Fake:
        def __init__(self, phonemes):
            self.phonemes = phonemes

        def synth(self, text):
            heard.append(text)

    c = config.load(None)
    c["radio"]["mode"] = "soundcard"
    monkeypatch.setitem(tts.BACKENDS, "piper", lambda c: Fake(False))
    tts.make(c).synth(mark("X"))
    monkeypatch.setitem(tts.BACKENDS, "piper", lambda c: Fake(True))
    tts.make(c).synth(mark("X"))
    assert heard == ["ecks-ray", f"{tts.PH_OPEN}'ehksr`ey{tts.PH_CLOSE}"]


def test_heard_chars_reads_whisper_output():
    from radio_agent import voicecheck
    assert voicecheck.heard_chars("November 0, Charlie Alpha Lima Lima.") == "N0CALL"
    assert voicecheck.heard_chars("November oh Charlie, alfa, Lima lima") == "N0CALL"
    assert voicecheck.heard_chars("DELGA") == "?"

