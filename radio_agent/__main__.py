"""radio-agent – talk to an AI coding agent over amateur radio.

  radio-agent run [-c config.yaml]       main loop
  radio-agent devices                    list sound cards
  radio-agent calibrate [-c ...]         show live receive levels to set squelch_threshold
  radio-agent say "text" [-c ...]        transmit a test phrase (with ID)
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from contextlib import contextmanager

from . import agents, config, cq, radio, speech, sstv, stt, tts
from .events import Bus, log, setup_logging
from .sidecar import Log, over_stats, serve
from .station import Station


class Transmitter:
    """Speaks replies: splits long ones into several overs, ends each one properly.

    The callsign goes out on the first reply of a QSO, then only every id_interval_seconds, and
    always when signing off ("…, N0CALL, clear") – not after every single over.
    """

    def __init__(self, cfg: dict, rad, voice, log: Log | None = None):
        self.cfg, self.radio, self.voice = cfg, rad, voice
        self.log = log or Log()
        self.air = threading.RLock()  # one thing on air at a time: speech waits for a playing picture
        self._cq: str | None = None   # set while calling CQ: what the screens show for it
        self.phonetic = speech.phonetic(cfg["station"]["callsign"])
        self.ident = f"This is {self.phonetic}."
        self.last_id = -1e9           # when the callsign last went out
        self.last_reply = -1e9        # when we last answered someone (CQ calls don't count)
        # ~14 characters per second of speech; leave room for the ID
        self.max_chars = max(80, int(cfg["radio"]["max_tx_seconds"] * 14) - len(self.ident) - 10)

    def send(self, text: str, final_over: bool = True, signoff: bool = False) -> None:
        """final_over=False is the "Roger, stand by" while the agent is still working.
        signoff=True: the QSO is ending, so finish with the callsign and "clear"."""
        with self.air:
            self._send(text, final_over, signoff)

    def send_cq(self, spoken: str, display: str) -> bool:
        """Call CQ – only if nothing else is on air right now; never queue up behind a reply."""
        if not self.air.acquire(blocking=False):
            return False
        try:
            self._cq = display
            self._send(spoken, final_over=True)
            self.last_id = time.time()  # a CQ call carries the callsign
        finally:
            self._cq = None
            self.air.release()
        return True

    def _id_tail(self, final_over: bool, signoff: bool) -> str:
        """How this over ends: a sign-off with the callsign, an occasional ID, or just "over"."""
        st = self.cfg["station"]
        on_air = self.cfg["radio"].get("reply", "radio") == "radio"
        if not final_over:  # "Roger, stand by" – the real answer follows
            return ""
        now = time.time()
        new_qso = now - self.last_reply > float(self.cfg["listen"]["conversation_window_seconds"])
        self.last_reply = now
        if signoff:
            self.last_id = now
            return f" This is {self.phonetic}, {st['callsign']}, clear."
        if st.get("id_every_over") or new_qso or now - self.last_id >= float(st.get("id_interval_seconds", 600)):
            self.last_id = now
            return f" This is {self.phonetic}, over." if on_air else f" This is {self.phonetic}."
        return " Over." if on_air else ""

    def _send(self, text: str, final_over: bool, signoff: bool = False) -> None:
        reply = self.cfg["radio"].get("reply", "radio")
        ack = not final_over
        tail = "" if self._cq else self._id_tail(final_over, signoff)  # a CQ call carries its own ID
        if reply == "screen" or (reply == "speaker" and self.cfg["radio"]["mode"] == "text"):
            # Receive-only station: nothing goes on air, so no chunking
            text = (text + tail).strip()
            log.info(f"🖥  agent> {text}")
            with self._on_air(text, None, ack):
                pass
            return
        if reply == "speaker":
            text = (text + tail).strip()
            synth = self.voice.synth(text)
            log.info(f"🔈 agent> {text}")
            with self._on_air(text, synth, ack):
                if synth is not None:
                    import sounddevice as sd
                    audio, sr = synth
                    sd.play(audio, sr, device=self.cfg["radio"]["output_device"], blocking=True)
            return
        chunks = speech.chunk_sentences(text, self.max_chars)
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            line = (chunk + (tail if last else " Continuing.")).strip()
            synth = self.voice.synth(line)
            log.info(f"📡 TX: {line}")
            with self._on_air(line, synth, ack):
                if synth is None:
                    self.radio.transmit_text(line)
                else:
                    audio, sr = synth
                    self.radio.transmit_audio(audio, sr)
            if not last:
                time.sleep(1.5)  # gap so others can break in

    def send_sstv(self, subject: str = "picture") -> None:
        """A slow-scan picture of the station, in the background so the agent can answer meanwhile.

        The picture always paints on the demo page and its signal shows on the waterfall. With
        sstv.play_audio it's also played through the reply output as real, decodable SSTV.
        """
        threading.Thread(target=self._sstv, args=(subject,), daemon=True).start()

    def _sstv(self, subject: str) -> None:
        import os

        import numpy as np
        sc = self.cfg["sstv"]
        mode, play = sc["mode"], bool(sc.get("play_audio"))
        m = sstv.MODES[mode]
        squeeze = None if play else float(sc.get("seconds") or 5)  # real audio has to take the mode's full time
        try:
            img = sstv.picture(self.cfg, mode)
            png = sstv.png_bytes(img)
            os.makedirs("logs/sstv", exist_ok=True)
            path = os.path.join("logs", "sstv", time.strftime("sstv-%Y%m%d-%H%M%S.png"))
            with open(path, "wb") as f:
                f.write(png)
            sr = 22050
            audio = sstv.encode(np.asarray(img), mode, sr, seconds=squeeze)
        except Exception as e:
            log.exception("making the SSTV picture failed")
            self.log.bus.emit("error", summary=f"SSTV failed: {e}")
            return
        seconds = len(audio) / sr
        image_id = self.log.add_image(png)
        log.info(f"📷 SSTV {subject}: {m['name']} {img.width}x{img.height}, {seconds:.1f}s"
                 f"{' (playing audio)' if play else ''} -> {path}")
        reply = self.cfg["radio"].get("reply", "radio")
        with self.air:  # don't talk over our own picture
            self.log.spectrum.play(audio, sr)
            self.log.bus.emit("sstv", id=image_id, subject=subject, mode=m["name"], seconds=seconds,
                              header_s=sstv.HEADER_S, line_s=sstv.line_seconds(mode, squeeze),
                              width=img.width, height=img.height, path=path, audio=play)
            try:
                if not play or reply == "screen" or self.cfg["radio"]["mode"] == "text":
                    time.sleep(seconds)  # the page paints it; nothing goes out
                elif reply == "speaker":
                    import sounddevice as sd
                    sd.play(audio, sr, device=self.cfg["radio"]["output_device"], blocking=True)
                else:
                    if seconds > self.cfg["radio"]["max_tx_seconds"]:
                        log.warning(f"{m['name']} takes {seconds:.0f}s, longer than radio.max_tx_seconds – it will be cut short")
                    self.radio.transmit_audio(audio, sr)
            finally:
                self.log.bus.emit("sstv_done", id=image_id, path=path)
        if play and reply == "radio":
            self.send("That was the picture.")  # the voice ID after the image

    @contextmanager
    def _on_air(self, text: str, synth, ack: bool):
        """tx_started/tx_done around one spoken piece; its audio goes on the waterfall as it plays."""
        extra = {}
        if synth is not None:
            audio, sr = synth
            extra = over_stats(audio, sr, self.cfg["radio"]["squelch_threshold"])
            self.log.spectrum.play(audio, sr)
        if self._cq:
            extra.update(cq=True, display=self._cq)
        self.log.bus.emit("tx_started", text=text, ack=ack, **extra)
        try:
            yield
        finally:
            self.log.bus.emit("tx_done", **({"cq": True} if self._cq else {}))


def project_vocab(workdir: str, limit: int = 40) -> str:
    """File/folder names from the agent's project, so Whisper expects your words."""
    import os
    import re
    words: list[str] = []
    skip = {"node_modules", ".git", ".next", "dist", "build", "__pycache__", ".venv"}
    try:
        for root, dirs, files in os.walk(workdir):
            dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
            for name in dirs + files:
                for w in re.split(r"[^A-Za-z0-9]+", os.path.splitext(name)[0]):
                    if len(w) > 2 and w.lower() not in (x.lower() for x in words):
                        words.append(w)
            if root.count(os.sep) - workdir.count(os.sep) >= 2 or len(words) >= limit:
                break
    except OSError:
        pass
    return ", ".join(words[:limit])


