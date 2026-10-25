import sys
import types

import pytest

from radio_agent import radio


class FakeSerial:
    """Records what the PTT lines were whenever the port was open."""

    def __init__(self, port=None):
        self.port = port
        self.is_open = False
        self._rts = self._dtr = True  # pyserial's defaults: both asserted
        self.events = []
        if port is not None:  # Serial(port) opens immediately, like pyserial
            self.open()

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, v):
        self._rts = v
        self.events.append(("rts", v, self.is_open))

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, v):
        self._dtr = v
        self.events.append(("dtr", v, self.is_open))

    def open(self):
        self.is_open = True
        self.events.append(("open", self.port, self._rts, self._dtr))

    def close(self):
        self.is_open = False
        self.events.append(("close",))


@pytest.fixture
def fake_serial(monkeypatch):
    made = []

    def factory(*a, **k):
        s = FakeSerial(*a, **k)
        made.append(s)
        return s

    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=factory))
    return made


def test_serial_ptt_opens_with_rts_unkeyed(fake_serial):
    ptt = radio._SerialPTT("COM4", assert_dtr=False, assert_rts=True)
    (ser,) = fake_serial
    opens = [e for e in ser.events if e[0] == "open"]
    assert opens == [("open", "COM4", False, True)]  # RTS (=TX) already off when the port opened
    assert ser.events[-1][0] == "open"               # lines set before opening, nothing after

    ptt.on()
    assert ser.rts is True
    ptt.off()
    assert ser.rts is False
    ptt.close()
    assert ser.events[-1] == ("close",) and ser.rts is False


def test_serial_ptt_never_keys_while_opening_dtr_wiring(fake_serial):
    radio._SerialPTT("COM9", assert_dtr=True, assert_rts=False)
    (ser,) = fake_serial
    assert [e for e in ser.events if e[0] == "open"] == [("open", "COM9", True, False)]


def _tone(amp):
    import numpy as np

    t = np.arange(16000) / 16000
    return (amp * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def test_tx_level_normalises_to_peak_then_applies_gain():
    import numpy as np

    for amp in (0.05, 0.3, 0.94):  # quiet and loud voices come out the same
        assert np.abs(radio.tx_level(_tone(amp), 1.0)).max() == pytest.approx(0.9, abs=1e-3)
        assert np.abs(radio.tx_level(_tone(amp), 0.5)).max() == pytest.approx(0.45, abs=1e-3)
    quiet = _tone(0.3)
    assert np.allclose(radio.tx_level(quiet, 0.5), quiet * 1.5, atol=1e-5)  # x3 to peak 0.9, then x0.5


def test_tx_gain_above_one_never_clips():
    import numpy as np

    loud = radio.tx_level(_tone(0.94), 2.0)
    assert np.abs(loud).max() < 1.0
    rms = lambda a: float(np.sqrt(np.mean(a * a)))
    assert rms(loud) > rms(radio.tx_level(_tone(0.94), 1.0)) * 1.1


def test_tx_level_leaves_silence_alone():
    import numpy as np

    silent = np.zeros(100, np.float32)
    assert not radio.tx_level(silent, 0.5).any()
    assert len(radio.tx_level(np.zeros(0, np.float32), 0.5)) == 0
