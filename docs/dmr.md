# DMR front end: hotspot + DVSwitch

Use your DMR handheld and your MMDVM/Pi-Star hotspot. You don't need a second radio or an audio cable.

```
DMR handheld ))) MMDVM hotspot (Pi-Star, untouched)
                       │  DMR over the network
                       ▼
                 MMDVM_Bridge ── AMBE ──► Analog_Bridge ──(md380-emu decodes AMBE → PCM)
                                                 │  USRP over UDP, 8 kHz PCM
                                                 ▼
                                           radio-agent  (radio.mode: dmr)
```

`radio-agent` only talks to Analog_Bridge over localhost UDP (USRP packets). Everything to the
left of it is standard DVSwitch, the same stack people use to bridge DMR to AllStar.

> **Status:** the radio-agent side is built and tested against a simulated Analog_Bridge. The DVSwitch
> configuration below is a starting point from the DVSwitch docs. Expect to tweak IDs, passwords and
> talkgroups on the night.

## Where to run it

**Leave Pi-Star alone.** Run DVSwitch and radio-agent on a second Linux box: a Pi 4 or 5, an old laptop,
anything. If the hotspot is a Pi Zero, it couldn't run Whisper anyway. Everything here connects outbound,
so Starlink CGNAT is fine.

## Step 1: install DVSwitch Server

On Debian or Raspberry Pi OS:

```bash
wget http://dvswitch.org/install-dvswitch-repo
sudo bash ./install-dvswitch-repo
sudo apt update && sudo apt install dvswitch-server
```

This installs MMDVM_Bridge, Analog_Bridge and the md380-emu AMBE decoder (which runs natively on ARM).

## Step 2: get the hotspot and the bridge onto the same DMR network

### Option A: via BrandMeister (easiest)

Your hotspot is probably on BrandMeister already. MMDVM_Bridge joins BrandMeister as a **second hotspot**
using your DMR ID plus an SSID.

1. On BrandMeister SelfCare, set a **hotspot security password**.
2. In `/opt/MMDVM_Bridge/MMDVM_Bridge.ini`:
   ```ini
   [General]
   Callsign=N0CALL
   Id=<your 7-digit DMR ID>02      ; ID + 2-digit SSID, different from the hotspot's
   [DMR Network]
   Enable=1
   Address=<nearest BM master, e.g. 2341.master.brandmeister.network>
   Port=62031
   Password=<BM hotspot password>
   Slot1=0
   Slot2=1
   ```
3. **Call it with a private call** to `<your DMR ID>02` from your handheld. Don't use a public talkgroup:
   that would put the agent on air for everyone. Program a contact for it on the radio.

### Option B: fully local (private, more setup)

Run a local DMR master (FreeDMR or HBlink3) on the same box. Point a **DMRGateway network** in Pi-Star
at it (Configuration → Expert → DMRGateway → add a network: Address = this box, Port 62031, with a
rewrite for a spare talkgroup). Point MMDVM_Bridge's `[DMR Network]` at the same master. Nothing leaves
your LAN.

## Step 3: Analog_Bridge

Edit `/opt/Analog_Bridge/Analog_Bridge.ini`:

```ini
[GENERAL]
exportMetadata = true          ; sends caller ID/callsign to radio-agent
decoderFallBack = true
useEmulator = true             ; md380-emu software AMBE decoder
emulatorAddress = 127.0.0.1:2470

[AMBE_AUDIO]
ambeMode = DMR
gatewayDmrId = <your DMR ID>
repeaterID = <your DMR ID>02
txTg = <your DMR ID>           ; reply as a private call back to you (or the TG you used)
txTs = 2

[USRP]
address = 127.0.0.1
txPort = 32001                 ; Analog_Bridge → radio-agent   (= dmr.listen_port)
rxPort = 34001                 ; radio-agent → Analog_Bridge   (= dmr.bridge_port)
usrpAudio = AUDIO_UNITY
tlvAudio = AUDIO_USE_GAIN
tlvGain = 0.35
```

Then start the services:

```bash
sudo systemctl enable --now md380-emu analog_bridge mmdvm_bridge
```

## Step 4: radio-agent

```yaml
radio:
  mode: dmr
dmr:
  listen_port: 32001
  bridge_port: 34001
  allowed_callsigns: ["N0CALL"]   # only answer you (your callsign)
stt:
  backend: faster-whisper         # or openai on a small box
tts:
  backend: piper
```

```bash
radio-agent run -c config.yaml
```

Key up and say *"Claude, radio check, over"*.

## DMR-specific notes

- **Caller ID for free.** DMR tells us who's transmitting. With `allowed_callsigns` set, the agent
  ignores everyone else. DMR IDs aren't cryptographically authenticated, but this is a big step up
  from analogue.
- **The keypad doesn't work for confirm here.** DTMF doesn't survive the AMBE codec reliably, so use the
  spoken commands: "confirm", "cancel", "say again".
- **Audio is 8 kHz** (DMR is narrowband). Whisper copes fine. If it mishears, add words to `stt.hint`.
- **Expect a little latency.** The vocoder and network add about half a second each way, on top of
  speech-to-text and the agent.
- **ID.** Your DMR ID and callsign are embedded in every DMR transmission, and radio-agent still gives
  the phonetic voice ID.

## Troubleshooting

- **radio-agent hears nothing:** run `sudo tcpdump -i lo udp port 32001`. If there are no packets,
  Analog_Bridge isn't getting audio, so check `journalctl -u analog_bridge -u mmdvm_bridge`.
- **Robotic or empty audio:** check that md380-emu is running (`systemctl status md380-emu`) and that `useEmulator = true` is set.
- **The reply never reaches your radio:** check `txTg`/`txTs` in `[AMBE_AUDIO]` and that the radio has
  the matching contact or receive group.
