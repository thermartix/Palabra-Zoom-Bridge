# ZoomSdkNativeProbe

Small C++ helper for loading the installed Zoom Meeting SDK for Windows.

Current state:

- `--sdk-info` loads `sdk.dll` and prints the SDK version.
- `--sdk-init` starts a guarded child process, loads `sdk.dll`, and calls
  `InitSDK`. This is verified with Zoom Meeting SDK `7.1.0 (41845)`. The child
  skips immediate cleanup because `CleanUPSDK` currently does not return
  reliably in this smoke-test shape.
- Meeting join, raw audio subscription, and interpreter/talkback output are the
  next steps.

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
```
