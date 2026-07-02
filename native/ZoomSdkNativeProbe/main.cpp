#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <iostream>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include "auth_service_interface.h"
#include "network_connection_handler_interface.h"
#include "zoom_sdk.h"

namespace {

using ZOOM_SDK_NAMESPACE::CleanUPSDK;
using ZOOM_SDK_NAMESPACE::AuthContext;
using ZOOM_SDK_NAMESPACE::AuthResult;
using ZOOM_SDK_NAMESPACE::AUTHRET_SUCCESS;
using ZOOM_SDK_NAMESPACE::IAccountInfo;
using ZOOM_SDK_NAMESPACE::IAuthService;
using ZOOM_SDK_NAMESPACE::IAuthServiceEvent;
using ZOOM_SDK_NAMESPACE::INetworkConnectionHandler;
using ZOOM_SDK_NAMESPACE::INetworkConnectionHelper;
using ZOOM_SDK_NAMESPACE::IProxySettingHandler;
using ZOOM_SDK_NAMESPACE::ISSLCertVerificationHandler;
using ZOOM_SDK_NAMESPACE::InitParam;
using ZOOM_SDK_NAMESPACE::LANGUAGE_English;
using ZOOM_SDK_NAMESPACE::LoginFailReason;
using ZOOM_SDK_NAMESPACE::LOGINSTATUS;
using ZOOM_SDK_NAMESPACE::SDKERR_SUCCESS;
using ZOOM_SDK_NAMESPACE::SDKError;
using ZOOM_SDK_NAMESPACE::ZoomSDKRawDataMemoryModeHeap;
#if defined(WIN32)
using ZOOM_SDK_NAMESPACE::SDKNotificationServiceError;
using ZOOM_SDK_NAMESPACE::SDKNotificationServiceStatus;
#endif

typedef const zchar_t* (*GetSDKVersionFn)();
typedef SDKError (*InitSDKFn)(InitParam&);
typedef SDKError (*CleanUPSDKFn)();
typedef SDKError (*CreateAuthServiceFn)(IAuthService**);
typedef SDKError (*DestroyAuthServiceFn)(IAuthService*);
typedef SDKError (*CreateNetworkConnectionHelperFn)(INetworkConnectionHelper**);
typedef SDKError (*DestroyNetworkConnectionHelperFn)(INetworkConnectionHelper*);

std::string JsonEscape(const std::string& value) {
    std::ostringstream out;
    for (char ch : value) {
        switch (ch) {
        case '\\': out << "\\\\"; break;
        case '"': out << "\\\""; break;
        case '\n': out << "\\n"; break;
        case '\r': out << "\\r"; break;
        case '\t': out << "\\t"; break;
        default:
            if (static_cast<unsigned char>(ch) < 0x20) {
                out << "\\u00";
                const char* hex = "0123456789abcdef";
                out << hex[(ch >> 4) & 0xf] << hex[ch & 0xf];
            } else {
                out << ch;
            }
            break;
        }
    }
    return out.str();
}

void WriteMessage(const char* type, const std::string& message) {
    std::cout << "{\"type\":\"" << type << "\",\"message\":\""
              << JsonEscape(message) << "\"}" << std::endl;
    std::cout.flush();
}

void WriteStatus(const std::string& message) {
    WriteMessage("status", message);
}

void WriteError(const std::string& message) {
    WriteMessage("error", message);
}

void WriteDone() {
    std::cout << "{\"type\":\"done\"}" << std::endl;
    std::cout.flush();
}

std::wstring Utf8ToWide(const std::string& value) {
    if (value.empty()) {
        return L"";
    }
    int size = MultiByteToWideChar(CP_UTF8, 0, value.c_str(), -1, nullptr, 0);
    if (size <= 0) {
        return L"";
    }
    std::wstring result(static_cast<size_t>(size - 1), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, value.c_str(), -1, &result[0], size);
    return result;
}

std::string WideToUtf8(const std::wstring& value) {
    if (value.empty()) {
        return "";
    }
    int size = WideCharToMultiByte(CP_UTF8, 0, value.c_str(), -1, nullptr, 0, nullptr, nullptr);
    if (size <= 0) {
        return "";
    }
    std::string result(static_cast<size_t>(size - 1), '\0');
    WideCharToMultiByte(CP_UTF8, 0, value.c_str(), -1, &result[0], size, nullptr, nullptr);
    return result;
}

bool Exists(const std::wstring& path) {
    DWORD attrs = GetFileAttributesW(path.c_str());
    return attrs != INVALID_FILE_ATTRIBUTES;
}

std::wstring JoinPath(const std::wstring& left, const std::wstring& right) {
    if (left.empty()) {
        return right;
    }
    wchar_t last = left[left.size() - 1];
    if (last == L'\\' || last == L'/') {
        return left + right;
    }
    return left + L"\\" + right;
}

std::wstring EnvWide(const wchar_t* name) {
    DWORD needed = GetEnvironmentVariableW(name, nullptr, 0);
    if (needed == 0) {
        return L"";
    }
    std::wstring value(needed, L'\0');
    GetEnvironmentVariableW(name, &value[0], needed);
    if (!value.empty() && value[value.size() - 1] == L'\0') {
        value.resize(value.size() - 1);
    }
    return value;
}

std::wstring ArgValue(const std::vector<std::wstring>& args, const wchar_t* name) {
    for (size_t index = 0; index + 1 < args.size(); ++index) {
        if (args[index] == name) {
            return args[index + 1];
        }
    }
    return L"";
}

std::wstring QuoteArg(const std::wstring& value) {
    std::wstring quoted = L"\"";
    for (wchar_t ch : value) {
        if (ch == L'"') {
            quoted += L"\\\"";
        } else {
            quoted += ch;
        }
    }
    quoted += L"\"";
    return quoted;
}

bool HasArg(const std::vector<std::wstring>& args, const wchar_t* name) {
    for (const std::wstring& arg : args) {
        if (arg == name) {
            return true;
        }
    }
    return false;
}

int ArgIntValue(const std::vector<std::wstring>& args, const wchar_t* name, int fallback) {
    std::wstring raw = ArgValue(args, name);
    if (raw.empty()) {
        return fallback;
    }
    wchar_t* end = nullptr;
    long value = std::wcstol(raw.c_str(), &end, 10);
    if (end == raw.c_str() || value <= 0 || value > 3600) {
        return fallback;
    }
    return static_cast<int>(value);
}

std::wstring ResolveSdkRoot(const std::vector<std::wstring>& args) {
    std::wstring root = ArgValue(args, L"--sdk-root");
    if (root.empty()) {
        root = EnvWide(L"ZOOM_MEETING_SDK_ROOT");
    }
    if (root.empty()) {
        root = EnvWide(L"ZOOM_SDK_ROOT");
    }
    return root;
}

std::wstring ResolveAuthToken() {
    return EnvWide(L"ZOOM_SDK_AUTH_TOKEN");
}

std::wstring ResolveSdkBin(const std::wstring& root) {
    if (root.empty()) {
        return L"";
    }
    std::wstring directBin = JoinPath(root, L"bin");
    if (Exists(JoinPath(directBin, L"sdk.dll"))) {
        return directBin;
    }
    std::wstring x64Bin = JoinPath(JoinPath(root, L"x64"), L"bin");
    if (Exists(JoinPath(x64Bin, L"sdk.dll"))) {
        return x64Bin;
    }
    return directBin;
}

class LoadedSdk {
public:
    explicit LoadedSdk(const std::wstring& sdkBin) : module_(nullptr) {
        SetDllDirectoryW(sdkBin.c_str());
        module_ = LoadLibraryW(JoinPath(sdkBin, L"sdk.dll").c_str());
    }

