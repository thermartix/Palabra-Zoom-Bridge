# ZoomSdkProbe

Small native helper executable for the Python `modules.zoom_sdk_process_adapter`.

Current state:

- `--simulate` works now and emits generated PCM using the JSON-lines protocol.
- Real Zoom Meeting SDK mode is intentionally a stub until the Windows Meeting
  SDK files and raw-audio callback wrapper are added.

Build with Visual Studio Build Tools:

```powershell
& "C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\MSBuild\Current\Bin\MSBuild.exe" native\ZoomSdkProbe\ZoomSdkProbe.csproj /p:Configuration=Release
```

Test through Python:

```powershell
& "C:\dev\.venvs\palabra_zoom\Scripts\python.exe" palabra_zoom.py --mode sdk-probe --zoom-sdk-meeting-number 123456789 --zoom-sdk-probe-seconds 1 --zoom-sdk-adapter-module modules.zoom_sdk_process_adapter --zoom-sdk-adapter-command native\ZoomSdkProbe\bin\Release\ZoomSdkProbe.exe --zoom-sdk-adapter-args --simulate
```

The `--zoom-sdk-adapter-args` option must be last because every value after it is
passed through to the helper executable.

Real SDK mode must initialize the Zoom Meeting SDK with `ZOOM_SDK_AUTH_TOKEN`,
join the meeting, subscribe to raw audio callbacks, and print JSON lines to
stdout as documented in the main README.
