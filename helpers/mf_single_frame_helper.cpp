#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mfreadwrite.h>
#include <propvarutil.h>
#include <wrl/client.h>

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <optional>
#include <queue>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

using Microsoft::WRL::ComPtr;
namespace fs = std::filesystem;

struct Options {
    std::wstring camera_id;
    std::wstring camera_name;
    int device_number = -1;
    int width = 1920;
    int height = 1080;
    int fps = 30;
    std::string pixel_format = "auto";
    fs::path output_dir;
    size_t ring_capacity = 256;
};

struct CaptureMode {
    int width = 0;
    int height = 0;
    std::string format;
};

struct Frame {
    uint64_t frame_id = 0;
    uint64_t callback_seq = 0;
    uint64_t arrival_qpc_ns = 0;
    double arrival_delta_ms = 0.0;
    int64_t mf_pts_100ns = 0;
    double mf_pts_delta_ms = 0.0;
    int width = 0;
    int height = 0;
    std::string format;
    std::vector<uint8_t> payload;
    uint64_t drop_count = 0;
    size_t occupancy_after_push = 0;
};

struct SharedState {
    std::mutex mutex;
    std::condition_variable cv;
    std::queue<Frame> queue;
    size_t capacity = 256;
    bool capture_done = false;
    std::atomic<bool> stop_requested = false;
    std::atomic<bool> capture_error = false;
    std::atomic<uint64_t> frame_count = 0;
    std::atomic<uint64_t> drop_count = 0;
    std::atomic<uint64_t> lock_failure_count = 0;
};

static uint64_t g_qpc_frequency = 0;

static uint64_t qpc_ns() {
    LARGE_INTEGER counter{};
    QueryPerformanceCounter(&counter);
    return static_cast<uint64_t>(
        (static_cast<long double>(counter.QuadPart) * 1000000000.0L) /
        static_cast<long double>(g_qpc_frequency));
}

static std::string narrow(const std::wstring& value) {
    if (value.empty()) return {};
    int size = WideCharToMultiByte(CP_UTF8, 0, value.c_str(), -1, nullptr, 0, nullptr, nullptr);
    std::string out(static_cast<size_t>(size - 1), '\0');
    WideCharToMultiByte(CP_UTF8, 0, value.c_str(), -1, out.data(), size, nullptr, nullptr);
    return out;
}

static std::wstring widen(const std::string& value) {
    if (value.empty()) return {};
    int size = MultiByteToWideChar(CP_UTF8, 0, value.c_str(), -1, nullptr, 0);
    std::wstring out(static_cast<size_t>(size - 1), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, value.c_str(), -1, out.data(), size);
    return out;
}

static std::string json_escape(const std::string& value) {
    std::ostringstream out;
    for (char ch : value) {
        switch (ch) {
            case '\\': out << "\\\\"; break;
            case '"': out << "\\\""; break;
            case '\n': out << "\\n"; break;
            case '\r': out << "\\r"; break;
            case '\t': out << "\\t"; break;
            default: out << ch; break;
        }
    }
    return out.str();
}

static void emit_json(const std::string& body) {
    std::cout << "{" << body << "}" << std::endl;
}

static std::wstring strip_dshow_prefix(std::wstring id) {
    const std::wstring prefix = L"video=";
    if (id.rfind(prefix, 0) == 0) {
        id = id.substr(prefix.size());
    }
    std::wstring unescaped;
    unescaped.reserve(id.size());
    for (size_t i = 0; i < id.size(); ++i) {
        if (id[i] == L'\\' && i + 1 < id.size() && (id[i + 1] == L':' || id[i + 1] == L'\\')) {
            unescaped.push_back(id[++i]);
        } else {
            unescaped.push_back(id[i]);
        }
    }
    return unescaped;
}

static GUID subtype_from_pixel_format(const std::string& format) {
    if (format == "mjpg" || format == "mjpeg") return MFVideoFormat_MJPG;
    if (format == "yuy2" || format == "yuyv422") return MFVideoFormat_YUY2;
    if (format == "nv12") return MFVideoFormat_NV12;
    if (format == "rgb24") return MFVideoFormat_RGB24;
    return GUID_NULL;
}

static std::string subtype_name(const GUID& subtype) {
    if (subtype == MFVideoFormat_MJPG) return "mjpg";
    if (subtype == MFVideoFormat_YUY2) return "yuy2";
    if (subtype == MFVideoFormat_NV12) return "nv12";
    if (subtype == MFVideoFormat_RGB24) return "rgb24";
    if (subtype == MFVideoFormat_H264) return "h264";
    return "unknown";
}

