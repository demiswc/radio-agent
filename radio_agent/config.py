"""Config loading: defaults merged with config.yaml."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

DEFAULTS: dict = {
    "station": {"callsign": "N0CALL", "id_every_over": True, "id_interval_seconds": 600,
                "operator": "",  # operator = whoever is calling in
                # shown on the screens only: who the agent plays, where, and on what frequency
                "name": "", "qth": "", "locator": "", "frequency": ""},
    "radio": {
        "mode": "text",
        "reply": "radio",          # radio | speaker | screen
        "input_device": None,
        "output_device": None,
        "sample_rate": 16000,
        "squelch_threshold": 0.02,
        "open_ms": 120,
        "hang_ms": 900,
        "max_listen_seconds": 60,
        "ptt": {
            "type": "vox",
            "port": "/dev/ttyACM0",
            "assert_dtr": True,
            "assert_rts": False,
            "tx_delay_ms": 300,
            "tx_tail_ms": 200,
        },
        "vox_lead_in_ms": 250,
        "max_tx_seconds": 45,
        "roger_beep_hz": 1000,     # tone your handheld sends on PTT release; 0 = don't listen for it
    },
    "dmr": {
        "listen_host": "127.0.0.1",
        "listen_port": 32001,      # Analog_Bridge [USRP] txPort – it sends to us here
        "bridge_host": "127.0.0.1",
        "bridge_port": 34001,      # Analog_Bridge [USRP] rxPort – we send replies here
        "talkgroup": 0,
        "end_of_over_timeout": 1.0,
        "tx_tail_ms": 300,
        "allowed_callsigns": [],   # e.g. ["N0CALL"]; empty = anyone
        "allowed_ids": [],         # DMR IDs; empty = anyone
    },
    "rtlsdr": {
        "frequency": "145.425M",
        "squelch": 100,            # rtl_fm -l squelch level (raise if it opens on noise). Needed: FM noise is loud
        "gain": "auto",            # or a number in dB, e.g. 40
        "ppm": 0,                  # frequency correction for your dongle
        "device_index": 0,
        "rtl_fm": "rtl_fm",
    },
    "listen": {
        "wake_words": ["claude", "agent", "computer"],
        "require_wake_word": True,     # false = treat every over as for the agent
        "conversation_window_seconds": 180,
        "pin": None,
        "lock_after_idle_seconds": 900,
        "wait_for_over": False,        # keep collecting transmissions until you say "over"
        "over_timeout_seconds": 6,     # ...or until nobody keys up for this long
        "standby_after_seconds": 3,    # say "stand by" only if the agent takes longer than this
        "send_on_roger_beep": True,    # roger beep heard = you let go, so send straight away
    },
    "stt": {"backend": "faster-whisper", "device": "cpu", "model": "base.en", "openai_model": "whisper-1", "hint": ""},
    "agent": {
        "backend": "claude-code",
        "workdir": "~/radio-sandbox",
        "allowed_tools": ["Read", "Edit", "Write", "Glob", "Grep"],
        "model": None,
        "ollama_url": "http://localhost:11434",
        "timeout_seconds": 600,
        "persona": "",                 # a character for the agent to play, added to its instructions
    },
    "tts": {"sapi_voice": "", "sapi_rate": 1, "backend": "piper", "piper_model": "~/piper/en_GB-alan-medium.onnx", "openai_voice": "onyx"},
    "sstv": {
        "seconds": 5,              # how long the picture takes to paint on the demo page
        "play_audio": False,       # true = play real SSTV audio through the reply output (decodable, takes the mode's full time)
        "mode": "robot36",         # robot36 (36 s) | martin1 (115 s): the audio mode when play_audio is on
        "folder": "sstv",          # your own photos to send (jpg/png); none there = the built-in picture
        "picture": "testcard",     # built-in picture when there are no photos: testcard | rockall
    },
    "cq": {
        "enabled": False,
        "interval_seconds": [60, 120],   # random gap between calls
        "first_after_seconds": 20,       # first call this long after start-up
        "quiet_after_seconds": 45,       # after a QSO, wait this long with nothing heard before calling again
        "on_air": False,                 # allow automatic calls when reply is "radio" (unattended TX: check your licence)
        # spoken; {phonetic} {callsign} {name} {qth} {locator} are filled in. "C Q" so every voice says the letters
        "text": "C Q, C Q, C Q. This is {phonetic}, {phonetic}, calling C Q from {qth}, and standing by.",
        "display": "CQ CQ CQ {callsign} {locator}",  # what the screens show, WSJT-X style
    },
    "sidecar": {"enabled": True, "host": "0.0.0.0", "port": 8073, "token": "change-me", "open_demo": False,
                "title": "RADIO-AGENT"},   # name shown on the /demo page
}


def dial_mhz(cfg: dict) -> str:
    """Dial frequency for the screens, e.g. "145.660": station.frequency, else rtlsdr.frequency, else 145.425."""
    raw = str(cfg["station"].get("frequency") or cfg.get("rtlsdr", {}).get("frequency") or "145.425")
    raw = raw.strip().upper().rstrip("M").strip()
    try:
        return f"{float(raw):.3f}"
    except ValueError:
        return "145.425"


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _read(path: str) -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"Config not found: {p}")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def load(path: str | None, profiles: list[str] | None = None) -> dict:
    """Defaults, then your config.yaml, then any profiles (e.g. profiles/stephen-rockall.yaml) on top.

    A profile is a partial config – a character, a look – kept apart from your station's own settings.
    """
    cfg = _merge(DEFAULTS, _read(path) if path else {})
    for prof in profiles or []:
        cfg = _merge(cfg, _read(prof))
    cfg["agent"]["workdir"] = os.path.expanduser(cfg["agent"]["workdir"])
    cfg["tts"]["piper_model"] = os.path.expanduser(cfg["tts"]["piper_model"])
    return cfg
