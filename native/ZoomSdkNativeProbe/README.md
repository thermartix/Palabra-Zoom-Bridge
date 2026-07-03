# ZoomSdkNativeProbe

Small C++ helper for loading the installed Zoom Meeting SDK for Windows.

Current state:

- `--sdk-info` loads `sdk.dll` and prints the SDK version.
- `--sdk-init` starts a guarded child process, loads `sdk.dll`, and calls
  `InitSDK`. This is verified with Zoom Meeting SDK `7.1.0 (41845)`. The child
  skips immediate cleanup because `CleanUPSDK` currently does not return
  reliably in this smoke-test shape.
- `--sdk-auth` initializes the SDK, waits for proxy detection, and authenticates
  with `ZOOM_SDK_AUTH_TOKEN`.
- `--sdk-join` authenticates, creates the meeting service, and attempts to join
  `ZOOM_SDK_MEETING_NUMBER`. Waiting room and waiting-for-host count as usable
  join states for this probe.
- `--custom-ui` initializes the SDK in custom UI mode. This avoids the default
  Zoom meeting UI path for probe joins, though the SDK may still create internal
  native windows.
- `--play-sting` registers a virtual mic and attempts to send a short original
  test sting after joining VoIP. The probe now registers the external audio
  source before calling `Join`, joins with audio enabled, and unmutes the actual
  self participant id. In the current SDK test, Zoom accepts
  `setExternalAudioSource`, initializes the virtual mic during connection, then
  uninitializes it before `in_meeting` and never calls the raw mic start callback.
- `--force-mic-send` is a diagnostic companion for `--play-sting`; it tries to
  send as soon as the SDK exposes the virtual mic sender. On this machine the SDK
  rejects that early send with `SDKERR_UNKNOWN`, confirming that the raw mic has
  not reached its accepted send state.
- `--raw-audio-diagnostics` logs the current meeting's raw recording, raw
  archiving, interpretation, talkback, and raw-audio subscribe state. Diagnostic
  runs join VoIP and unmute the real self participant before checking raw-audio
  state. In the live no-interpretation test meeting on this machine, Zoom
  reported no raw recording permission, raw-audio subscribe was denied with
  `SDKERR_NO_PERMISSION`, raw archiving returned
  `SDKERR_MEETING_DONT_SUPPORT_FEATURE`, interpretation was disabled, and
  talkback was supported.
- `--raw-audio-probe` is the first SDK input-path probe for Palabra. It joins
  VoIP, attempts Zoom's raw recording/raw archiving gates, subscribes to mixed
  meeting raw audio, and emits JSON `audio` messages with base64 PCM for the
  Python process adapter. Pair it with Python's `--zoom-sdk-play-local` to hear
  the meeting audio locally before wiring Palabra.
- `--talkback-sting` probes the SDK's direct talkback PCM path. It creates a
  talkback channel, invites the first other participant who supports talkback,
  and sends a short original 48 kHz mono PCM test sting with
  `SendAudioDataToChannel`. This is not the final interpretation-channel path,
  but it is the next viable SDK audio-output probe because it does not depend on
  raw recording permission or the virtual mic start callback.
- `--list-sdk-mics`, `--sdk-mic NAME`, and `--hold-seconds N` are diagnostics
  for the fallback Windows microphone-device path. They confirmed that the SDK
  can see and select `CABLE-A Output`, but this does not solve multi-agent audio
  because multiple SDK agents would still share the same Windows microphone
  device.
- `ZOOM_SDK_APP_PRIVILEGE_TOKEN` can be set when Zoom requires a separate app
  privilege token. The SDK auth JWT is not used for that join field.
- Raw audio subscription and interpreter/talkback output are the next steps after
  the virtual mic start path is resolved.

Build:

```powershell
& "C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\MSBuild\Current\Bin\MSBuild.exe" native\ZoomSdkNativeProbe\ZoomSdkNativeProbe.vcxproj /p:Configuration=Release /p:Platform=x64 /p:ZoomMeetingSdkRoot=C:\dev\zoom-sdk-windows
```

Test directly:

```powershell
& native\ZoomSdkNativeProbe\bin\x64\Release\ZoomSdkNativeProbe.exe --sdk-info --sdk-root C:\dev\zoom-sdk-windows
& native\ZoomSdkNativeProbe\bin\x64\Release\ZoomSdkNativeProbe.exe --sdk-init --sdk-root C:\dev\zoom-sdk-windows --timeout 8
```