static HRESULT create_source_reader(const Options& options, IMFSourceReader** reader) {
    ComPtr<IMFAttributes> attrs;
    HRESULT hr = MFCreateAttributes(&attrs, 1);
    if (FAILED(hr)) return hr;
    hr = attrs->SetGUID(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE, MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID);
    if (FAILED(hr)) return hr;

    IMFActivate** devices = nullptr;
    UINT32 count = 0;
    hr = MFEnumDeviceSources(attrs.Get(), &devices, &count);
    if (FAILED(hr)) return hr;
    if (count == 0) return HRESULT_FROM_WIN32(ERROR_NOT_FOUND);

    std::wstring wanted = !options.camera_name.empty()
        ? options.camera_name
        : strip_dshow_prefix(options.camera_id);
    int seen_name = 0;
    ComPtr<IMFMediaSource> source;
    for (UINT32 i = 0; i < count; ++i) {
        WCHAR* friendly = nullptr;
        UINT32 friendly_len = 0;
        devices[i]->GetAllocatedString(MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME, &friendly, &friendly_len);
        std::wstring name = friendly ? std::wstring(friendly, friendly_len) : L"";
        CoTaskMemFree(friendly);

        bool name_match = wanted.empty() || name == wanted;
        bool ordinal_match = options.device_number < 0 || seen_name == options.device_number;
        if (name_match) {
            if (ordinal_match) {
                hr = devices[i]->ActivateObject(IID_PPV_ARGS(&source));
                break;
            }
            ++seen_name;
        }
    }
    for (UINT32 i = 0; i < count; ++i) {
        devices[i]->Release();
    }
    CoTaskMemFree(devices);
    if (!source) return HRESULT_FROM_WIN32(ERROR_NOT_FOUND);

    ComPtr<IMFAttributes> reader_attrs;
    hr = MFCreateAttributes(&reader_attrs, 1);
    if (FAILED(hr)) return hr;
    reader_attrs->SetUINT32(MF_READWRITE_DISABLE_CONVERTERS, FALSE);
    hr = MFCreateSourceReaderFromMediaSource(source.Get(), reader_attrs.Get(), reader);
    return hr;
}

static HRESULT configure_reader(IMFSourceReader* reader, const Options& options, CaptureMode* mode) {
    ComPtr<IMFMediaType> type;
    HRESULT hr = MFCreateMediaType(&type);
    if (FAILED(hr)) return hr;
    hr = type->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    if (FAILED(hr)) return hr;
    GUID subtype = subtype_from_pixel_format(options.pixel_format);
    if (subtype != GUID_NULL) {
        hr = type->SetGUID(MF_MT_SUBTYPE, subtype);
        if (FAILED(hr)) return hr;
    }
    MFSetAttributeSize(type.Get(), MF_MT_FRAME_SIZE, options.width, options.height);
    MFSetAttributeRatio(type.Get(), MF_MT_FRAME_RATE, options.fps, 1);
    hr = reader->SetCurrentMediaType(MF_SOURCE_READER_FIRST_VIDEO_STREAM, nullptr, type.Get());
    if (FAILED(hr) && subtype != GUID_NULL) {
        type->DeleteItem(MF_MT_SUBTYPE);
        hr = reader->SetCurrentMediaType(MF_SOURCE_READER_FIRST_VIDEO_STREAM, nullptr, type.Get());
    }
    if (FAILED(hr)) return hr;

    ComPtr<IMFMediaType> current;
    hr = reader->GetCurrentMediaType(MF_SOURCE_READER_FIRST_VIDEO_STREAM, &current);
    if (SUCCEEDED(hr)) {
        GUID selected_subtype = GUID_NULL;
        current->GetGUID(MF_MT_SUBTYPE, &selected_subtype);
        mode->format = subtype_name(selected_subtype);
        UINT32 actual_width = 0;
        UINT32 actual_height = 0;
        if (SUCCEEDED(MFGetAttributeSize(current.Get(), MF_MT_FRAME_SIZE, &actual_width, &actual_height))) {
            mode->width = static_cast<int>(actual_width);
            mode->height = static_cast<int>(actual_height);
        }
    }
    if (mode->width <= 0) mode->width = options.width;
    if (mode->height <= 0) mode->height = options.height;
    if (mode->format.empty()) mode->format = "unknown";
    return hr;
}

static bool push_frame(SharedState& state, Frame&& frame) {
    std::lock_guard<std::mutex> lock(state.mutex);
    if (state.queue.size() >= state.capacity) {
        state.drop_count.fetch_add(1);
        return false;
    }
    frame.drop_count = state.drop_count.load();
    frame.occupancy_after_push = state.queue.size() + 1;
    state.queue.push(std::move(frame));
    state.cv.notify_one();
    return true;
}

