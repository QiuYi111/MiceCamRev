# MiceCam — Dual Camera Recorder

双摄像头同步录像工具，使用 ffmpeg 后端 + PyQt6 界面。

## 功能

- 🎥 双摄像头同时录制 + 实时预览
- ⚡ 优先使用硬件编码 (VideoToolbox / AMF / NVENC)
- 📼 MP4 输出 (H.264 / H.265)
- ⏱️ SRT 纳秒级时间戳 (wall clock + steady clock)
- 🖥️ PyQt6 原生界面
- 📦 打包为单个 exe (PyInstaller + 预编译 ffmpeg)

## 开发

```bash
# 安装依赖
uv sync

# 运行
uv run micecam
# 或
uv run python -m micecam

# 测试
uv run pytest tests/ -v
```

## 打包 (Windows)

```bash
# 1. 下载预编译的 Windows ffmpeg
uv run python scripts/download_ffmpeg.py

# 2. 打包
uv run pyinstaller micecam.spec

# 3. 输出在 dist/MiceCam.exe
```

## 架构

```
摄像头枚举 → ffmpeg -list_devices / -list_options
     ↓
预览 ← ffmpeg rawvideo pipe → QThread → QImage → QLabel
     ↓
录制 ← ffmpeg → MP4 (H.264/H.265 硬件编码)
     ↓
时间戳 ← Python time.time() + time.monotonic_ns() → SRT
```

## 许可

MIT