def looks_like_hallucination(text: str, hint: str) -> bool:
    """Whisper sometimes 'hears' words from its hint list in noise, e.g. 'Next.js.js.'"""
    import re
    words = [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]
    if not words:
        return True
    hint_words = set(re.split(r"[^a-z0-9]+", hint.lower()))
    return len(words) <= 4 and all(w in hint_words or w in ("js", "over") for w in words)


def caller_allowed(cfg: dict, meta: dict) -> bool:
    """DMR only: optional allow list by callsign / DMR ID (IDs aren't authenticated, but it stops strays)."""
    if cfg["radio"]["mode"] != "dmr":
        return True
    calls = [c.upper() for c in cfg["dmr"]["allowed_callsigns"]]
    ids = [int(i) for i in cfg["dmr"]["allowed_ids"]]
    if not calls and not ids:
        return True
    return (meta.get("callsign", "").upper() in calls) or (meta.get("dmr_id") in ids)


def save_wav(folder: str, audio, sr: int) -> str:
    """Write one over to <folder>/over-HHMMSS.wav so you can hear exactly what the station heard."""
    import os
    import wave
    import numpy as np
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, time.strftime("over-%H%M%S.wav"))
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def run(cfg: dict, save_audio: str | None = None, plain: bool = False, bus: Bus | None = None) -> None:
    bus = bus or Bus()
    ui = None
    if not plain:
        from .console import ConsoleUI
        ui = ConsoleUI(cfg, bus)
        ui.start()
    try:
        _run(cfg, save_audio, bus, ui)
    except KeyboardInterrupt:
        pass
    finally:
        if ui is not None:
            ui.stop()