static void writer_loop(SharedState& state, const Options& options, const std::string& format) {
    fs::create_directories(options.output_dir / "frames");
    std::ofstream log(options.output_dir / "frame_log.csv", std::ios::binary);
    log << "frame_id,arrival_qpc_ns,arrival_delta_ms,mf_pts_100ns,mf_pts_delta_ms,"
           "callback_seq,width,height,format,bytes,drop_count,ring_buffer_size_after_push,"
           "write_start_qpc_ns,write_end_qpc_ns,write_latency_ms\n";

    std::ofstream raw_bin;
    std::ofstream raw_idx;
    if (format != "mjpg") {
        raw_bin.open(options.output_dir / "frames.bin", std::ios::binary);
        raw_idx.open(options.output_dir / "frames.idx.csv", std::ios::binary);
        raw_idx << "frame_id,offset,bytes,arrival_qpc_ns,mf_pts_100ns,width,height,format\n";
    }

    uint64_t raw_offset = 0;
    for (;;) {
        Frame frame;
        {
            std::unique_lock<std::mutex> lock(state.mutex);
            state.cv.wait(lock, [&] { return state.capture_done || !state.queue.empty(); });
            if (state.queue.empty() && state.capture_done) break;
            frame = std::move(state.queue.front());
            state.queue.pop();
        }

        uint64_t write_start = qpc_ns();
        if (frame.format == "mjpg") {
            std::ostringstream name;
            name << "frame_" << std::setw(6) << std::setfill('0') << frame.frame_id << ".jpg";
            std::ofstream jpg(options.output_dir / "frames" / name.str(), std::ios::binary);
            jpg.write(reinterpret_cast<const char*>(frame.payload.data()), static_cast<std::streamsize>(frame.payload.size()));
        } else {
            raw_bin.write(reinterpret_cast<const char*>(frame.payload.data()), static_cast<std::streamsize>(frame.payload.size()));
            raw_idx << frame.frame_id << "," << raw_offset << "," << frame.payload.size() << ","
                    << frame.arrival_qpc_ns << "," << frame.mf_pts_100ns << ","
                    << frame.width << "," << frame.height << "," << frame.format << "\n";
            raw_offset += frame.payload.size();
        }
        uint64_t write_end = qpc_ns();
        double latency_ms = static_cast<double>(write_end - write_start) / 1000000.0;

        log << frame.frame_id << "," << frame.arrival_qpc_ns << ","
            << std::fixed << std::setprecision(6) << frame.arrival_delta_ms << ","
            << frame.mf_pts_100ns << "," << frame.mf_pts_delta_ms << ","
            << frame.callback_seq << "," << frame.width << "," << frame.height << ","
            << frame.format << "," << frame.payload.size() << "," << frame.drop_count << ","
            << frame.occupancy_after_push << "," << write_start << "," << write_end << ","
            << latency_ms << "\n";
    }
}

static void capture_loop(SharedState& state, IMFSourceReader* reader, const CaptureMode& mode) {
    uint64_t last_arrival = 0;
    int64_t last_pts = 0;
    uint64_t callback_seq = 0;
    uint64_t last_drop_emit_count = 0;
    for (;;) {
        if (state.stop_requested.load()) break;
        DWORD stream_index = 0;
        DWORD flags = 0;
        LONGLONG timestamp = 0;
        ComPtr<IMFSample> sample;
        HRESULT hr = reader->ReadSample(
            MF_SOURCE_READER_FIRST_VIDEO_STREAM,
            0,
            &stream_index,
            &flags,
            &timestamp,
            &sample);
        uint64_t arrival = qpc_ns();
        ++callback_seq;
        if (FAILED(hr)) {
            emit_json("\"event\":\"error\",\"message\":\"ReadSample failed\"");
            state.capture_error.store(true);
            state.stop_requested.store(true);
            break;
        }
        if (flags & MF_SOURCE_READERF_ENDOFSTREAM) break;
        if (!sample) continue;

        ComPtr<IMFMediaBuffer> buffer;
        hr = sample->ConvertToContiguousBuffer(&buffer);
        if (FAILED(hr)) {
            emit_json("\"event\":\"warning\",\"message\":\"ConvertToContiguousBuffer failed\"");
            continue;
        }
        BYTE* data = nullptr;
        DWORD max_len = 0;
        DWORD current_len = 0;
        hr = buffer->Lock(&data, &max_len, &current_len);
        if (FAILED(hr)) {
            uint64_t failures = state.lock_failure_count.fetch_add(1) + 1;
            emit_json("\"event\":\"warning\",\"message\":\"Buffer Lock failed\",\"lock_failure_count\":" +
                      std::to_string(failures));
            continue;
        }

        Frame frame;
        frame.frame_id = state.frame_count.fetch_add(1) + 1;
        frame.callback_seq = callback_seq;
        frame.arrival_qpc_ns = arrival;
        frame.arrival_delta_ms = last_arrival ? static_cast<double>(arrival - last_arrival) / 1000000.0 : 0.0;
        frame.mf_pts_100ns = timestamp;
        frame.mf_pts_delta_ms = last_pts ? static_cast<double>(timestamp - last_pts) / 10000.0 : 0.0;
        frame.width = mode.width;
        frame.height = mode.height;
        frame.format = mode.format;
        frame.payload.assign(data, data + current_len);
        buffer->Unlock();
        last_arrival = arrival;
        last_pts = timestamp;

        if (!push_frame(state, std::move(frame))) {
            uint64_t drop_count = state.drop_count.load();
            if (drop_count == 1 || drop_count % 30 == 0 || drop_count - last_drop_emit_count >= 30) {
                last_drop_emit_count = drop_count;
                emit_json("\"event\":\"dropped\",\"drop_count\":" + std::to_string(drop_count));
            }
        }
        uint64_t count = state.frame_count.load();
        if (count % 30 == 0) {
            emit_json("\"event\":\"frame_count\",\"frame_count\":" + std::to_string(count));
        }
    }
    {
        std::lock_guard<std::mutex> lock(state.mutex);
        state.capture_done = true;
    }
    state.cv.notify_all();
}

