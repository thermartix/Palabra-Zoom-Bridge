#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <cmath>
#include <iostream>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include "auth_service_interface.h"
#include "meeting_service_interface.h"
#include "meeting_service_components/meeting_audio_interface.h"
#include "meeting_service_components/meeting_participants_ctrl_interface.h"
#include "network_connection_handler_interface.h"
#include "rawdata/rawdata_audio_helper_interface.h"
#include "rawdata/zoom_rawdata_api.h"
#include "zoom_sdk.h"

namespace {

using ZOOM_SDK_NAMESPACE::CleanUPSDK;
using ZOOM_SDK_NAMESPACE::AuthContext;
using ZOOM_SDK_NAMESPACE::AuthResult;
using ZOOM_SDK_NAMESPACE::AUTHRET_SUCCESS;
using ZOOM_SDK_NAMESPACE::IAccountInfo;
using ZOOM_SDK_NAMESPACE::IAuthService;
using ZOOM_SDK_NAMESPACE::IAuthServiceEvent;
using ZOOM_SDK_NAMESPACE::IMeetingAppSignalHandler;
using ZOOM_SDK_NAMESPACE::IMeetingAudioController;
using ZOOM_SDK_NAMESPACE::IMeetingParticipantsController;
using ZOOM_SDK_NAMESPACE::IMeetingService;
using ZOOM_SDK_NAMESPACE::IMeetingServiceEvent;
using ZOOM_SDK_NAMESPACE::INetworkConnectionHandler;
using ZOOM_SDK_NAMESPACE::INetworkConnectionHelper;
using ZOOM_SDK_NAMESPACE::IProxySettingHandler;
using ZOOM_SDK_NAMESPACE::ISSLCertVerificationHandler;
using ZOOM_SDK_NAMESPACE::IZoomSDKAudioRawDataHelper;
using ZOOM_SDK_NAMESPACE::IZoomSDKAudioRawDataSender;
using ZOOM_SDK_NAMESPACE::IZoomSDKVirtualAudioMicEvent;
using ZOOM_SDK_NAMESPACE::InitParam;
using ZOOM_SDK_NAMESPACE::IUserInfo;
using ZOOM_SDK_NAMESPACE::LANGUAGE_English;
using ZOOM_SDK_NAMESPACE::LoginFailReason;
using ZOOM_SDK_NAMESPACE::LOGINSTATUS;
using ZOOM_SDK_NAMESPACE::ConnectionQuality;
using ZOOM_SDK_NAMESPACE::JoinParam;
using ZOOM_SDK_NAMESPACE::JoinParam4WithoutLogin;
using ZOOM_SDK_NAMESPACE::MEETING_STATUS_ENDED;
using ZOOM_SDK_NAMESPACE::MEETING_STATUS_FAILED;
using ZOOM_SDK_NAMESPACE::MEETING_STATUS_INMEETING;
using ZOOM_SDK_NAMESPACE::MEETING_STATUS_IN_WAITING_ROOM;
using ZOOM_SDK_NAMESPACE::MEETING_STATUS_WAITINGFORHOST;
using ZOOM_SDK_NAMESPACE::MeetingComponentType;
using ZOOM_SDK_NAMESPACE::MeetingParameter;
using ZOOM_SDK_NAMESPACE::MeetingStatus;
using ZOOM_SDK_NAMESPACE::SDK_UT_WITHOUT_LOGIN;
using ZOOM_SDK_NAMESPACE::SDKERR_SUCCESS;
using ZOOM_SDK_NAMESPACE::SDKError;
using ZOOM_SDK_NAMESPACE::StatisticsWarningType;
using ZOOM_SDK_NAMESPACE::ZoomSDKAudioChannel_Mono;
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
typedef SDKError (*CreateMeetingServiceFn)(IMeetingService**);
typedef SDKError (*DestroyMeetingServiceFn)(IMeetingService*);
typedef SDKError (*CreateNetworkConnectionHelperFn)(INetworkConnectionHelper**);
typedef SDKError (*DestroyNetworkConnectionHelperFn)(INetworkConnectionHelper*);
typedef IZoomSDKAudioRawDataHelper* (*GetAudioRawdataHelperFn)();

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

void FastExit(unsigned int code) {
    TerminateProcess(GetCurrentProcess(), code);
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

bool HasAnyArg(const std::vector<std::wstring>& args, const wchar_t* first, const wchar_t* second) {
    return HasArg(args, first) || HasArg(args, second);
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

std::wstring ResolveAppPrivilegeToken() {
    return EnvWide(L"ZOOM_SDK_APP_PRIVILEGE_TOKEN");
}

std::wstring ResolveMeetingNumber() {
    return EnvWide(L"ZOOM_SDK_MEETING_NUMBER");
}

std::wstring ResolveMeetingPassword() {
    return EnvWide(L"ZOOM_SDK_PASSWORD");
}

std::wstring ResolveDisplayName() {
    std::wstring name = EnvWide(L"ZOOM_SDK_DISPLAY_NAME");
    return name.empty() ? L"Palabra SDK Probe" : name;
}

UINT64 ParseMeetingNumber(const std::wstring& value) {
    std::wstring digits;
    for (wchar_t ch : value) {
        if (ch >= L'0' && ch <= L'9') {
            digits.push_back(ch);
        }
    }
    if (digits.empty()) {
        return 0;
    }
    return static_cast<UINT64>(_wcstoui64(digits.c_str(), nullptr, 10));
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

std::string MeetingStatusName(MeetingStatus status) {
    switch (status) {
    case ZOOM_SDK_NAMESPACE::MEETING_STATUS_IDLE: return "idle";
    case ZOOM_SDK_NAMESPACE::MEETING_STATUS_CONNECTING: return "connecting";
    case ZOOM_SDK_NAMESPACE::MEETING_STATUS_WAITINGFORHOST: return "waiting_for_host";
    case ZOOM_SDK_NAMESPACE::MEETING_STATUS_INMEETING: return "in_meeting";
    case ZOOM_SDK_NAMESPACE::MEETING_STATUS_DISCONNECTING: return "disconnecting";
    case ZOOM_SDK_NAMESPACE::MEETING_STATUS_RECONNECTING: return "reconnecting";
    case ZOOM_SDK_NAMESPACE::MEETING_STATUS_FAILED: return "failed";
    case ZOOM_SDK_NAMESPACE::MEETING_STATUS_ENDED: return "ended";
    case ZOOM_SDK_NAMESPACE::MEETING_STATUS_LOCKED: return "locked";
    case ZOOM_SDK_NAMESPACE::MEETING_STATUS_UNLOCKED: return "unlocked";
    case ZOOM_SDK_NAMESPACE::MEETING_STATUS_IN_WAITING_ROOM: return "in_waiting_room";
    default: return "status_" + std::to_string(static_cast<int>(status));
    }
}

class MeetingEvent : public IMeetingServiceEvent {
public:
    MeetingEvent() : done_(false), reachedMeeting_(false), lastStatus_(ZOOM_SDK_NAMESPACE::MEETING_STATUS_IDLE), lastResult_(0) {}

    void onMeetingStatusChanged(MeetingStatus status, int result = 0) override {
        WriteStatus(
            "meeting status=" + MeetingStatusName(status) +
            " result=" + std::to_string(result));
        if (done_.load()) {
            return;
        }
        lastStatus_ = status;
        lastResult_ = result;
        if (status == MEETING_STATUS_INMEETING) {
            reachedMeeting_.store(true);
            done_.store(true);
        } else if (status == MEETING_STATUS_WAITINGFORHOST || status == MEETING_STATUS_IN_WAITING_ROOM) {
            reachedMeeting_.store(true);
            done_.store(true);
        } else if (status == MEETING_STATUS_FAILED || status == MEETING_STATUS_ENDED) {
            done_.store(true);
        }
    }

    void onMeetingStatisticsWarningNotification(StatisticsWarningType) override {}
    void onMeetingParameterNotification(const MeetingParameter*) override {}
    void onSuspendParticipantsActivities() override {}
    void onAICompanionActiveChangeNotice(bool) override {}
    void onMeetingTopicChanged(const zchar_t*) override {}
    void onMeetingFullToWatchLiveStream(const zchar_t*) override {
        WriteStatus("meeting is full; livestream callback received");
    }
    void onUserNetworkStatusChanged(MeetingComponentType, ConnectionQuality, unsigned int, bool) override {}
#if defined(WIN32)
    void onAppSignalPanelUpdated(IMeetingAppSignalHandler*) override {}
#endif

    const std::atomic<bool>& done() const {
        return done_;
    }

    bool reachedMeeting() const {
        return reachedMeeting_.load();
    }

    MeetingStatus lastStatus() const {
        return lastStatus_;
    }

    int lastResult() const {
        return lastResult_;
    }

private:
    std::atomic<bool> done_;
    std::atomic<bool> reachedMeeting_;
    MeetingStatus lastStatus_;
    int lastResult_;
};

class StingMicEvent : public IZoomSDKVirtualAudioMicEvent {
public:
    explicit StingMicEvent(bool forceSendOnInitialize)
        : sender_(nullptr),
          canSend_(false),
          done_(false),
          sentSuccessfully_(false),
          forceSendOnInitialize_(forceSendOnInitialize) {}

    ~StingMicEvent() override {
        canSend_.store(false);
        if (thread_.joinable()) {
            thread_.join();
        }
    }

    void onMicInitialize(IZoomSDKAudioRawDataSender* sender) override {
        sender_ = sender;
        WriteStatus("virtual mic initialized");
        if (forceSendOnInitialize_) {
            StartSending("virtual mic initialize forced-send diagnostic");
        }
    }

    void onMicStartSend() override {
        StartSending("virtual mic start send");
    }

    void onMicStopSend() override {
        WriteStatus("virtual mic stop send");
        canSend_.store(false);
    }

    void onMicUninitialized() override {
        WriteStatus("virtual mic uninitialized");
        canSend_.store(false);
        sender_ = nullptr;
    }

    const std::atomic<bool>& done() const {
        return done_;
    }

    bool sentSuccessfully() const {
        return sentSuccessfully_.load();
    }

private:
    void StartSending(const char* reason) {
        if (canSend_.exchange(true)) {
            WriteStatus(std::string(reason) + " ignored; send thread already running");
            return;
        }
        WriteStatus(reason);
        done_.store(false);
        if (thread_.joinable()) {
            thread_.join();
        }
        thread_ = std::thread([this]() { SendSting(); });
    }

    void SendSting() {
        if (!sender_) {
            WriteStatus("virtual mic sender was not available");
            done_.store(true);
            return;
        }

        const int sampleRate = 48000;
        const int blockFrames = 960;
        const int totalFrames = sampleRate * 2;
        const double notes[] = {261.63, 329.63, 392.00, 523.25, 392.00, 523.25};
        const int noteCount = static_cast<int>(sizeof(notes) / sizeof(notes[0]));
        const int framesPerNote = totalFrames / noteCount;
        std::vector<short> samples(blockFrames);

        bool failed = false;
        for (int frame = 0; frame < totalFrames && canSend_.load(); frame += blockFrames) {
            int framesThisBlock = std::min(blockFrames, totalFrames - frame);
            for (int i = 0; i < framesThisBlock; ++i) {
                int absoluteFrame = frame + i;
                int noteIndex = std::min(noteCount - 1, absoluteFrame / framesPerNote);
                double t = static_cast<double>(absoluteFrame) / sampleRate;
                double local = static_cast<double>(absoluteFrame % framesPerNote) / framesPerNote;
                double env = std::min(1.0, local * 16.0) * std::min(1.0, (1.0 - local) * 8.0);
                double base = notes[noteIndex];
                double wave =
                    0.70 * std::sin(2.0 * 3.14159265358979323846 * base * t) +
                    0.22 * std::sin(2.0 * 3.14159265358979323846 * base * 2.0 * t) +
                    0.08 * std::sin(2.0 * 3.14159265358979323846 * base * 3.0 * t);
                int value = static_cast<int>(wave * env * 12000.0);
                value = std::max(-32768, std::min(32767, value));
                samples[i] = static_cast<short>(value);
            }
            if (framesThisBlock < blockFrames) {
                std::fill(samples.begin() + framesThisBlock, samples.end(), 0);
            }

            SDKError sendResult = sender_->send(
                reinterpret_cast<char*>(samples.data()),
                static_cast<unsigned int>(framesThisBlock * sizeof(short)),
                sampleRate,
                ZoomSDKAudioChannel_Mono);
            if (sendResult != SDKERR_SUCCESS) {
                WriteStatus("virtual mic send returned SDKError " + std::to_string(static_cast<int>(sendResult)));
                failed = true;
                break;
            }
            Sleep(20);
        }

        sentSuccessfully_.store(!failed);
        done_.store(true);
        WriteStatus("virtual mic sting completed");
    }

    IZoomSDKAudioRawDataSender* sender_;
    std::atomic<bool> canSend_;
    std::atomic<bool> done_;
    std::atomic<bool> sentSuccessfully_;
    bool forceSendOnInitialize_;
    std::thread thread_;
};

int PrintHelp() {
    std::cerr
        << "ZoomSdkNativeProbe\n"
        << "  --sdk-info          Load sdk.dll and print the SDK version.\n"
        << "  --sdk-init          Load sdk.dll and call InitSDK/CleanUPSDK.\n"
        << "  --sdk-auth          Initialize SDK and authenticate with ZOOM_SDK_AUTH_TOKEN.\n"
        << "  --sdk-join          Authenticate and attempt to join ZOOM_SDK_MEETING_NUMBER.\n"
        << "  --play-sting        After joining, send a short original test sting into the SDK mic.\n"
        << "  --force-mic-send    Diagnostic: send as soon as the SDK exposes a virtual mic sender.\n"
        << "  --custom-ui         Initialize the SDK without the default Zoom meeting UI.\n"
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
    if (HasAnyArg(args, L"--play-sting", L"--play-test-sting")) {
        commandLine += L" --play-sting";
    }
    if (HasArg(args, L"--force-mic-send")) {
        commandLine += L" --force-mic-send";
    }
    if (HasArg(args, L"--custom-ui")) {
        commandLine += L" --custom-ui";
    }
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

int RunJoinWithWatchdog(const std::vector<std::wstring>& args) {
    return RunChildWithWatchdog(args, L"--sdk-join", L"--sdk-join-child");
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
    if (HasArg(args, L"--custom-ui")) {
        initParam.obConfigOpts.optionalFeatures = ENABLE_CUSTOMIZED_UI_FLAG;
        WriteStatus("custom UI mode enabled");
    }

    WriteStatus("calling InitSDK");
    SDKError initResult = initSdk(initParam);

    if (initResult != SDKERR_SUCCESS) {
        WriteError("InitSDK failed with SDKError " + std::to_string(static_cast<int>(initResult)) + ".");
        return false;
    }
    WriteStatus("InitSDK succeeded");
    return true;
}

int RunSdkProbe(const std::vector<std::wstring>& args, bool initialize, bool authenticate, bool joinMeeting) {
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

    if (initialize || authenticate || joinMeeting) {
        CleanUPSDKFn cleanupSdk = reinterpret_cast<CleanUPSDKFn>(sdk.proc("CleanUPSDK"));
        if (!cleanupSdk) {
            WriteError("sdk.dll did not expose CleanUPSDK.");
            return 2;
        }

        if (!InitializeSdk(args, sdk)) {
            return 2;
        }

        if (authenticate || joinMeeting) {
            CreateAuthServiceFn createAuthService = reinterpret_cast<CreateAuthServiceFn>(sdk.proc("CreateAuthService"));
            DestroyAuthServiceFn destroyAuthService = reinterpret_cast<DestroyAuthServiceFn>(sdk.proc("DestroyAuthService"));
            CreateMeetingServiceFn createMeetingService = reinterpret_cast<CreateMeetingServiceFn>(sdk.proc("CreateMeetingService"));
            DestroyMeetingServiceFn destroyMeetingService = reinterpret_cast<DestroyMeetingServiceFn>(sdk.proc("DestroyMeetingService"));
            CreateNetworkConnectionHelperFn createNetworkHelper = reinterpret_cast<CreateNetworkConnectionHelperFn>(sdk.proc("CreateNetworkConnectionHelper"));
            DestroyNetworkConnectionHelperFn destroyNetworkHelper = reinterpret_cast<DestroyNetworkConnectionHelperFn>(sdk.proc("DestroyNetworkConnectionHelper"));
            GetAudioRawdataHelperFn getAudioRawdataHelper = reinterpret_cast<GetAudioRawdataHelperFn>(sdk.proc("GetAudioRawdataHelper"));
            if (!createAuthService || !destroyAuthService) {
                WriteError("sdk.dll did not expose CreateAuthService/DestroyAuthService.");
                return 2;
            }
            if (joinMeeting && (!createMeetingService || !destroyMeetingService)) {
                WriteError("sdk.dll did not expose CreateMeetingService/DestroyMeetingService.");
                return 2;
            }
            if (joinMeeting && HasAnyArg(args, L"--play-sting", L"--play-test-sting") && !getAudioRawdataHelper) {
                WriteError("sdk.dll did not expose GetAudioRawdataHelper.");
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

            IMeetingService* meetingService = nullptr;
            if (joinMeeting) {
                bool playSting = HasAnyArg(args, L"--play-sting", L"--play-test-sting");
                std::wstring meetingNumberRaw = ResolveMeetingNumber();
                UINT64 meetingNumber = ParseMeetingNumber(meetingNumberRaw);
                if (meetingNumber == 0) {
                    WriteError("ZOOM_SDK_MEETING_NUMBER is required for --sdk-join.");
                    destroyAuthService(authService);
                    if (networkHelper) {
                        networkHelper->UnRegisterNetworkConnectionHandler();
                        destroyNetworkHelper(networkHelper);
                    }
                    return 2;
                }

                SDKError meetingCreateResult = createMeetingService(&meetingService);
                if (meetingCreateResult != SDKERR_SUCCESS || !meetingService) {
                    WriteError("CreateMeetingService failed with SDKError " + std::to_string(static_cast<int>(meetingCreateResult)) + ".");
                    destroyAuthService(authService);
                    if (networkHelper) {
                        networkHelper->UnRegisterNetworkConnectionHandler();
                        destroyNetworkHelper(networkHelper);
                    }
                    return 2;
                }

                MeetingEvent meetingEvent;
                SDKError meetingEventResult = meetingService->SetEvent(&meetingEvent);
                if (meetingEventResult != SDKERR_SUCCESS) {
                    WriteError("IMeetingService::SetEvent failed with SDKError " + std::to_string(static_cast<int>(meetingEventResult)) + ".");
                    destroyMeetingService(meetingService);
                    destroyAuthService(authService);
                    if (networkHelper) {
                        networkHelper->UnRegisterNetworkConnectionHandler();
                        destroyNetworkHelper(networkHelper);
                    }
                    return 2;
                }

                std::wstring displayName = ResolveDisplayName();
                std::wstring password = ResolveMeetingPassword();
                std::wstring appPrivilegeToken = ResolveAppPrivilegeToken();
                JoinParam joinParam;
                joinParam.userType = SDK_UT_WITHOUT_LOGIN;
                JoinParam4WithoutLogin& withoutLogin = joinParam.param.withoutloginuserJoin;
                withoutLogin.meetingNumber = meetingNumber;
                withoutLogin.userName = displayName.c_str();
                withoutLogin.psw = password.empty() ? nullptr : password.c_str();
                withoutLogin.app_privilege_token = appPrivilegeToken.empty() ? nullptr : appPrivilegeToken.c_str();
                withoutLogin.isVideoOff = true;
                withoutLogin.isAudioOff = !playSting;
                withoutLogin.isMyVoiceInMix = false;
                withoutLogin.isAudioRawDataStereo = false;
                withoutLogin.eAudioRawdataSamplingRate = ZOOM_SDK_NAMESPACE::AudioRawdataSamplingRate_48K;

                WriteStatus("calling Join meeting=" + WideToUtf8(meetingNumberRaw) + " display_name=" + WideToUtf8(displayName));
                SDKError joinResult = meetingService->Join(joinParam);
                if (joinResult != SDKERR_SUCCESS) {
                    WriteError("Join call failed with SDKError " + std::to_string(static_cast<int>(joinResult)) + ".");
                    destroyMeetingService(meetingService);
                    destroyAuthService(authService);
                    if (networkHelper) {
                        networkHelper->UnRegisterNetworkConnectionHandler();
                        destroyNetworkHelper(networkHelper);
                    }
                    return 2;
                }

                int joinTimeoutSeconds = std::max(1, ArgIntValue(args, L"--timeout", 30) - proxyTimeoutSeconds);
                if (!WaitForFlag(meetingEvent.done(), joinTimeoutSeconds)) {
                    WriteError("Meeting join status did not complete within " + std::to_string(joinTimeoutSeconds) + " seconds.");
                    if (HasArg(args, L"--skip-cleanup")) {
                        FastExit(3);
                    }
                    meetingService->Leave(ZOOM_SDK_NAMESPACE::LEAVE_MEETING);
                    destroyMeetingService(meetingService);
                    destroyAuthService(authService);
                    if (networkHelper) {
                        networkHelper->UnRegisterNetworkConnectionHandler();
                        destroyNetworkHelper(networkHelper);
                    }
                    return 3;
                }

                if (!meetingEvent.reachedMeeting()) {
                    WriteError(
                        "Meeting join did not reach a usable meeting state; last status=" +
                        MeetingStatusName(meetingEvent.lastStatus()) +
                        " result=" + std::to_string(meetingEvent.lastResult()) + ".");
                    if (HasArg(args, L"--skip-cleanup")) {
                        FastExit(2);
                    }
                    destroyMeetingService(meetingService);
                    destroyAuthService(authService);
                    if (networkHelper) {
                        networkHelper->UnRegisterNetworkConnectionHandler();
                        destroyNetworkHelper(networkHelper);
                    }
                    return 2;
                }

                WriteStatus("Meeting join reached " + MeetingStatusName(meetingEvent.lastStatus()));

                if (playSting) {
                    IMeetingAudioController* audioController = meetingService->GetMeetingAudioController();
                    if (!audioController) {
                        WriteError("Meeting audio controller was not available.");
                        if (HasArg(args, L"--skip-cleanup")) {
                            FastExit(2);
                        }
                        destroyMeetingService(meetingService);
                        destroyAuthService(authService);
                        if (networkHelper) {
                            networkHelper->UnRegisterNetworkConnectionHandler();
                            destroyNetworkHelper(networkHelper);
                        }
                        return 2;
                    }

                    IZoomSDKAudioRawDataHelper* audioRawHelper = getAudioRawdataHelper();
                    if (!audioRawHelper) {
                        WriteError("GetAudioRawdataHelper returned null.");
                        if (HasArg(args, L"--skip-cleanup")) {
                            FastExit(2);
                        }
                        destroyMeetingService(meetingService);
                        destroyAuthService(authService);
                        if (networkHelper) {
                            networkHelper->UnRegisterNetworkConnectionHandler();
                            destroyNetworkHelper(networkHelper);
                        }
                        return 2;
                    }

                    StingMicEvent sting(HasArg(args, L"--force-mic-send"));
                    SDKError sourceResult = audioRawHelper->setExternalAudioSource(&sting);
                    WriteStatus("setExternalAudioSource returned SDKError " + std::to_string(static_cast<int>(sourceResult)));
                    if (sourceResult != SDKERR_SUCCESS) {
                        WriteError("Could not set external audio source.");
                        if (HasArg(args, L"--skip-cleanup")) {
                            FastExit(2);
                        }
                        destroyMeetingService(meetingService);
                        destroyAuthService(authService);
                        if (networkHelper) {
                            networkHelper->UnRegisterNetworkConnectionHandler();
                            destroyNetworkHelper(networkHelper);
                        }
                        return 2;
                    }

                    SDKError joinVoipResult = audioController->JoinVoip();
                    WriteStatus("JoinVoip returned SDKError " + std::to_string(static_cast<int>(joinVoipResult)));
                    unsigned int selfUserId = 0;
                    IMeetingParticipantsController* participantsController = meetingService->GetMeetingParticipantsController();
                    if (participantsController) {
                        IUserInfo* myself = participantsController->GetMySelfUser();
                        if (myself) {
                            selfUserId = myself->GetUserID();
                            WriteStatus(
                                "self user id=" + std::to_string(selfUserId) +
                                " muted=" + std::to_string(myself->IsAudioMuted() ? 1 : 0) +
                                " audio_type=" + std::to_string(static_cast<int>(myself->GetAudioJoinType())));
                        } else {
                            WriteStatus("GetMySelfUser returned null");
                        }
                    } else {
                        WriteStatus("Meeting participants controller was not available");
                    }
                    if (audioController->CanUnMuteBySelf()) {
                        SDKError unmuteResult = audioController->UnMuteAudio(selfUserId);
                        WriteStatus("UnMuteAudio(self) returned SDKError " + std::to_string(static_cast<int>(unmuteResult)));
                    } else {
                        WriteStatus("SDK reports self-unmute is not currently allowed");
                    }

                    int playTimeoutSeconds = std::min(10, std::max(3, ArgIntValue(args, L"--timeout", 30) / 6));
                    if (!WaitForFlag(sting.done(), playTimeoutSeconds)) {
                        WriteError("Virtual mic sting did not complete within " + std::to_string(playTimeoutSeconds) + " seconds.");
                        if (HasArg(args, L"--skip-cleanup")) {
                            FastExit(3);
                        }
                        destroyMeetingService(meetingService);
                        destroyAuthService(authService);
                        if (networkHelper) {
                            networkHelper->UnRegisterNetworkConnectionHandler();
                            destroyNetworkHelper(networkHelper);
                        }
                        return 3;
                    }
                    if (!sting.sentSuccessfully()) {
                        WriteError("Virtual mic sting did not send successfully.");
                        if (HasArg(args, L"--skip-cleanup")) {
                            FastExit(2);
                        }
                        destroyMeetingService(meetingService);
                        destroyAuthService(authService);
                        if (networkHelper) {
                            networkHelper->UnRegisterNetworkConnectionHandler();
                            destroyNetworkHelper(networkHelper);
                        }
                        return 2;
                    }
                    WriteStatus("Virtual mic sting sent");
                }
                meetingService->Leave(ZOOM_SDK_NAMESPACE::LEAVE_MEETING);
            }

            if (meetingService) {
                destroyMeetingService(meetingService);
            }
            destroyAuthService(authService);
            if (networkHelper) {
                networkHelper->UnRegisterNetworkConnectionHandler();
                destroyNetworkHelper(networkHelper);
            }
        }

        if (HasArg(args, L"--skip-cleanup")) {
            WriteDone();
            FastExit(0);
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

    if (HasArg(args, L"--sdk-join")) {
        return RunJoinWithWatchdog(args);
    }

    if (HasArg(args, L"--sdk-init-child")) {
        FastExit(RunSdkProbe(args, true, false, false));
    }

    if (HasArg(args, L"--sdk-auth-child")) {
        FastExit(RunSdkProbe(args, false, true, false));
    }

    if (HasArg(args, L"--sdk-join-child")) {
        FastExit(RunSdkProbe(args, false, true, true));
    }

    if (HasArg(args, L"--sdk-info") || args.empty()) {
        return RunSdkProbe(args, false, false, false);
    }

    WriteError("Unknown mode. Use --sdk-info, --sdk-init, --sdk-auth, --sdk-join, or --help.");
    return 2;
}
