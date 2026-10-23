"""DMR front end tested against a fake Analog_Bridge on localhost UDP."""
import socket
import threading
import time

import numpy as np

from radio_agent import config, dmr
from radio_agent.__main__ import caller_allowed


def _cfg(listen_port, bridge_port):
    c = config.load(None)
    c["radio"]["mode"] = "dmr"
    c["dmr"].update(listen_port=listen_port, bridge_port=bridge_port, end_of_over_timeout=0.3, tx_tail_ms=0)
    return c


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _tlv(dmr_id, tg, call):
    body = dmr_id.to_bytes(3, "big") + (0).to_bytes(4, "big") + tg.to_bytes(3, "big") + bytes([2, 1]) + call.encode() + b"\x00"
    return bytes([dmr.TLV_SET_INFO, len(body)]) + body


def test_pack_roundtrip():
    p = dmr.unpack(dmr.pack(7, True, payload=b"\x01\x00" * 160, talkgroup=9))
    assert p["seq"] == 7 and p["keyup"] and p["tg"] == 9 and len(p["payload"]) == 320


def test_parse_caller_tlv_and_text():
    assert dmr.parse_caller(dmr.USRP_TLV, _tlv(2351234, 9, "N1CALL")) == {"dmr_id": 2351234, "talkgroup": 9, "callsign": "N1CALL"}
    assert dmr.parse_caller(dmr.USRP_TEXT, b"INFO: MM0ABC on TG 9\x00")["callsign"] == "MM0ABC"


def test_listen_collects_an_over_with_caller():
    lp, bp = _free_port(), _free_port()
    radio = dmr.DMRRadio(_cfg(lp, bp))
    tone = (0.5 * np.sin(2 * np.pi * 440 * np.arange(8000) / 8000) * 32767).astype("<i2")

    def bridge():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        time.sleep(0.05)
        s.sendto(dmr.pack(1, False, dmr.USRP_TLV, _tlv(2351234, 9, "N1CALL")), ("127.0.0.1", lp))
        for i in range(0, len(tone), 160):
            s.sendto(dmr.pack(i, True, payload=tone[i:i + 160].tobytes()), ("127.0.0.1", lp))
        s.sendto(dmr.pack(999, False), ("127.0.0.1", lp))

    threading.Thread(target=bridge, daemon=True).start()
    heard = radio.listen()
    radio.close()
    assert heard.sample_rate == 8000
    assert abs(len(heard.audio) - 8000) <= 160
    assert heard.meta["callsign"] == "N1CALL"


def test_listen_ends_on_timeout_without_keyup_zero():
    lp, bp = _free_port(), _free_port()
    radio = dmr.DMRRadio(_cfg(lp, bp))

    def bridge():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        time.sleep(0.05)
        for i in range(10):
            s.sendto(dmr.pack(i, True, payload=b"\x00\x00" * 160), ("127.0.0.1", lp))

    threading.Thread(target=bridge, daemon=True).start()
    heard = radio.listen()
    radio.close()
    assert len(heard.audio) == 1600


def test_transmit_sends_paced_frames_then_unkey():
    lp, bp = _free_port(), _free_port()
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", bp))
    sink.settimeout(2)
    radio = dmr.DMRRadio(_cfg(lp, bp))
    audio = np.zeros(16000, np.float32)  # 1 s at 16 kHz -> 50 frames at 8 kHz
    t0 = time.time()
    radio.transmit_audio(audio, 16000)
    elapsed = time.time() - t0
    packets = []
    while True:
        try:
            packets.append(dmr.unpack(sink.recv(4096)))
        except socket.timeout:
            break
        if not packets[-1]["keyup"]:
            break
    radio.close()
    sink.close()
    assert sum(p["keyup"] for p in packets) == 50
    assert not packets[-1]["keyup"]
    assert all(len(p["payload"]) == 320 for p in packets if p["keyup"])
    assert elapsed > 0.9  # real-time pacing


def test_allow_list():
    c = config.load(None)
    c["radio"]["mode"] = "dmr"
    assert caller_allowed(c, {})                       # empty list = anyone
    c["dmr"]["allowed_callsigns"] = ["N1CALL"]
    assert caller_allowed(c, {"callsign": "n1call"})
    assert not caller_allowed(c, {"callsign": "MM0ABC"})
    assert not caller_allowed(c, {})