static Options parse_args(int argc, char** argv) {
    Options options;
    for (int i = 1; i < argc; ++i) {
        std::string key = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) return "";
            return argv[++i];
        };
        if (key == "--camera-id") options.camera_id = widen(next());
        else if (key == "--camera-name") options.camera_name = widen(next());
        else if (key == "--device-number") options.device_number = std::stoi(next());
        else if (key == "--width") options.width = std::stoi(next());
        else if (key == "--height") options.height = std::stoi(next());
        else if (key == "--fps") options.fps = std::stoi(next());
        else if (key == "--pixel-format") options.pixel_format = next();
        else if (key == "--output-dir") options.output_dir = fs::u8path(next());
        else if (key == "--ring-buffer-capacity") options.ring_capacity = static_cast<size_t>(std::stoul(next()));
        else if (key == "--shared-wall-start" || key == "--shared-steady-start-ns") (void)next();
    }
    if (options.output_dir.empty()) {
        throw std::runtime_error("--output-dir is required");
    }
    return options;
}

int main(int argc, char** argv) {
    LARGE_INTEGER freq{};
    QueryPerformanceFrequency(&freq);
    g_qpc_frequency = static_cast<uint64_t>(freq.QuadPart);

    try {
        Options options = parse_args(argc, argv);
        fs::create_directories(options.output_dir);

        HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        if (FAILED(hr)) throw std::runtime_error("CoInitializeEx failed");
        hr = MFStartup(MF_VERSION);
        if (FAILED(hr)) throw std::runtime_error("MFStartup failed");

        ComPtr<IMFSourceReader> reader;
        hr = create_source_reader(options, &reader);
        if (FAILED(hr)) throw std::runtime_error("Could not open Media Foundation camera source");

        CaptureMode mode;
        hr = configure_reader(reader.Get(), options, &mode);
        if (FAILED(hr)) throw std::runtime_error("Could not configure Media Foundation capture mode");

        SharedState state;
        state.capacity = options.ring_capacity;

        emit_json("\"event\":\"ready\",\"qpc_frequency\":" + std::to_string(g_qpc_frequency) +
                  ",\"format\":\"" + json_escape(mode.format) + "\",\"width\":" +
                  std::to_string(mode.width) + ",\"height\":" + std::to_string(mode.height));

        std::thread writer([&] { writer_loop(state, options, mode.format); });
        std::thread capture([&] { capture_loop(state, reader.Get(), mode); });

        std::thread command([&] {
            for (std::string line; std::getline(std::cin, line); ) {
                if (line == "q" || line == "quit" || line == "stop") {
                    state.stop_requested.store(true);
                    break;
                }
            }
            state.stop_requested.store(true);
        });
        command.detach();

        capture.join();
        writer.join();

        std::string stop_reason = state.capture_error.load() ? "capture_error" : "user";
        emit_json("\"event\":\"stopped\",\"stop_reason\":\"" + stop_reason + "\",\"frame_count\":" +
                  std::to_string(state.frame_count.load()) + ",\"drop_count\":" +
                  std::to_string(state.drop_count.load()));
        MFShutdown();
        CoUninitialize();
        return 0;
    } catch (const std::exception& exc) {
        emit_json("\"event\":\"error\",\"message\":\"" + json_escape(exc.what()) + "\"");
        return 1;
    }
}