def _run(cfg: dict, save_audio: str | None, bus: Bus, ui) -> None:
    say = ui.note if ui is not None else log.info  # start-up lines worth seeing on screen
    rad = radio.make(cfg)
    try:
        _loop(cfg, save_audio, bus, ui, rad, say)
    finally:
        rad.close()


def _loop(cfg: dict, save_audio: str | None, bus: Bus, ui, rad, say) -> None:
    ears = stt.make(cfg)
    voice = tts.make(cfg)
    agent = agents.make(cfg)
    history = Log(bus=bus)
    if ui is not None:
        ui.level = lambda: history.spectrum.level
        ui.ready = True
    tx = Transmitter(cfg, rad, voice, history)
    if hasattr(rad, "monitor"):
        rad.monitor = history.spectrum.feed
    if hasattr(rad, "on_open"):
        rad.on_open = lambda: bus.emit("over_started")
    sc = cfg["sidecar"]
    if sc["enabled"]:
        try:
            serve(history, cfg)
        except OSError as e:  # e.g. run.bat already running: carry on without the web page
            log.exception("sidecar failed to start")
            bus.emit("error", summary=f"web page not started – port {sc['port']} is in use ({e.strerror})")
            sc = {**sc, "enabled": False}
    if sc["enabled"]:
        demo = f"http://localhost:{sc['port']}/demo?t={sc['token']}"
        say(f"Sidecar: http://{sc['host']}:{sc['port']}/?t={sc['token']}")
        say(f"Demo screen: {demo}")
        if sc.get("open_demo"):
            import webbrowser
            webbrowser.open(demo)
    station = Station(cfg, agent, history, ack=lambda s: tx.send(s, final_over=False), on_sstv=tx.send_sstv)
    if cq.allowed(cfg):
        caller = cq.CQCaller(cfg, bus, tx.send_cq, level=lambda: history.spectrum.level)
        caller.start()
        say(f"Calling CQ every {caller.lo:.0f}–{caller.hi:.0f}s when the frequency's quiet "
            f"(pauses during a QSO, resumes {caller.quiet_after:.0f}s after it)")
    vocab = project_vocab(cfg["agent"]["workdir"])
    recent: list[str] = []  # last few lines of the conversation, fed to Whisper as context
    mode = cfg["radio"]["mode"]
    log.info(f"radio-agent up · {cfg['station']['callsign']} · radio={mode} · agent={cfg['agent']['backend']}")
    if mode == "text":
        print("Type as if you were on the radio, e.g. 'Claude, what files are in this project? Over'. "
              "'dtmf 1' = confirm, 'dtmf 9' = cancel. Ctrl-D to quit.")
    wait_for_over = bool(cfg["listen"].get("wait_for_over"))
    over_timeout = float(cfg["listen"].get("over_timeout_seconds", 6))
    # Your handheld's roger beep means PTT released: answer at once instead of waiting for "over"
    send_on_release = bool(cfg["listen"].get("send_on_roger_beep", True))
    parts: list[str] = []  # pieces of one long over, stitched together until "over"
    rx_total = stt_total = 0.0  # across the pieces, for the over's summary line
    while True:
        heard = rad.listen(timeout=over_timeout if parts else None)
        if heard is None:
            break
        text = heard.text
        released = send_on_release and heard.meta.get("roger_beep")
        ended_by = "typed" if heard.text else None
        if parts and text is None and (heard.audio is None or not len(heard.audio)):
            # nobody keyed up again (or the roger beep came after a pause): send what we have
            text, parts = " ".join(parts), []
            ended_by = "roger beep" if released else "timeout"
            log.info(f"📻 roger beep – sending: {text!r}" if released else f"⏱  no 'over' heard – sending: {text!r}")
            heard.audio = None
        if text is None and heard.audio is not None and 0 < len(heard.audio) < 0.6 * heard.sample_rate:
            log.debug(f"blip of {len(heard.audio) / heard.sample_rate:.2f}s ignored")
            continue  # a squelch blip / kerchunk, not speech
        if text is None and heard.audio is not None and len(heard.audio):
            rx_s = len(heard.audio) / heard.sample_rate
            if save_audio:
                log.info(f"💾 {rx_s:.1f}s of audio -> {save_wav(save_audio, heard.audio, heard.sample_rate)}")
            t0 = time.time()
            try:
                text = ears.transcribe(heard.audio, heard.sample_rate, context=f"{vocab}. {' '.join(recent)}")
            except Exception as e:  # keep the station up if STT hiccups
                log.exception("speech-to-text failed")
                bus.emit("error", summary=f"speech-to-text failed: {e}")
                continue
            stt_s = time.time() - t0
            log.info(f"🎙 heard ({stt_s:.1f}s STT): {text!r}  dtmf={heard.dtmf!r}")
            stats = over_stats(heard.audio, heard.sample_rate, cfg["radio"]["squelch_threshold"])
            # a lone "Over." finishing a long instruction is real, not noise
            ends_long_over = bool(parts) and speech.ends_with_over(text)
            if not ends_long_over and looks_like_hallucination(text, cfg["stt"].get("hint", "")):
                log.info("   (ignored – sounds like noise, not speech)")
                bus.emit("ignored", text=text, reason="noise", **stats)
                continue
            bus.emit("stt_done", text=text, rx_s=rx_s, stt_s=stt_s, dtmf=heard.dtmf, **stats)
            rx_total, stt_total = rx_total + rx_s, stt_total + stt_s
            ended_by = "roger beep" if released else "squelch closed"
        elif heard.text:
            bus.emit("stt_done", text=heard.text)  # text mode: typed, nothing to transcribe
        if wait_for_over and text and heard.audio is not None:
            parts.append(text)
            if not speech.ends_with_over(text) and not released:
                log.info("   …waiting for 'over'")
                bus.emit("over_waiting", text=" ".join(parts), timeout=over_timeout)
                continue
            if released:
                log.info("📻 roger beep – sending")
            ended_by = "roger beep" if released else "over"
            text, parts = " ".join(parts), []
        if not caller_allowed(cfg, heard.meta):
            log.info(f"ignoring {heard.meta} – not in dmr allow list")
            bus.emit("ignored", text=text or "", reason="not on the allow list")
            continue
        if text:
            bus.emit("over_finished", text=text, rx_s=rx_total, stt_s=stt_total, ended_by=ended_by)
        rx_total = stt_total = 0.0
        try:
            reply = station.handle(text or "", heard.dtmf)
            if reply:
                recent = (recent + [text or "", reply])[-4:]
                tx.send(reply, signoff=station.signoff)
        except Exception as e:  # one bad over mustn't take the station off air
            log.exception("handling the over failed")
            bus.emit("error", summary=f"{type(e).__name__}: {e}")