    ~LoadedSdk() {
        if (module_) {
            FreeLibrary(module_);
        }
    }

    bool ok() const {
        return module_ != nullptr;
    }

    std::string lastError() const {
        DWORD error = GetLastError();
        return "LoadLibrary failed with Windows error " + std::to_string(error) + ".";
    }

    FARPROC proc(const char* name) const {
        return module_ ? GetProcAddress(module_, name) : nullptr;
    }

private:
    HMODULE module_;
};

class AuthEvent : public IAuthServiceEvent {
public:
    AuthEvent() : done_(false), result_(ZOOM_SDK_NAMESPACE::AUTHRET_NONE) {}

    void onAuthenticationReturn(AuthResult ret) override {
        result_ = ret;
        done_.store(true);
        WriteStatus("authentication callback result=" + std::to_string(static_cast<int>(ret)));
    }

    void onLoginReturnWithReason(LOGINSTATUS, IAccountInfo*, LoginFailReason) override {}
    void onLogout() override {}
    void onZoomIdentityExpired() override {
        WriteStatus("Zoom identity expired");
    }
    void onZoomAuthIdentityExpired() override {
        WriteStatus("Zoom auth identity expired");
    }
#if defined(WIN32)
    void onNotificationServiceStatus(SDKNotificationServiceStatus status, SDKNotificationServiceError error) override {
        WriteStatus(
            "notification service status=" + std::to_string(static_cast<int>(status)) +
            " error=" + std::to_string(static_cast<int>(error)));
    }
#endif

