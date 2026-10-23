"""DMR front end via DVSwitch.

Your DMR handheld talks to the MMDVM hotspot; DVSwitch's Analog_Bridge decodes the AMBE
voice to plain PCM and exchanges it with us as USRP packets over UDP on localhost.
No second radio, no audio cable.

USRP packet = 32-byte header (big-endian) + payload
    eye[4]="USRP" | seq | memory | keyup | talkgroup | type | mpxid | reserved
type 0 = voice: 160 x int16 little-endian samples, 8 kHz mono (20 ms)
type 2 = text, type 4 = TLV metadata (who's talking)
keyup=0 marks the end of an over.
"""
from __future__ import annotations

import re
import socket
import struct
import time

import numpy as np

from . import dtmf
from .radio import Heard, resample

HDR = struct.Struct(">4sIIIIIII")
USRP_VOICE, USRP_DTMF, USRP_TEXT, USRP_PING, USRP_TLV = 0, 1, 2, 3, 4
TLV_SET_INFO = 8
SR = 8000
FRAME = 160  # samples per 20 ms frame


def pack(seq: int, keyup: bool, ptype: int = USRP_VOICE, payload: bytes = b"", talkgroup: int = 0) -> bytes:
    return HDR.pack(b"USRP", seq, 0, int(keyup), talkgroup, ptype, 0, 0) + payload


def unpack(data: bytes):
    if len(data) < HDR.size or data[:4] != b"USRP":
        return None
    eye, seq, mem, keyup, tg, ptype, mpx, res = HDR.unpack_from(data)
    return {"seq": seq, "keyup": bool(keyup), "tg": tg, "type": ptype, "payload": data[HDR.size:]}


_CALL_RE = re.compile(r"\b([A-Z]{1,2}[0-9][A-Z0-9]{0,1}[0-9]?[A-Z]{1,4})\b")


def parse_caller(ptype: int, payload: bytes) -> dict:
    """Best-effort: pull the caller's DMR ID and callsign out of Analog_Bridge metadata."""
    info: dict = {}
    try:
        if ptype == USRP_TLV and len(payload) >= 2 and payload[0] == TLV_SET_INFO:
            body = payload[2 : 2 + payload[1]]
            info["dmr_id"] = int.from_bytes(body[0:3], "big")
            info["talkgroup"] = int.from_bytes(body[7:10], "big")
            call = body[12:].split(b"\x00")[0].decode(errors="ignore").strip()
            if call:
                info["callsign"] = call.upper()
        elif ptype == USRP_TEXT:
            text = payload.split(b"\x00")[0].decode(errors="ignore").upper()
            m = _CALL_RE.search(text)
            if m:
                info["callsign"] = m.group(1)
    except Exception:
        pass
    return info


class DMRRadio:
    """Same interface as TextRadio / SoundcardRadio."""

    sample_rate = SR

    def __init__(self, cfg: dict):
        d = cfg["dmr"]
        self.d = d
        self.max_tx = int(cfg["radio"]["max_tx_seconds"] * SR)
        self.max_listen = float(cfg["radio"]["max_listen_seconds"])
        self.talkgroup = int(d.get("talkgroup") or 0)
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.rx.bind((d["listen_host"], int(d["listen_port"])))
        self.tx_addr = (d["bridge_host"], int(d["bridge_port"]))
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.seq = 0
        self.end_timeout = float(d.get("end_of_over_timeout", 1.0))

    # ---------- receive ----------
    def listen(self, timeout: float | None = None) -> Heard | None:
        frames: list[np.ndarray] = []
        meta: dict = {}
        started = None
        self.rx.settimeout(None)
        while True:
            try:
                data, _ = self.rx.recvfrom(4096)
            except socket.timeout:
                if frames:
                    break  # bridge stopped sending without a keyup=0 – treat as end of over
                continue
            p = unpack(data)
            if p is None:
                continue
            if p["type"] in (USRP_TLV, USRP_TEXT):
                meta.update(parse_caller(p["type"], p["payload"]))
                continue
            if p["type"] != USRP_VOICE:
                continue
            if p["keyup"]:
                if started is None:
                    started = time.time()
                    self.rx.settimeout(self.end_timeout)
                pcm = np.frombuffer(p["payload"][: FRAME * 2], dtype="<i2").astype(np.float32) / 32768
                frames.append(pcm)
                if time.time() - started > self.max_listen:
                    break
            elif frames:
                break  # keyup=0: end of over
        self.rx.settimeout(None)
        audio = np.concatenate(frames) if frames else np.zeros(0, np.float32)
        return Heard(audio=audio, dtmf=dtmf.decode(audio, SR) if len(audio) else "", sample_rate=SR, meta=meta)

    # ---------- transmit ----------
    def transmit_text(self, text: str) -> None:
        raise RuntimeError("DMRRadio needs audio – configure a TTS backend")

    def transmit_audio(self, audio: np.ndarray, sr: int) -> None:
        audio = resample(audio, sr, SR)[: self.max_tx]
        pad = (-len(audio)) % FRAME
        pcm = (np.clip(np.concatenate([audio, np.zeros(pad, np.float32)]), -1, 1) * 32767).astype("<i2")
        t0 = time.time()
        for i in range(0, len(pcm), FRAME):
            self.seq += 1
            self.tx.sendto(pack(self.seq, True, payload=pcm[i : i + FRAME].tobytes(), talkgroup=self.talkgroup), self.tx_addr)
            # pace in real time so the bridge's buffer doesn't overflow
            delay = t0 + (i // FRAME + 1) * 0.02 - time.time()
            if delay > 0:
                time.sleep(delay)
        self.seq += 1
        self.tx.sendto(pack(self.seq, False, talkgroup=self.talkgroup), self.tx_addr)
        time.sleep(float(self.d.get("tx_tail_ms", 300)) / 1000)

    def close(self) -> None:
        self.rx.close()
        self.tx.close()