def main(argv: list[str] | None = None) -> None:
    if not sys.stdout.isatty() and hasattr(sys.stdout, "reconfigure"):
        # piped/redirected on Windows defaults to cp1252, which can't write emoji or box characters
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="radio-agent")
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "devices", "calibrate", "say"])
    ap.add_argument("text", nargs="?")
    ap.add_argument("-c", "--config", default=None)
    ap.add_argument("--agent", help="override agent.backend (echo, claude-code, anthropic, openai, ollama)")
    ap.add_argument("--radio", help="override radio.mode (text, soundcard, rtlsdr, dmr)")
    ap.add_argument("--save-audio", metavar="FOLDER", help="save every over as a .wav, to check what was actually heard")
    ap.add_argument("--profile", action="append", metavar="FILE",
                    help="layer a profile over the config, e.g. profiles/stephen-rockall.yaml (repeatable)")
    ap.add_argument("--plain", action="store_true", help="print simple log lines instead of the live screen (for debugging or piping)")
    a = ap.parse_args(argv)
    cfg = config.load(a.config, a.profile)
    if a.agent:
        cfg["agent"]["backend"] = a.agent
    if a.radio:
        cfg["radio"]["mode"] = a.radio

    if a.cmd == "devices":
        import sounddevice as sd
        print(sd.query_devices())
    elif a.cmd == "calibrate":
        if cfg["radio"]["mode"] not in ("soundcard", "rtlsdr"):
            cfg["radio"]["mode"] = "soundcard"
        r = radio.make(cfg)
        print("Key up on your handheld and talk, then stop. Set squelch_threshold between the two levels.")
        seen = []
        for lvl in r.levels(15):
            seen.append(lvl)
            bar = "#" * min(60, int(lvl * 600))
            print(f"\r{lvl:6.3f} {bar:<60}", end="", flush=True)
        print()
        if seen:
            import numpy as np
            quiet, loud = float(np.percentile(seen, 20)), float(np.percentile(seen, 95))
            print(f"quiet ≈ {quiet:.4f}   talking peak ≈ {loud:.4f}")
            if loud < max(quiet * 3, 0.0005):
                print("Not much difference – move the mic closer to the radio speaker or turn the radio up.")
            else:
                print(f"Suggested squelch_threshold: {max(quiet * 2, (quiet + loud) / 4):.4f}")
                if loud < 0.02:
                    print("It works, but the signal is quiet – turn the radio volume or the Windows input level up for more headroom.")
    elif a.cmd == "say":
        rad = radio.make(cfg)
        Transmitter(cfg, rad, tts.make(cfg)).send(a.text or "Radio check.")
        rad.close()
    else:
        # text mode reads the keyboard, which a live screen would fight with; piped output wants plain lines too
        plain = a.plain or cfg["radio"]["mode"] == "text" or not sys.stdout.isatty()
        setup_logging(plain=plain)
        run(cfg, a.save_audio, plain=plain)


if __name__ == "__main__":
    main(sys.argv[1:])
