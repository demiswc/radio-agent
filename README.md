# 📻 radio-agent

Talk to an AI assistant over amateur radio. Key up on a handheld, ask a question or give a coding
instruction, and hear the answer come back.

A second radio at home is wired to a PC or Raspberry Pi. It hears your over, transcribes it with
Whisper, hands it to **Claude Code** (or another assistant, such as Claude or ChatGPT over their
APIs, or a local Ollama model), and speaks the reply. It can reply over the air, through the PC
speakers, or just on screen. Code, diffs and paths that don't read well aloud go to a small web page
on your phone.

```
 handheld ))) ((( base radio ══ cable ══ PC / Raspberry Pi
 (you)                                   ├─ squelch detect, roger beep, DTMF
                                         ├─ Whisper       speech → text
                                         ├─ agent         Claude Code / Anthropic / OpenAI / Ollama
                                         ├─ voice         Piper / Windows SAPI / DECtalk / espeak
                                         ├─ terminal UI   (run.bat)
                                         └─ web pages     phone sidecar + WSJT-X style demo screen
```

> **You:** "Claude, the checkout page is throwing a 500, have a look. Over."
> **Station:** "Roger, stand by."
> **Station:** "It's a missing environment variable in the payment handler. I've added a check and the
> tests pass. This is November Zero Charlie Alpha Lima Lima."

