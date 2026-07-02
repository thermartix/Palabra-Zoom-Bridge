# Palabra Zoom Bridge

Version: 0.3.23

Local MVP bridge for one Zoom interpretation channel:

```text
Zoom Spanish audio -> VB cable -> Python bridge -> Palabra -> VB cable -> Zoom German interpreter mic
```

## 1. Install Python dependencies

```powershell
& "C:\Users\marti\.venvs\palabra_zoom\Scripts\python.exe" -m pip install -r requirements.txt
```

Install `ffmpeg` and make sure it is available on `PATH` if you want MP3 debug recordings.

## 2. Configure Palabra credentials

Copy `.env.example` to `.env` and fill in:

```text
PALABRA_CLIENT_ID=...
PALABRA_CLIENT_SECRET=...
ZOOM_SDK_CLIENT_ID=...
ZOOM_SDK_CLIENT_SECRET=...
```

The Zoom SDK values are used only by `sdk-probe` mode and must stay local.

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
input_block_ms = 50
playback_buffer_ms = 500
playback_tempo = 1.075
playback_max_tempo = 1.15
playback_tempo_algorithm = "resample"
playback_fade_ms = 5
idle_noise_amplitude = 0
output_gain = 0.55
output_peak_limit = 0.65
startup_delay = 0.0
task_ready_timeout_seconds = 30.0
task_poll_seconds = 2.0
end_task_eos_timeout_seconds = 2.0
graceful_shutdown_timeout_seconds = 6.0
playback_drain_timeout_seconds = 30.0

[palabra]
# These segmentation settings favor complete returned audio over low latency.
# Changing them can make Palabra split or replace speech segments
# unpredictably, which may cause missing words/audio chunks.
segment_confirmation_silence_threshold = 0.8
only_confirm_by_silence = true
sentence_splitter_enabled = false
translate_partial_transcriptions = false
desired_queue_level_ms = 3000
max_queue_level_ms = 8000
auto_tempo = false
min_tempo = 1.0
max_tempo = 1.0

