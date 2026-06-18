# Palabra Zoom Bridge

Local MVP bridge for one Zoom interpretation channel:

```text
Zoom Spanish audio -> VB cable -> Python bridge -> Palabra -> VB cable -> Zoom German interpreter mic
```

## 1. Install Python dependencies

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 2. Configure Palabra credentials

Copy `.env.example` to `.env` and fill in:

```text
PALABRA_CLIENT_ID=...
PALABRA_CLIENT_SECRET=...
```

## 3. Configure bridge defaults

Set the default language pair, audio devices, and timing settings in `config.toml`:

```toml
[translation]
source_language = "es"
target_language = "de"
voice_id = "your-voice-id"

[zoom]
# Copy these from Zoom's Speaker and Microphone settings. The bridge uses the
# matching opposite cable sides automatically.
speaker_device = "CABLE-B Input"
mic_device = "CABLE-A Output"

[audio]
# Optional direct overrides. Use a device name substring or numeric id from
# --list-devices when automatic Zoom-device matching is not enough.
# input_device = "CABLE-B Output"
# output_device = "CABLE-A Input"
device_rate = 48000
api_rate = 24000
# Keep Palabra mono, but open the Windows VB-Cable streams as stereo.
channels = 1
device_channels = 2
# DirectSound is the proven-good backend for this bot PC.
# WASAPI is blocked because it fails inside Windows WDM/KS with DeviceIoControl GLE=0x490.
hostapi_preference = ["Windows DirectSound", "MME", "Windows WDM-KS"]

[bridge]
chunk_ms = 320
input_block_ms = 0
playback_buffer_ms = 500
playback_fade_ms = 5
idle_noise_amplitude = 0
output_gain = 0.55
startup_delay = 3.0

[palabra]
segment_confirmation_silence_threshold = 0.3
only_confirm_by_silence = false
sentence_splitter_enabled = true
desired_queue_level_ms = 2000
max_queue_level_ms = 5000
auto_tempo = true
min_tempo = 1.0
max_tempo = 1.1

[diagnostics]
test_seconds = 3.0
test_volume = 0.5
record_output_wav = false
```

`voice_id` is optional. When set, the bridge passes it through to Palabra speech generation for the interpreted audio.

`channels` controls the Palabra websocket audio format. `device_channels` controls the local Windows virtual cable streams. The proven-good setup keeps Palabra mono and writes stereo into Zoom's microphone cable, which prevents silent interpretation audio with VB-Cable endpoints that appear as multi-channel DirectSound devices.

`playback_buffer_ms` is the base jitter buffer before audio is released to Zoom's microphone cable. The live default is intentionally low so listeners hear the interpretation close to real time. If playback underruns, the bridge grows the buffer in 500 ms steps up to 5000 ms, then relaxes it back down after stable playback. The bridge uses Palabra phrase boundaries from `output_audio_data.transcription.last_chunk` when available: if playback reserve is low at a phrase end, it waits there instead of starting the next phrase with too little buffered audio. A local quiet-block gate remains as a fallback.

`playback_fade_ms`, `idle_noise_amplitude`, and `output_gain` control the exact stream sent into Zoom's microphone cable. With Zoom Original Sound enabled, keep `idle_noise_amplitude = 0` unless you are specifically testing Zoom gating. `output_gain` reduces the translated signal before Zoom so downstream automatic gain or recording does not clip.

The `[palabra]` section is tuned for live interpretation rather than offline dubbing. `segment_confirmation_silence_threshold` controls how much silence Palabra waits for before confirming a segment; lower values reduce long waits but can split phrases earlier. `only_confirm_by_silence` can force stricter phrase confirmation at the cost of latency, `sentence_splitter_enabled` allows long sentences to become smaller phrase chunks, and the queue level values keep a modest translated-speech reserve. Palabra documents `desired_queue_level_ms` as starting at 2000 ms, so this config uses the lowest valid value. `auto_tempo` lets Palabra speak slightly faster, up to `max_tempo`, when it needs to catch up.

The `[zoom]` devices should match what you selected in Zoom. For the example above, the script records from the matching `CABLE-B Output` side and plays translated audio into the matching `CABLE-A Input` side automatically.

Device settings can be name substrings like `"CABLE-B Input"` or exact numeric ids from `--list-devices`. Numeric ids are machine-specific, so prefer names in `config.toml`.