The base package is a plain, general-purpose station. It also ships with one optional **demo
profile**: "Stephen", a fictional physicist calling CQ from Rockall (see
[Profiles](#profiles-the-stephen-on-rockall-demo)).

---

## What you need

- **A base radio** that stays at home, plus your handheld. Any FM rig works; a Baofeng is fine.
- **A PC** (Windows tested) **or a Raspberry Pi 4/5**. An NVIDIA GPU makes Whisper much faster, but
  isn't required.
- **A cable** between the base radio and the computer (see below).
- **An AI assistant:** [Claude Code](https://claude.com/claude-code) installed and logged in, or an
  API key for Anthropic or OpenAI, or a local [Ollama](https://ollama.com) model.
- **Python 3.10+.**

## Cables: get audio in and out of the radio

You need two audio paths: **radio speaker → computer input** (to hear you) and, if you want it to
answer over the air, **computer output → radio mic** (to transmit).

### Recommended: an AIOC cable

The [AIOC](https://github.com/skuep/AIOC) ("all-in-one cable") plugs into the Kenwood-style K1 socket
used by Baofeng, Quansheng and many others. It shows up on the computer as a USB sound card *and* a
serial port for PTT. There are no levels to fight and no VOX to tune, and it's the most reliable
option.

```yaml
radio:
  mode: soundcard
  reply: radio
  ptt: { type: serial, port: COM5, assert_dtr: true, assert_rts: false }   # /dev/ttyACM0 on a Pi
```

If the radio doesn't key, swap `assert_dtr` and `assert_rts`. Commercial interfaces such as the
Digirig or SignaLink work the same way.

### Or: a custom cable with VOX

**Know your K1 socket first.** On Baofeng, Quansheng and other Kenwood-style (K1) handhelds:

| Socket | Carries | Tip | Ring | Sleeve |
|---|---|---|---|---|
| **2.5 mm** | **speaker out** | speaker + | TX data | ground |
| **3.5 mm** | **mic in** | mic bias (+V) | mic + | mic − |

**The radio keys PTT when the two sleeves are joined.** A computer's audio input and output share a
ground, so if both leads go straight into the PC the sleeves meet and the radio sits **stuck in
transmit**. (Ask us how we know: a 3.5 mm lead in the "obvious" socket keyed the radio on the first
night, because that socket is the mic.)

1. **Receive:** radio **2.5 mm speaker** socket → the computer's **line in** (blue socket), or the
   input of a cheap USB sound card. An off-the-shelf "2.5 mm to 3.5 mm stereo cable" does it, no
   soldering. With only this plug connected the radio **cannot key up**. Use line in, not mic in:
   mic in has too much gain and puts a bias voltage on the plug. Set the radio volume to about a third.
2. **Transmit (optional):** computer **headphone/line out** → a **ground loop isolator** (a
   transformer dongle, so the two sleeves never meet) → an **attenuator** → the radio's **3.5 mm mic**
   socket. A line-level signal is too hot for a mic input, so a 10 kΩ series / 1 kΩ shunt divider
   brings it down by about 21 dB, with a 1 µF capacitor to keep the radio's mic bias off it:

   ```
   PC out → [ground loop isolator] → tip ──[10k]──┬──[1µF]──► 3.5 mm RING   (mic +)
                                                [1k]
                                     sleeve ──────┴──────────► 3.5 mm SLEEVE (mic −)
                                                              3.5 mm TIP (bias): not connected
   ```

   Don't overdo the attenuation. A 100 Ω shunt (about 40 dB) was too quiet for a Quansheng/Baofeng
   mic input: the radio barely modulated and the other end heard more hiss than voice. 470 Ω (about
   27 dB) is a middle option. Fine-tune the level in software with `radio.tx_gain`: each over is
   normalised to a 0.9 peak and then multiplied by it (start at 1.0).

   Keep the radio's own mic gain low and send it plenty of audio instead. Mic gain amplifies
   whatever noise the cable picks up along with the voice. On a Quansheng, MIC at +15.1 dB (the
   maximum) gave a hissy signal; +8.0 dB with `tx_gain: 2.0` was much cleaner.

   Turn **VOX on** in the base radio. A cut-in-half 3.5 mm stereo lead makes this cable.
3. **Before going on air:** with both leads plugged into the PC, check with a multimeter that there is
   **no continuity** between the 2.5 mm sleeve and the 3.5 mm sleeve. Then, with the radio on low
   power, plug in and make sure the TX light stays off.

Check your own radio's pinout before soldering anything – not every radio with a two-pin socket is
wired the same.

```yaml
radio:
  mode: soundcard
  reply: radio
  ptt: { type: vox }
  vox_lead_in_ms: 250       # a short tone opens VOX so the first word isn't clipped
  output_device: 5          # a dedicated USB sound card, so Windows' "ding" doesn't key the radio
```

### Or: a custom cable with USB-serial PTT

The same two audio leads as the VOX cable, but the computer keys the radio itself: no lead-in tone,
no clipped first words, and a Windows "ding" can't put the radio on air. Add a USB-serial adapter (an
FT232R board with header pins, jumper set to 5 V) and a PC817 optocoupler. RTS lights the LED, and
the transistor joins the radio's two sleeves, which is PTT. Turn **VOX off** on the radio.

```
USB-serial adapter (5 V)                 PC817                       radio
  VCC ──[1k]──► pin 1 (LED +)            pin 4 (collector) ──► 3.5 mm SLEEVE (mic − / PTT)
  RTS ────────► pin 2 (LED −)            pin 3 (emitter)   ──► 2.5 mm SLEEVE (ground)
```

RTS idles high, so the LED is off. Asserting RTS pulls it low, the LED lights and the radio transmits.

- **Wire the emitter straight to the 2.5 mm sleeve, never to the adapter's GND.** The adapter's
  ground is the PC's ground. Wired there, the radio's mic return runs through the computer, and every
  transmission carries the PC's electrical noise as hiss, ground loop isolator or not. The telltale:
  the radio only keys while the receive lead is plugged into the computer.
- **Twist the two PTT wires together.** A loose pair is a good antenna for the radio's own signal.
- **If the radio pulses on high power,** RF is getting back into the adapter. Move the antenna away
  from the cables, clip a ferrite on the PTT pair right at the adapter (3–4 turns), and put a
  1–10 nF capacitor across the two sleeves at the radio end. That shorts out RF without affecting
  PTT. Neater still: mount the PC817 in the box at the radio end, with a 100 nF capacitor across the
  LED, so only the LED wires run back to the computer.

```yaml
radio:
  mode: soundcard
  reply: radio
  ptt: { type: serial, port: COM4, assert_dtr: false, assert_rts: true, tx_delay_ms: 400 }
  tx_gain: 1.0              # raise it (and lower the radio's MIC gain) if the signal sounds hissy
```

The port is in Device Manager under "Ports (COM & LPT)". `radio-agent ptt-test -c config.yaml` keys
the radio for 3 seconds with no audio: listen on another radio for a clean, quiet carrier. Opening
the port doesn't blip the transmitter, because radio-agent sets RTS and DTR to "off" before it opens.

### Simplest start: receive only

Wire just the receive side (the 2.5 mm speaker lead above) and set `reply: speaker` (answers come out of the PC speakers) or
`reply: screen` (text only). Nothing is transmitted, so you don't need a licence check for the PC
side. It's also the best way to get Whisper and squelch working before adding TX.

Keep the base radio on **low power**, and use `radio-agent calibrate` to set the squelch level (below).

---

## Install

### Windows

```bat
git clone https://github.com/<you>/radio-agent.git
cd radio-agent
setup.bat
```

`setup.bat` creates `.venv`, installs everything and lists your sound devices. Copy
`config.example.yaml` to `config.yaml` and edit it (see [Configure](#configure)).

For an NVIDIA GPU: `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` in the venv, then set
`stt.device: cuda`.

### Linux / Raspberry Pi

```bash
sudo apt install -y python3-venv libportaudio2 git
git clone https://github.com/<you>/radio-agent.git && cd radio-agent
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[radio,whisper]'
cp config.example.yaml config.yaml
```

For the voice, download [Piper](https://github.com/rhasspy/piper) and a voice (e.g.
`en_GB-alan-medium.onnx` plus its `.json`) into `~/piper`, or `sudo apt install espeak-ng` and set
`tts.backend: espeak`. To run as a service, see `systemd/radio-agent.service`.

### Voices: DECtalk

[DECtalk](https://github.com/dectalk/dectalk) is the classic 1980s/90s synthesiser, and the Stephen
profile's voice ("Perfect Paul"). It is open source but not bundled here; download it yourself:

- **Windows:** on the repo's [Actions → Build DECtalk](https://github.com/dectalk/dectalk/actions/workflows/build.yml)
  page, open the latest green run and download the **vs2022** artifact (or **vs6**). Unzip its
  `AMD64` folder to `%USERPROFILE%\dectalk`, or to a `dectalk` folder next to this README. The
  vs2022 build has no `say.exe`, so radio-agent drives its `DECtalk.dll` directly; the vs6 build's
  `say.exe` works too.
- **Linux:** download the **ubuntu-latest** artifact and put its `dist/` contents in `./dectalk`
  or `~/dectalk`, or put `say` on your `PATH`.

Then in `config.yaml`:

```yaml
tts:
  backend: dectalk
  dectalk_path: ""       # blank = PATH, ./dectalk, ~/dectalk; or the say executable / its folder
  dectalk_voice: paul    # paul, harry, frank, dennis, betty, ursula, wendy, rita, kit
  dectalk_rate: 180      # words per minute
  fallback: sapi         # optional: use this voice (with a warning) if DECtalk isn't installed
```

`radio-agent say "Radio check" -c config.yaml` tests it. The `dectalk/` folder is git-ignored.

### Pronunciation

Before any voice speaks, whole words are swapped from a small built-in list (`PRONUNCIATIONS`
in `radio_agent/tts.py`): WAV → "wave", SSTV → "S S T V", QSO, QRM, UTC, MHz → "megahertz", kHz,
dB, CTCSS, 73 → "seventy three", Rockall → "Rock all", Crieff → "Creef", Perthshire → "Perth-sure",
README → "read me", npm. Matching ignores case and only takes whole words, so "wave" stays "wave".
Add your own, or map a built-in word to `""` to turn it off:

```yaml
tts:
  pronunciations:
    Strathearn: "Strath-ern"
```

The NATO phonetic alphabet (`PHONETICS`) has its own table. It is only used on the phonetics the
station generates itself (callsign IDs, CQ calls), so "Hotel", "Mike" or "two" in an ordinary
reply are spoken normally. Each word has a respelling, which Piper, SAPI and the rest say ("Delta"
→ "dell-tah"), and a DECtalk entry: either `[phonemes]` or plain text, whichever DECtalk gets heard
correctly with. For example `[d'ehltax]` came out as "Delga", but "Dell-tah" is heard as Delta.

Two commands check it:

- `radio-agent say --check-pronunciation -c config.yaml` runs every phonetic word and the
  test callsign (N0CALL, plus your own and station.operator) through the voice and then the
  station's Whisper model, with no hint prompt, and prints a pass/fail table with what Whisper
  heard and what the voice was sent. Re-run it whenever you change the voice, the rate or a
  pronunciation. Add `--compare` to also try each word plain, respelled and as DECtalk phonemes.
  With DECtalk Paul at 180 wpm it scored 37/38 (counted with the old second test call). Quebec
  isn't reliably heard in any form tried so far.
- `radio-agent say --voice-test -c config.yaml` says them one at a time on the PC speakers, to check
  by ear.

Override any of them: a plain string is a respelling for every voice, or set DECtalk's own entry too:

```yaml
tts:
  pronunciations:
    Delta: "dell-tuh"
    Sierra: {say: "see-err-ah", dectalk: "[siy'ehrax]"}   # dectalk: "[phonemes]" or plain text
```

Test DECtalk phonemes with the check: a bad code makes DECtalk say "command error" out loud.

A Pi Zero is too slow for local Whisper. Use `stt.backend: openai`, or run on a bigger machine.

### The assistant

- **Claude Code** (`agent.backend: claude-code`): `npm i -g @anthropic-ai/claude-code`, then run
  `claude` once to log in. It works in `agent.workdir`; point that at a **sandbox** project.
- **Anthropic / OpenAI** (plain chat, no file access): put `ANTHROPIC_API_KEY=` or `OPENAI_API_KEY=` in `.env`.
- **Ollama**: install it and pull a model; set `agent.backend: ollama`.

## Configure

Everything has a default, so `config.yaml` only needs what's different for you. The main ones:

| Setting | What it's for |
|---|---|
| `station.callsign` | Your callsign: given phonetically at the start of a QSO, every `id_interval_seconds`, and on sign-off |
| `radio.input_device` / `output_device` | Sound devices, from `radio-agent devices` |
| `radio.reply` | `radio` (transmit), `speaker` (PC speakers) or `screen` (text only) |
| `radio.squelch_threshold` | Audio level that counts as someone talking; set it with `calibrate` |
| `radio.ptt` | How the radio is keyed: `serial` (DTR/RTS on a COM port), `vox` or `none` |
| `radio.tx_gain` | Transmit level, applied after each over is normalised to a 0.9 peak |
| `listen.require_wake_word` | `true` on a shared frequency ("Claude, …"); `false` on a private link |
| `agent.backend` / `agent.model` / `agent.workdir` | Which assistant, which model, which project |
| `agent.require_confirm` | `true`: nothing changes until you say "confirm" |
| `stt.model` / `stt.device` | `small.en` on `cuda` is accurate and fast; `base.en` on `cpu` is fine |

Then check the audio:

```bat
radio-agent devices                    :: list sound cards
calibrate.bat                          :: key up, talk, stop: it suggests a squelch_threshold
radio-agent ptt-test -c config.yaml    :: serial PTT: key for 3 seconds, no audio
radio-agent say "Radio check" -c config.yaml
```

## Run it

```bat
run.bat                                :: Windows
radio-agent run -c config.yaml         :: anywhere
```

The terminal shows a live screen in the style of Claude Code:
- a `›` block for each of your overs, with how long it was and how it ended
- a `●` block for each reply, with how long the assistant took
- a status line at the bottom (listening, receiving, thinking, transmitting, the next CQ)

Everything else goes to `logs/radio-agent.log`. Use `run.bat --plain` for simple log lines instead,
for debugging or piping. `--save-audio recordings` saves every over as a WAV, which is handy when
transcripts look wrong.

Two web pages are served (the address is printed at start-up):
- **`http://<pc>:8073/?t=<token>`**: the phone sidecar, with code, diffs and anything that doesn't speak well.
- **`http://<pc>:8073/demo?t=<token>`**: a **WSJT-X style screen**, for showing it off. It has a live
  waterfall of the receive audio, Band Activity and Rx Frequency panes, the dial, a UTC clock, and an SSTV
  receiver window. Set `sidecar.open_demo: true` to open it automatically.

Change `sidecar.token` from `change-me` before you put it on a network.

## On the air

| Say | or key | Does |
|---|---|---|
| "Claude, …" | | Wake word; follow-ups within 3 minutes don't need it |
| "…, over" | | Sends what you've said. A roger beep (if your handheld sends one) or a pause also ends your turn |
| "confirm" / "go ahead" | `1` | Do the change the assistant proposed (when `require_confirm` is on) |
| "cancel" / "negative" | `9` | Drop it; nothing is changed |
| "say again" | `0` | Repeat the last reply |
| "status" | `5` | What's pending |
| "new session" | | Forget the conversation |
| "send me an SSTV" | | The station sends a slow-scan picture (see below) |
| "weather fax for Biscay" | | The latest WEFAX weather chart for that area (see below) |
| "close" / "clear the picture" | | Close the SSTV picture |
| "73" / "QRT" / "…, clear" / "thanks for the contact" | | The station signs off: "…Seventy-three, (your call), this is (its call), clear." |
| | `*PIN#` | Unlock, if `listen.pin` is set |

Long instructions can span several transmissions: keep talking after a pause and it stitches them
together until you say "over" (`listen.wait_for_over`).

### Extras

- **SSTV.** Say "SSTV" in an over and a picture paints on the demo screen in about 5 seconds, with
  its signal on the waterfall. It sends one of your photos from `sstv/`, or a built-in test card.
  With `sstv.play_audio: true` it also plays real **Robot 36** audio, which a phone app (e.g. Robot36 on
  Android) can decode. That takes the full 36 s.
- **WEFAX weather charts.** Ask for a weather fax, weather chart or surface analysis for somewhere
  ("48 hour forecast for the Bay of Biscay", "wind and waves around Hawaii", "satellite picture of the
  Caribbean"). The station downloads the newest radiofax chart for that area from
  [NOAA's radiofax archive](https://tgftp.nws.noaa.gov/fax/). These are the same charts the US Coast
  Guard broadcasts on HF, updated as each one is issued. The chart paints in the receiver window like
  a fax coming in, with a fax-style signal on the waterfall.
  - **Coverage:** the North Atlantic (east and west), the tropical Atlantic and Caribbean, the Gulf of
    Mexico, the north-east, north-west, tropical and central Pacific, and the Arctic/Alaska.
  - **Charts:** surface analysis, 24/48/72 hour forecasts, wind and waves, 500 mb, satellite, and
    tropical cyclone danger areas.
  - **Elsewhere:** for places NOAA doesn't cover, the assistant can search the web and pass a chart's
    image address (`SSTV: https://…`).
  - **Making it the default:** set `sstv.source: wefax` and every "SSTV" request sends a chart, for
    `wefax.default_region` when you don't say where.
- **CQ calling** (`cq.enabled`). The station calls CQ every 1–2 minutes when the frequency is quiet,
  stops during a QSO, and resumes 45 s after it ends. It only works with `reply: speaker` or
  `screen` unless you set `cq.on_air: true` (see the licence notes).
- **QSO procedure.** The station adds every callsign itself; the assistant only writes the message
  (anything it adds anyway, like a callsign or a trailing "over", is taken out). With the station as
  N0CALL and you as N1CALL (example calls: use your own), the Stephen profile says:
  - first over of a QSO (nothing exchanged for `qso.new_after_minutes`, default 10), and again at least
    every `station.id_interval_seconds`: "November One Charlie Alpha Lima Lima, this is November Zero
    Charlie Alpha Lima Lima base station, Stephen the automated operator. *…message…* Over to N1CALL,
    N0CALL base station, over."
  - the quick back-and-forth in between: "N1CALL. *…message…* N0CALL base station, over." (calls
    said letter by letter)
  - a long reply split over several overs ends each earlier part "N0CALL base station, continuing."
  - when you sign off ("73", "seventy-three", "QRT", "…, clear"): "*…message…* Seventy-three,
    November One Charlie Alpha Lima Lima, this is November Zero Charlie Alpha Lima Lima base station,
    Stephen the automated operator, clear." The next over starts a new QSO.

  The station and the operator can be the same call, if you're calling your own station from a
  handheld.

  The built-in defaults leave out "base station" and "Stephen the automated operator".

  The other station is the callsign heard in the over that starts the QSO (for example answering a
  CQ), or else `station.operator`. The demo screen shows each over as WSJT-X does: "<them> <me>
  *message*". The wording comes from the `qso:` section of the config, so a profile can use its own
  style (see `config.example.yaml`). Set `station.id_every_over: true` for the full ID every over.
- **Contacts and the logbook.** How the station behaves depends on who started the QSO:
  - **Someone answers its CQ** (within `qso.cq_answer_minutes`, default 5): it called CQ to make a
    contact, so it leads. It gives a signal report, its name, QTH and the real weather where it is,
    then asks for their name, QTH and report. It reads them back, checked against HamQTH, and logs
    the contact. It doesn't offer to help with anything.
  - **You call CQ** and it answers: you're in control, and it follows your lead.
  - **You just call it**, for example to ask it something: it answers as an assistant.

  It also answers without its wake word when you give its callsign, or just after it has called CQ.
  Contacts go into an ADIF logbook, `logs/<callsign>.adi`, which loggers, LoTW, QRZ and HamQTH can
  all import. Each logged contact also shows up on the demo screen.
  - **Weather** comes from [Open-Meteo](https://open-meteo.com) (free, no key) at the position of
    `station.locator`: wind in Beaufort, sea state, visibility, temperatures and the pressure tendency.
  - **Callsign lookups** use [HamQTH](https://www.hamqth.com) (free): make an account, then put
    `HAMQTH_USER=` and `HAMQTH_PASS=` in `.env`. Without them it just skips the check.
- **WEFAX.** Ask for a weather fax or chart ("SSTV me the 48 hour forecast for Biscay") and the latest
  NOAA radiofax chart for that area paints in the same receiver window as an SSTV picture. Asking
  for its SSTV or QSL card still sends the station's own picture.
- **DMR.** With a DMR handheld and an MMDVM / Pi-Star hotspot, `radio.mode: dmr` talks to DVSwitch's
  Analog_Bridge. You don't need a second radio or a cable, and caller ID lets it answer only you
  (`dmr.allowed_callsigns`). See **[docs/dmr.md](docs/dmr.md)**.

## Profiles: the Stephen-on-Rockall demo

A **profile** is a partial config layered over yours. It changes the character and the look, not
your hardware or callsign:

```bat
run-stephen.bat
:: same as: radio-agent run -c config.yaml --profile profiles\stephen-rockall.yaml
```

[`profiles/stephen-rockall.yaml`](profiles/stephen-rockall.yaml) turns the station into **Stephen**, a
fictional theoretical physicist operating from the islet of Rockall in the North Atlantic. He has a
dry wit, likes a cosmology aside, and answers an SSTV request with the latest WEFAX weather chart
for wherever you ask about, or his own sunset-over-Rockall picture if you ask for "a picture". He calls
CQ when the band is quiet, and the demo screen is titled ROCKALL-X. He uses *your*
`station.callsign` and knows `station.operator` is the person calling him. The voice he was designed
around is DECtalk's classic "Perfect Paul" (see [Voices: DECtalk](#voices-dectalk)); without
DECtalk installed he uses the Windows SAPI voice and prints a warning.

Stephen is a made-up character for demos, not a portrayal of anyone real. To make your own, copy
the profile and change `station.name`, `qth`, `agent.persona` (it can use `{callsign}`,
`{operator}`, `{name}` and `{qth}`), `sstv.picture` and `sidecar.title`.

## Assistants

| `agent.backend` | Can change files? | Notes |
|---|---|---|
| `claude-code` | yes | Runs Claude Code in `agent.workdir`. With `require_confirm` it plans read-only until you confirm; `allowed_tools` limits what it can run |
| `anthropic` / `openai` | no | Plain chat, good for questions |
| `ollama` | no | Local model, no internet needed (portable, emergency comms) |
| `echo` | no | Repeats you back, for testing the radio side |

`radio-agent run --agent echo --radio text` tests the whole loop at the keyboard with no radio at all.

## Safety and licence notes (UK; check your own country's rules)

- **Anyone on the frequency can talk to it.** Point `agent.workdir` at a sandbox, never at production.
  Keep SSH keys, `.env` files and deploy scripts out of its reach, and keep `allowed_tools` tight. On a
  shared frequency, keep `require_wake_word: true` and consider `listen.pin`.
- **DTMF PINs are sent in the clear.** They stop casual users, not a determined listener.
- **Identification.** Your callsign must go out at the start and end of a QSO and at regular
  intervals. The defaults do that. Only ever use a callsign you currently hold.
- **Unattended and automatic transmissions.** Automatic CQ calls and a station that answers on its
  own are unattended operation. Check your licence terms first. That's why `cq.on_air` is off by
  default and why `reply: speaker` is a good place to start.
- **Content.** The assistant is told to keep to plain language, no profanity and no advertising.
  Amateur radio isn't for business, so keep this to experiments and hobby projects.
- **Transmit cap.** `max_tx_seconds` limits each over; long replies are split, with gaps for others to
  break in.

## Troubleshooting

- **Transcripts cut off or miss words:** keep `stt.vad_filter: false`. Whisper's voice detection drops
  chunks of FM speech. Save the audio (`--save-audio recordings`) and compare it with what was transcribed.
- **It triggers on noise, or never hears you:** re-run `calibrate.bat` and set `squelch_threshold`
  between the quiet level and your talking level.
- **Hiss on transmit, but the radio sounds clean on its own mic:** check the PTT wiring first (the
  optocoupler's emitter goes to the 2.5 mm sleeve, not the adapter's GND). Then the levels: less
  attenuation or a higher `tx_gain` going in, and a lower MIC gain on the radio.
- **`ptt-test` or `say` fails with "could not open port … Access is denied":** another copy of
  radio-agent (`run.bat`) has the COM port open. Stop it first.
- **The radio pulses or drops out on high power:** RF is getting into the PTT circuit. See the
  USB-serial PTT section above, and stay on low power until it's fixed.
- **"WEFAX chart not available":** the PC couldn't reach tgftp.nws.noaa.gov. The station sends its own
  picture instead.
- **"web page not started – port 8073 is in use":** another copy is already running, or another
  program has the port. Change `sidecar.port`.
- **Claude Code isn't found:** set `agent.claude_path` to the full path of `claude` (`claude.cmd` on Windows).

## Layout

```
radio_agent/
  __main__.py  CLI, run loop, transmitter (chunking, callsign ID, SSTV, CQ)
  radio.py     sound card radio: squelch-gated receive, roger beep, PTT (serial / VOX)
  dmr.py       DMR via DVSwitch Analog_Bridge (USRP over UDP)
  rtlsdr.py    receive-only RTL-SDR front end
  stt.py       faster-whisper / OpenAI speech-to-text
  agents.py    Claude Code / Anthropic / OpenAI / Ollama / echo, and the radio system prompt
  station.py   one over at a time: wake words, confirm, commands, SSTV, sign-off (no audio, fully tested)
  speech.py    phrase matching, SAY/DETAIL/CONFIRM/SSTV/SIGNOFF parsing, phonetics
  events.py    the event stream both screens are drawn from
  console.py   the live terminal screen
  sidecar.py   web server: phone page, demo screen, waterfall feed
  demo.html    the WSJT-X style demo screen
  sstv.py      SSTV encoder (Robot 36, Martin M1), WEFAX-style signal, built-in pictures
  wefax.py     latest NOAA radiofax charts by area
  cq.py        automatic CQ calling
  tts.py dtmf.py config.py
profiles/      character profiles (stephen-rockall.yaml)
tests/         pytest: `pip install -e .[dev]` then `pytest`
```

## Licence

MIT. See [LICENSE](LICENSE).