[diagnostics]
test_seconds = 3.0
test_volume = 0.5
record_debug_mp3 = false
```

`voice_id` is optional. When set, the bridge passes it through to Palabra speech generation for the interpreted audio.

`channels` controls the Palabra websocket input format. Palabra websocket output is handled as fixed 24 kHz mono audio, per the API contract, and is then resampled/remixed for the local cable. `device_channels` controls the local Windows virtual cable streams. The proven-good setup keeps Palabra input mono and writes stereo into Zoom's microphone cable, which prevents silent interpretation audio with VB-Cable endpoints that appear as multi-channel DirectSound devices.

`playback_buffer_ms` is the base jitter buffer before audio is released to Zoom's microphone cable. Set it in `config.toml` to 300 ms or higher; lower values are not recommended for stable live playback. `phrase_start_buffer_ms` is a larger first-phrase buffer that smooths Palabra chunk jitter before partial phrase audio is released; raise it if words still split with silence, or lower it if latency matters more. The bridge groups Palabra audio by `transcription_id`, `translation_part_id`, and language, then waits to release the start of each phrase until it has enough audio for the phrase-start buffer or Palabra marks the phrase complete with `last_chunk`. `playback_tempo` is the normal local playback speed, and `playback_max_tempo` is the catch-up ceiling when translated audio backlog builds; both affect only the local Zoom microphone output, not the raw Palabra API audio. Tempo adjustment is applied before audio enters the callback playback queue, so the callback only copies prepared PCM. Keep `playback_tempo_algorithm = "resample"` for the stable live path; `"rubberband"` is experimental and accumulates larger blocks before playback, so check the callback debug MP3 for artifacts. If playback still underruns during active speech, the bridge holds the remaining partial audio for the next refill instead of playing a tiny word fragment into silence, grows the buffer in small 200 ms steps, and later relaxes back down after stable playback. A local quiet-block gate remains as a fallback.

`playback_fade_ms`, `idle_noise_amplitude`, `output_gain`, and `output_peak_limit` control the exact stream sent into Zoom's microphone cable. `playback_fade_ms` is also used as a short chunk-join crossfade inside the live callback buffer, which reduces edge clicks when prepared audio blocks meet without changing the raw Palabra API recording. With Zoom Original Sound enabled, keep `idle_noise_amplitude = 0` unless you are specifically testing Zoom gating. `output_gain` reduces the translated signal before Zoom so downstream automatic gain or recording does not clip. `output_peak_limit` is a final callback limiter; keep it below 1.0 to prevent rare processing spikes or clicks from hitting Zoom hard enough to trigger automatic mic-volume ducking.

`startup_delay` is now only an optional extra settle delay after Palabra reports `current_task`; normal startup readiness is driven by `get_task` polling. Manual Ctrl+C shutdown sends Palabra `end_task` and waits briefly for EOS so the last interpreted phrase can drain. Zoom meeting-ended shutdown closes immediately because there is no meeting audio path left.

The `[palabra]` section is tuned to favor complete returned audio over the lowest possible latency. This segmentation profile is the setting group that stopped the observed missing/cut-off Palabra audio: `segment_confirmation_silence_threshold = 0.8`, `only_confirm_by_silence = true`, `sentence_splitter_enabled = false`, and `translate_partial_transcriptions = false`. Change these carefully; lower thresholds, non-silence confirmation, sentence splitting, or partial translation can make Palabra split or replace speech segments unpredictably, which may bring missing words/audio chunks back. The larger queue levels give Palabra more reserve before speaking. `auto_tempo = false` keeps speech timing fixed during cut-out tests; enable it later if catching up becomes more important than maximum completeness.

The `[zoom]` devices should match what you selected in Zoom. For the example above, the script records from the matching `CABLE-B Output` side and plays translated audio into the matching `CABLE-A Input` side automatically.

By default, `zoom.end_bridge_when_meeting_ends = true` watches for a visible Zoom meeting or webinar window. After that window has been seen once, the bridge stops and closes the Palabra session if the matching window title disappears for `meeting_end_grace_seconds`. If Zoom uses a localized or custom title on this machine, add a stable substring to `zoom.meeting_title_patterns`; use `--no-end-when-zoom-meeting-ends` to disable this for a run.

Device settings can be name substrings like `"CABLE-B Input"` or exact numeric ids from `--list-devices`. Numeric ids are machine-specific, so prefer names in `config.toml`.

The bridge ranks complete cable pairs by `audio.hostapi_preference`, then chooses the matching opposite cable ends on the same audio host API. WASAPI is blocked in the script for this bot PC. On this machine, WASAPI fails while starting the VB-Cable stream with a Windows WDM/KS `DeviceIoControl` error `GLE=0x490`, which means Windows could not find the requested driver property/element for that endpoint.

`input_block_ms` controls the bridge input callback size. The default `50` ms keeps callbacks regular without making the capture path too chatty.

In normal use, leave `input_device` and `output_device` commented out. They are only direct overrides for troubleshooting.

You can override any of these defaults on the command line for a single run, for example `--source-language`, `--target-language`, `--input-device`, `--output-device`, or `--device-rate`.

## 4. List audio devices

```powershell
& "C:\Users\marti\.venvs\palabra_zoom\Scripts\python.exe" palabra_zoom.py --list-devices
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
& "C:\Users\marti\.venvs\palabra_zoom\Scripts\python.exe" palabra_zoom.py --test-output "CABLE-A Input"
```

Zoom's microphone meter should move.

Next, set Zoom's speaker to `CABLE-B Input`, play meeting audio in Zoom, and meter the matching recording side:

```powershell
& "C:\Users\marti\.venvs\palabra_zoom\Scripts\python.exe" palabra_zoom.py --meter-input "CABLE-B Output"
```

The level meter should move when Spanish audio is audible in Zoom.

## 7. Start the bridge

```powershell
& "C:\Users\marti\.venvs\palabra_zoom\Scripts\python.exe" palabra_zoom.py
```

To start the bridge with MP3 debug recording enabled for this run:

```powershell
& "C:\Users\marti\.venvs\palabra_zoom\Scripts\python.exe" palabra_zoom.py --record-debug-mp3
```

To test audio-device selection without starting a Palabra session:

```powershell
& "C:\Users\marti\.venvs\palabra_zoom\Scripts\python.exe" palabra_zoom.py --check-devices
```

The bridge writes debugging files to `logs/`:

- `cable_route.log` keeps a timestamped history of the selected cable route and final bridge devices.
- `last_error.txt` is overwritten with the latest startup/runtime error so the details survive if the terminal window closes.

To diagnose noisy interpreted audio, run with `--record-debug-mp3` or set `diagnostics.record_debug_mp3 = true`. During the live run, the bridge writes lightweight timestamped WAV files for the mono input sent to Palabra, Palabra's mono output, the stereo audio queued for Zoom's microphone cable, and the exact callback buffers sent to PortAudio, including any inserted silence. After shutdown, it converts those WAV files to MP3 in `debug/` and deletes each temporary WAV after a successful conversion. It also writes `palabra_text_events_*.txt`, with timestamped partial/final transcription events, output audio chunk metadata, skipped/malformed output-audio payloads, and warnings for output audio groups that ended without `last_chunk=true`. If the queued Zoom microphone MP3 is clean but the callback MP3 has gaps, the bridge is receiving good audio but the real-time playback buffer is starving before Zoom receives it. The older `--record-output-wav` flag and `diagnostics.record_output_wav` setting are still accepted as compatibility aliases, but final recordings are written as MP3.

To check whether Palabra is sending timing or phrase metadata with the stream, add `--dump-palabra-messages` for one short test run. It prints the first payload shape for each message type and replaces base64 audio with a length marker.

Override configured values for a single run:

```powershell
& "C:\Users\marti\.venvs\palabra_zoom\Scripts\python.exe" palabra_zoom.py --input-device "CABLE-B Output" --output-device "CABLE-A Input" --source-language es --target-language en
```

If device names are ambiguous, use numeric ids from `--list-devices`:

```powershell
& "C:\Users\marti\.venvs\palabra_zoom\Scripts\python.exe" palabra_zoom.py --input-device <input-id> --output-device <output-id>
```

## 8. Zoom meeting flow

1. Host schedules the meeting with Language Interpretation enabled.
2. Host assigns `maveprg+es.de.interpreter@gmail.com` as the Spanish/German interpreter.
3. Bot account joins with the Zoom desktop client and is signed in as that email.
4. Host starts interpretation.
5. Bot is recognized by Zoom as the interpreter for the German channel.
6. Start the bridge after the bot's Zoom speaker/microphone devices are set.

Guests should then select German in Zoom's Interpretation menu.

## 9. Internal architecture

The bridge is split into small modules so the current virtual-cable mode can stay
as a fallback while SDK-based multilang support is added:

- `palabra_zoom.py` is the CLI entrypoint and lifecycle orchestrator.
- `modules/palabra_client.py` owns Palabra session, websocket, task, and message helpers.
- `modules/palabra_agent.py` models one Palabra worker for one target language. The
  current cable mode creates one agent; multilang mode can create one per target.
- `modules/transports/cable.py` contains the existing VB-Cable audio path and diagnostics.
- `modules/transports/base.py` defines the minimal transport shape for cable and future
  Zoom SDK transports.
- `modules/audio_utils.py`, `modules/debug_recording.py`, `modules/config.py`,
  `modules/zoom_desktop.py`, and `modules/system_power.py` hold shared helpers
  that used to live in the entrypoint.

Phase II should flesh out the `modules/transports/zoom_sdk.py` implementation, then have the
orchestrator select either the cable transport or the SDK transport from config.

## 10. Zoom SDK probe MVP

The first SDK mode is `sdk-probe`. It does not start Palabra and does not change
the virtual-cable fallback. Its purpose is to prove that a Zoom Meeting SDK raw
audio adapter can join a meeting and deliver PCM audio into this app.

Dry-run the probe recorder without Zoom:

```powershell
& "C:\Users\marti\.venvs\palabra_zoom\Scripts\python.exe" palabra_zoom.py --mode sdk-probe --zoom-sdk-dry-run --zoom-sdk-probe-seconds 3
```

That writes a generated test tone to `debug/zoom_sdk_probe.wav`.

For a real SDK probe, configure `[zoom_sdk]` in `config.toml`:

```toml
[app]
mode = "sdk-probe"

[zoom_sdk]
meeting_number = "..."
password = "..."
display_name = "Palabra SDK Probe"
adapter_module = "your_zoom_sdk_adapter"
output_wav = "debug/zoom_sdk_probe.wav"
sample_rate = 48000
channels = 1
```

The adapter module must expose either `run_probe(settings, audio_callback)` or
`create_adapter(settings, audio_callback).run_probe()`. The callback receives
`pcm_s16le` bytes plus sample rate and channel count. The generated Meeting SDK
JWT is available to the adapter as `settings.auth_token`; do not log or print it.
Once this probe records clean meeting audio, the next step is wiring that source
into one `PalabraAgent`.