The bridge ranks complete cable pairs by `audio.hostapi_preference`, then chooses the matching opposite cable ends on the same audio host API. WASAPI is blocked in the script for this bot PC. On this machine, WASAPI fails while starting the VB-Cable stream with a Windows WDM/KS `DeviceIoControl` error `GLE=0x490`, which means Windows could not find the requested driver property/element for that endpoint.

`input_block_ms = 0` lets PortAudio choose the callback block size requested by the selected Windows audio backend. Set a positive value only if you specifically need fixed-size PortAudio callbacks.

In normal use, leave `input_device` and `output_device` commented out. They are only direct overrides for troubleshooting.

You can override any of these defaults on the command line for a single run, for example `--source-language`, `--target-language`, `--input-device`, `--output-device`, or `--device-rate`.

## 4. List audio devices

```powershell
.\.venv\Scripts\python.exe palabra_zoom.py --list-devices
```

Look for the VB-Audio devices. You should see each virtual cable on more than one Windows audio backend, usually including `Windows DirectSound`, `MME`, and sometimes `Windows WDM-KS`. WASAPI may also appear, but this bridge blocks it on this bot PC.

```text
CABLE-A:
  CABLE-A Input  (playback side, use as bridge output)
  CABLE-A Output (recording side, use as Zoom microphone)

CABLE-B:
  CABLE-B Input  (playback side, use as Zoom speaker)
  CABLE-B Output (recording side, use as bridge input)
```

## 5. Recommended Windows audio routing

Use one cable for Zoom-to-bridge and one cable for bridge-to-Zoom.

In the Zoom desktop client signed in as `maveprg+es.de.interpreter@gmail.com`:

- Speaker: `CABLE-B Input (VB-Audio Virtual Cable B)`
- Microphone: `CABLE-A Output (VB-Audio Virtual Cable A)`

The bridge records from the matching recording side:

- Bridge input: `CABLE-B Output`
- Bridge output: `CABLE-A Input`

## 6. Test the cable routing

With Zoom open on the bot account, set Zoom's microphone to `CABLE-A Output`, then play a test tone into that cable:

```powershell
.\.venv\Scripts\python.exe palabra_zoom.py --test-output "CABLE-A Input"
```

Zoom's microphone meter should move.

Next, set Zoom's speaker to `CABLE-B Input`, play meeting audio in Zoom, and meter the matching recording side:

```powershell
.\.venv\Scripts\python.exe palabra_zoom.py --meter-input "CABLE-B Output"
```

The level meter should move when Spanish audio is audible in Zoom.

## 7. Start the bridge

```powershell
.\.venv\Scripts\python.exe palabra_zoom.py
```

To test audio-device selection without starting a Palabra session:

```powershell
.\.venv\Scripts\python.exe palabra_zoom.py --check-devices
```

The bridge writes debugging files to `logs/`:

- `cable_route.log` keeps a timestamped history of the selected cable route and final bridge devices.
- `last_error.txt` is overwritten with the latest startup/runtime error so the details survive if the terminal window closes.

To diagnose noisy interpreted audio, run with `--record-output-wav` or set `diagnostics.record_output_wav = true`. The bridge writes timestamped WAV files to `debug/`: Palabra's mono output, the stereo audio queued for Zoom's microphone cable, and the exact callback buffers sent to PortAudio, including any inserted silence. If the queued Zoom microphone WAV is clean but the callback WAV has gaps, the bridge is receiving good audio but the real-time playback buffer is starving before Zoom receives it.

To check whether Palabra is sending timing or phrase metadata with the stream, add `--dump-palabra-messages` for one short test run. It prints the first payload shape for each message type and replaces base64 audio with a length marker.

Override configured values for a single run:

```powershell
.\.venv\Scripts\python.exe palabra_zoom.py --input-device "CABLE-B Output" --output-device "CABLE-A Input" --source-language es --target-language en
```

If device names are ambiguous, use numeric ids from `--list-devices`:

```powershell
.\.venv\Scripts\python.exe palabra_zoom.py --input-device <input-id> --output-device <output-id>
```

## 8. Zoom meeting flow

1. Host schedules the meeting with Language Interpretation enabled.
2. Host assigns `maveprg+es.de.interpreter@gmail.com` as the Spanish/German interpreter.
3. Bot account joins with the Zoom desktop client and is signed in as that email.
4. Host starts interpretation.
5. Bot is recognized by Zoom as the interpreter for the German channel.
6. Start the bridge after the bot's Zoom speaker/microphone devices are set.

Guests should then select German in Zoom's Interpretation menu.