Test through Python:

```powershell
& "C:\Users\marti\.venvs\palabra_zoom\Scripts\python.exe" palabra_zoom.py --mode sdk-probe --zoom-sdk-meeting-number 123456789 --zoom-sdk-probe-seconds 1 --zoom-sdk-root C:\dev\zoom-sdk-windows --zoom-sdk-adapter-module modules.zoom_sdk_process_adapter --zoom-sdk-adapter-command native\ZoomSdkNativeProbe\bin\x64\Release\ZoomSdkNativeProbe.exe --zoom-sdk-adapter-args --sdk-info
& "C:\Users\marti\.venvs\palabra_zoom\Scripts\python.exe" palabra_zoom.py --mode sdk-probe --zoom-sdk-meeting-number 123456789 --zoom-sdk-probe-seconds 1 --zoom-sdk-root C:\dev\zoom-sdk-windows --zoom-sdk-adapter-module modules.zoom_sdk_process_adapter --zoom-sdk-adapter-command native\ZoomSdkNativeProbe\bin\x64\Release\ZoomSdkNativeProbe.exe --zoom-sdk-adapter-args --sdk-auth --timeout 60
& "C:\Users\marti\.venvs\palabra_zoom\Scripts\python.exe" palabra_zoom.py --mode sdk-probe --zoom-sdk-meeting-number 123456789 --zoom-sdk-probe-seconds 1 --zoom-sdk-root C:\dev\zoom-sdk-windows --zoom-sdk-adapter-module modules.zoom_sdk_process_adapter --zoom-sdk-adapter-command native\ZoomSdkNativeProbe\bin\x64\Release\ZoomSdkNativeProbe.exe --zoom-sdk-adapter-args --sdk-join --timeout 60
& "C:\Users\marti\.venvs\palabra_zoom\Scripts\python.exe" palabra_zoom.py --mode sdk-probe --zoom-sdk-meeting-number 123456789 --zoom-sdk-probe-seconds 1 --zoom-sdk-root C:\dev\zoom-sdk-windows --zoom-sdk-adapter-module modules.zoom_sdk_process_adapter --zoom-sdk-adapter-command native\ZoomSdkNativeProbe\bin\x64\Release\ZoomSdkNativeProbe.exe --zoom-sdk-adapter-args --sdk-join --play-sting --custom-ui --timeout 120
& "C:\Users\marti\.venvs\palabra_zoom\Scripts\python.exe" palabra_zoom.py --mode sdk-probe --zoom-sdk-meeting-number 123456789 --zoom-sdk-probe-seconds 1 --zoom-sdk-root C:\dev\zoom-sdk-windows --zoom-sdk-adapter-module modules.zoom_sdk_process_adapter --zoom-sdk-adapter-command native\ZoomSdkNativeProbe\bin\x64\Release\ZoomSdkNativeProbe.exe --zoom-sdk-adapter-args --sdk-join --custom-ui --raw-audio-diagnostics --timeout 120
& "C:\Users\marti\.venvs\palabra_zoom\Scripts\python.exe" palabra_zoom.py --mode sdk-probe --zoom-sdk-meeting-number 123456789 --zoom-sdk-probe-seconds 1 --zoom-sdk-root C:\dev\zoom-sdk-windows --zoom-sdk-adapter-module modules.zoom_sdk_process_adapter --zoom-sdk-adapter-command native\ZoomSdkNativeProbe\bin\x64\Release\ZoomSdkNativeProbe.exe --zoom-sdk-adapter-args --sdk-join --custom-ui --talkback-sting --timeout 120
& "C:\Users\marti\.venvs\palabra_zoom\Scripts\python.exe" palabra_zoom.py --mode sdk-probe --zoom-sdk-play-local --zoom-sdk-meeting-number 123456789 --zoom-sdk-probe-seconds 10 --zoom-sdk-root C:\dev\zoom-sdk-windows --zoom-sdk-adapter-module modules.zoom_sdk_process_adapter --zoom-sdk-adapter-command native\ZoomSdkNativeProbe\bin\x64\Release\ZoomSdkNativeProbe.exe --zoom-sdk-adapter-args --sdk-join --custom-ui --raw-audio-probe --timeout 120
```