    const std::atomic<bool>& done() const {
        return done_;
    }

    AuthResult result() const {
        return result_;
    }

private:
    std::atomic<bool> done_;
    AuthResult result_;
};

class NetworkEvent : public INetworkConnectionHandler {
public:
    NetworkEvent() : proxyDone_(false) {}

    void onProxyDetectComplete() override {
        proxyDone_.store(true);
        WriteStatus("proxy detection completed");
    }

    void onProxySettingNotification(IProxySettingHandler* handler) override {
        WriteStatus("proxy credentials requested; cancelling interactive proxy prompt");
        if (handler) {
            handler->Cancel();
        }
    }

    void onSSLCertVerifyNotification(ISSLCertVerificationHandler* handler) override {
        WriteStatus("SSL certificate verification requested; cancelling SDK auth probe");
        if (handler) {
            handler->Cancel();
        }
    }

    const std::atomic<bool>& proxyDone() const {
        return proxyDone_;
    }

private:
    std::atomic<bool> proxyDone_;
};

int PrintHelp() {
    std::cerr
        << "ZoomSdkNativeProbe\n"
        << "  --sdk-info          Load sdk.dll and print the SDK version.\n"
        << "  --sdk-init          Load sdk.dll and call InitSDK/CleanUPSDK.\n"
        << "  --sdk-auth          Initialize SDK and authenticate with ZOOM_SDK_AUTH_TOKEN.\n"
        << "  --sdk-root PATH     SDK root, e.g. C:\\dev\\zoom-sdk-windows.\n"
        << "  --timeout SECONDS   SDK call timeout for probe modes. Default: 30.\n"
        << "\n"
        << "The SDK root can also come from ZOOM_MEETING_SDK_ROOT.\n";
    return 0;
}

int RunChildWithWatchdog(const std::vector<std::wstring>& args, const wchar_t* publicMode, const wchar_t* childMode) {
    wchar_t exePath[MAX_PATH] = {0};
    DWORD pathLen = GetModuleFileNameW(nullptr, exePath, MAX_PATH);
    if (pathLen == 0 || pathLen >= MAX_PATH) {
        WriteError("Could not resolve helper executable path.");
        return 2;
    }

    std::wstring root = ResolveSdkRoot(args);
    int timeoutSeconds = ArgIntValue(args, L"--timeout", 30);
    std::wstring commandLine = QuoteArg(exePath) + L" ";
    commandLine += childMode;
    if (!root.empty()) {
        commandLine += L" --sdk-root " + QuoteArg(root);
    }
    commandLine += L" --timeout " + std::to_wstring(timeoutSeconds);
    commandLine += L" --skip-cleanup";

    STARTUPINFOW startupInfo = {};
    startupInfo.cb = sizeof(startupInfo);
    PROCESS_INFORMATION processInfo = {};
    std::vector<wchar_t> mutableCommand(commandLine.begin(), commandLine.end());
    mutableCommand.push_back(L'\0');

    WriteStatus("starting child probe");
    BOOL created = CreateProcessW(
        nullptr,
        mutableCommand.data(),
        nullptr,
        nullptr,
        TRUE,
        0,
        nullptr,
        nullptr,
        &startupInfo,
        &processInfo);

    if (!created) {
        WriteError("CreateProcess failed with Windows error " + std::to_string(GetLastError()) + ".");
        return 2;
    }

    DWORD waitResult = WaitForSingleObject(processInfo.hProcess, static_cast<DWORD>(timeoutSeconds + 5) * 1000);
    if (waitResult == WAIT_TIMEOUT) {
        TerminateProcess(processInfo.hProcess, 3);
        WaitForSingleObject(processInfo.hProcess, 5000);
        CloseHandle(processInfo.hThread);
        CloseHandle(processInfo.hProcess);
        WriteError(WideToUtf8(publicMode) + " child probe did not exit within " + std::to_string(timeoutSeconds + 5) + " seconds.");
        return 3;
    }

    DWORD exitCode = 1;
    GetExitCodeProcess(processInfo.hProcess, &exitCode);
    CloseHandle(processInfo.hThread);
    CloseHandle(processInfo.hProcess);
    return static_cast<int>(exitCode);
}

int RunInitWithWatchdog(const std::vector<std::wstring>& args) {
    return RunChildWithWatchdog(args, L"--sdk-init", L"--sdk-init-child");
}

int RunAuthWithWatchdog(const std::vector<std::wstring>& args) {
    return RunChildWithWatchdog(args, L"--sdk-auth", L"--sdk-auth-child");
}

bool WaitForFlag(const std::atomic<bool>& done, int timeoutSeconds) {
    DWORD start = GetTickCount();
    DWORD timeoutMs = static_cast<DWORD>(timeoutSeconds) * 1000;
    MSG msg;
    while (!done.load()) {
        while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        if (GetTickCount() - start > timeoutMs) {
            return false;
        }
        Sleep(10);
    }
    return true;
}

bool InitializeSdk(const std::vector<std::wstring>& args, LoadedSdk& sdk) {
    InitSDKFn initSdk = reinterpret_cast<InitSDKFn>(sdk.proc("InitSDK"));
    if (!initSdk) {
        WriteError("sdk.dll did not expose InitSDK.");
        return false;
    }

    InitParam initParam;
    initParam.strWebDomain = L"https://zoom.us";
    initParam.strSupportUrl = L"https://zoom.us";
    initParam.emLanguageID = LANGUAGE_English;
    initParam.hResInstance = GetModuleHandleW(nullptr);
    initParam.enableLogByDefault = true;
    initParam.enableGenerateDump = true;
    initParam.rawdataOpts.audioRawdataMemoryMode = ZoomSDKRawDataMemoryModeHeap;

    WriteStatus("calling InitSDK");
    SDKError initResult = initSdk(initParam);

    if (initResult != SDKERR_SUCCESS) {
        WriteError("InitSDK failed with SDKError " + std::to_string(static_cast<int>(initResult)) + ".");
        return false;
    }
    WriteStatus("InitSDK succeeded");
    return true;
}

int RunSdkProbe(const std::vector<std::wstring>& args, bool initialize, bool authenticate) {
    std::wstring root = ResolveSdkRoot(args);
    if (root.empty()) {
        WriteError("Missing SDK root. Set zoom_sdk.sdk_root, pass --sdk-root, or set ZOOM_MEETING_SDK_ROOT.");
        return 2;
    }

    std::wstring sdkBin = ResolveSdkBin(root);
    std::wstring sdkDll = JoinPath(sdkBin, L"sdk.dll");
    if (!Exists(sdkDll)) {
        WriteError("Could not find sdk.dll at " + WideToUtf8(sdkDll) + ".");
        return 2;
    }

    WriteStatus("loading Zoom SDK from " + WideToUtf8(sdkDll));
    LoadedSdk sdk(sdkBin);
    if (!sdk.ok()) {
        WriteError(sdk.lastError());
        return 2;
    }

    GetSDKVersionFn getVersion = reinterpret_cast<GetSDKVersionFn>(sdk.proc("GetSDKVersion"));
    if (!getVersion) {
        WriteError("sdk.dll did not expose GetSDKVersion.");
        return 2;
    }

    const zchar_t* version = getVersion();
    WriteStatus("Zoom SDK version " + WideToUtf8(version ? version : L""));

    if (initialize || authenticate) {
        CleanUPSDKFn cleanupSdk = reinterpret_cast<CleanUPSDKFn>(sdk.proc("CleanUPSDK"));
        if (!cleanupSdk) {
            WriteError("sdk.dll did not expose CleanUPSDK.");
            return 2;
        }

        if (!InitializeSdk(args, sdk)) {
            return 2;
        }

        if (authenticate) {
            CreateAuthServiceFn createAuthService = reinterpret_cast<CreateAuthServiceFn>(sdk.proc("CreateAuthService"));
            DestroyAuthServiceFn destroyAuthService = reinterpret_cast<DestroyAuthServiceFn>(sdk.proc("DestroyAuthService"));
            CreateNetworkConnectionHelperFn createNetworkHelper = reinterpret_cast<CreateNetworkConnectionHelperFn>(sdk.proc("CreateNetworkConnectionHelper"));
            DestroyNetworkConnectionHelperFn destroyNetworkHelper = reinterpret_cast<DestroyNetworkConnectionHelperFn>(sdk.proc("DestroyNetworkConnectionHelper"));
            if (!createAuthService || !destroyAuthService) {
                WriteError("sdk.dll did not expose CreateAuthService/DestroyAuthService.");
                return 2;
            }
            if (!createNetworkHelper || !destroyNetworkHelper) {
                WriteError("sdk.dll did not expose CreateNetworkConnectionHelper/DestroyNetworkConnectionHelper.");
                return 2;
            }

            std::wstring token = ResolveAuthToken();
            if (token.empty()) {
                WriteError("ZOOM_SDK_AUTH_TOKEN is required for --sdk-auth.");
                return 2;
            }

            int timeoutSeconds = ArgIntValue(args, L"--timeout", 30);
            int proxyTimeoutSeconds = std::min(10, std::max(1, timeoutSeconds / 3));
            INetworkConnectionHelper* networkHelper = nullptr;
            NetworkEvent networkEvent;
            SDKError networkResult = createNetworkHelper(&networkHelper);
            if (networkResult == SDKERR_SUCCESS && networkHelper) {
                SDKError registerResult = networkHelper->RegisterNetworkConnectionHandler(&networkEvent);
                if (registerResult == SDKERR_SUCCESS) {
                    WriteStatus("waiting for proxy detection");
                    if (!WaitForFlag(networkEvent.proxyDone(), proxyTimeoutSeconds)) {
                        WriteStatus("proxy detection did not complete within " + std::to_string(proxyTimeoutSeconds) + " seconds; continuing");
                    }
                } else {
                    WriteStatus("RegisterNetworkConnectionHandler returned SDKError " + std::to_string(static_cast<int>(registerResult)));
                }
            } else {
                WriteStatus("CreateNetworkConnectionHelper returned SDKError " + std::to_string(static_cast<int>(networkResult)));
            }

            IAuthService* authService = nullptr;
            SDKError createResult = createAuthService(&authService);
            if (createResult != SDKERR_SUCCESS || !authService) {
                WriteError("CreateAuthService failed with SDKError " + std::to_string(static_cast<int>(createResult)) + ".");
                if (networkHelper) {
                    networkHelper->UnRegisterNetworkConnectionHandler();
                    destroyNetworkHelper(networkHelper);
                }
                return 2;
            }

            AuthEvent authEvent;
            SDKError setEventResult = authService->SetEvent(&authEvent);
            if (setEventResult != SDKERR_SUCCESS) {
                WriteError("IAuthService::SetEvent failed with SDKError " + std::to_string(static_cast<int>(setEventResult)) + ".");
                destroyAuthService(authService);
                if (networkHelper) {
                    networkHelper->UnRegisterNetworkConnectionHandler();
                    destroyNetworkHelper(networkHelper);
                }
                return 2;
            }

            AuthContext authContext;
            authContext.jwt_token = token.c_str();
            WriteStatus("calling SDKAuth");
            SDKError authCallResult = authService->SDKAuth(authContext);
            if (authCallResult != SDKERR_SUCCESS) {
                WriteError("SDKAuth call failed with SDKError " + std::to_string(static_cast<int>(authCallResult)) + ".");
                destroyAuthService(authService);
                return 2;
            }

            int authTimeoutSeconds = std::max(1, timeoutSeconds - proxyTimeoutSeconds);
            if (!WaitForFlag(authEvent.done(), authTimeoutSeconds)) {
                WriteError("SDKAuth callback did not arrive within " + std::to_string(authTimeoutSeconds) + " seconds.");
                destroyAuthService(authService);
                if (networkHelper) {
                    networkHelper->UnRegisterNetworkConnectionHandler();
                    destroyNetworkHelper(networkHelper);
                }
                return 3;
            }

            AuthResult authResult = authEvent.result();
            if (authResult != AUTHRET_SUCCESS) {
                WriteError("SDK authentication failed with AuthResult " + std::to_string(static_cast<int>(authResult)) + ".");
                destroyAuthService(authService);
                if (networkHelper) {
                    networkHelper->UnRegisterNetworkConnectionHandler();
                    destroyNetworkHelper(networkHelper);
                }
                return 2;
            }

            WriteStatus("SDK authentication succeeded");
            destroyAuthService(authService);
            if (networkHelper) {
                networkHelper->UnRegisterNetworkConnectionHandler();
                destroyNetworkHelper(networkHelper);
            }
        }

        if (HasArg(args, L"--skip-cleanup")) {
            WriteDone();
            ExitProcess(0);
        }
        cleanupSdk();
        WriteStatus("CleanUPSDK completed");
    }

    WriteDone();
    return 0;
}

} // namespace

int wmain(int argc, wchar_t* argv[]) {
    std::vector<std::wstring> args;
    for (int index = 1; index < argc; ++index) {
        args.push_back(argv[index]);
    }

    if (HasArg(args, L"--help") || HasArg(args, L"-h")) {
        return PrintHelp();
    }

    if (HasArg(args, L"--sdk-init")) {
        return RunInitWithWatchdog(args);
    }

    if (HasArg(args, L"--sdk-auth")) {
        return RunAuthWithWatchdog(args);
    }

    if (HasArg(args, L"--sdk-init-child")) {
        return RunSdkProbe(args, true, false);
    }

    if (HasArg(args, L"--sdk-auth-child")) {
        return RunSdkProbe(args, false, true);
    }

    if (HasArg(args, L"--sdk-info") || args.empty()) {
        return RunSdkProbe(args, false, false);
    }

    WriteError("Unknown mode. Use --sdk-info, --sdk-init, --sdk-auth, or --help.");
    return 2;
}
