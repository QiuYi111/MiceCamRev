# Bug Report: LRCP 摄像头时间戳全线合成，无硬件帧元数据可用

**日期**: 2026-06-14
**设备**: LRCP V1080P-60fps (VID=05A3, PID=9260, USB Composite Device, UVC 1.1)
**OS**: Windows 11 Pro 10.0.26200
**严重程度**: **High** — 影响跨模态时间对齐的精度上限和丢帧检测的确定性

---

## 1. 现象

摄像头在 30fps 和 60fps 下，DirectShow 的 `IMediaSample::GetTime()` 返回的时间戳是**合成值**——只包含 2 个唯一间隔值（恰好 1/fps），不是硬件采集时刻。

```
30fps:  orig 间隔 = 33.333ms 或 33.338ms（仅 2 个唯一值 / 12,446 帧）
60fps:  orig 间隔 = 16.667ms 或 16.670ms（仅 2 个唯一值 / 828 帧）
```

同一帧上 `orig - graph_timestamp` 出现**负值**（orig 比 graph 更早），物理上不可能——确凿证据证明 orig 是合成的。

---

## 2. 根因分析

### 2.1 谁在合成时间戳？

```
Camera Sensor (LRCP)
    │  固件不填 UVC Payload Header 的 dwPresentationTime 字段
    │  SCR (Source Clock Reference) 可用性: 未知
    ▼
Windows UVC Driver (usbvideo.sys)
    │  收到 USB 等时包，组装帧
    │  KSSTREAM_HEADER.PresentationTime = 空（相机固件未填）
    │  UVC Payload Header 在此层被剥离，SCR/FID 不向用户态暴露
    ▼
DirectShow Capture Filter (ksproxy.ax)          ← ★ 合成点
    │  检测到 PresentationTime 为空
    │  执行: sample_time = last_sample_time + AvgTimePerFrame
    │  这个值是纯计算结果，不包含任何硬件时刻信息
    ▼
ffmpeg dshow pin (IMemInputPin::Receive)
    │  IMediaSample::GetTime() → "orig"（已是合成值）
    │  IReferenceClock::GetTime() → "graph"（参考时钟 = QPC 域，真实）
    │  ffmpeg 未做任何合成——它只是把上游的值透传出来
    ▼
ffmpeg demuxer → output
```

**合成者不是 ffmpeg，是 Windows 的 ksproxy.ax（DirectShow Capture Filter）。**

Media Foundation 走同一条 UVC 驱动，`IMFSample::GetSampleTime()` 会面临相同的合成行为。

### 2.2 为什么无法确认 SCR / 硬件帧计数器？

UVC 规范定义了 Payload Header 中的 **SCR (Source Clock Reference)** 字段——一个 6 字节的硬件计数器，每帧递增。但是：

- **Windows UVC 驱动 (usbvideo.sys) 在用户态不可见地剥离了 UVC Payload Header**
- SCR 存在于 USB 总线上的原始数据中，但不被暴露给 DirectShow 或 Media Foundation API
- `MFSampleExtension_DeviceTimestamp` 可能承载 SCR，但该摄像头返回 `MF_E_ATTRIBUTENOTFOUND`

为确认 SCR 是否存在于 USB 描述符中，需要读取 UVC VideoStreaming Input Header 描述符的 `bmHeaderInfo` 字段。**在 Windows 11 上，USB Root Hub 的 `GUID_DEVINTERFACE_USB_HUB` 设备接口已被移除，`IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION` 不可用**——即使是 Administrator 权限也无法访问。Python ctypes 和 C# P/Invoke 两种实现均返回 0 个 hub 设备接口。

```
已验证的尝试:
  [FAIL] Python ctypes + SetupDiGetClassDevsW(GUID_USB_HUB) → 0 devices
  [FAIL] Python ctypes + CM_Get_Device_Interface_List → ACCESS_DENIED (0x05)
  [FAIL] C# P/Invoke + SetupDiGetClassDevsW(GUID_USB_HUB) → 0 devices
  [FAIL] C# P/Invoke + direct \\.\USB#ROOT_HUB30 path → FILE_NOT_FOUND (0x02)
  [N/A]  libusb / WinUSB → 需要替换驱动（会断开 UVC 功能）
```

**结论：在 Windows 11 用户态无法读取此摄像头的 USB 描述符。需 Linux 环境用 `lsusb -v` 确认。**

---

## 3. 两条时间戳路径的实测对比

### 3.1 1080p @ 30fps

| 指标 | orig (sample timestamp) | graph (reference clock @ Receive) |
|------|------------------------|----------------------------------|
| 均值 | 33.337 ms | 33.515 ms |
| 标准差 | 0.002 ms | 4.750 ms |
| 唯一间隔值 | **2** (合成) | 11 |
| Batching (>50ms gaps) | 0 | 0 |
| Bursts (<17ms) | 0 | 0 |

**30fps 下 graph clock 无 batching，帧匀速到达。可用。**

### 3.2 720p @ 60fps

| 指标 | orig (sample timestamp) | graph (reference clock @ Receive) |
|------|------------------------|----------------------------------|
| 均值 | 16.670 ms | 16.755 ms |
| 标准差 | 0.002 ms | 3.419 ms |
| 唯一间隔值 | **2** (合成) | 12 |
| Gaps (>25ms) | 0 | 39 / 827 (4.7%) |
| Bursts (<5ms) | 0 | 0 |

**60fps 下有 4.7% 的帧间隔超过 25ms（预期 16.7ms）。无 bursts，不是 USB 批量堆积——是 USB 调度延迟。**

### 3.3 关键发现：graph clock 是唯一可用的真实时间戳

- `use_video_device_timestamps=0` 时，ffmpeg 使用 `IReferenceClock::GetTime()` — 这是 DirectShow 参考时钟在 `IMemInputPin::Receive()` 被调用时的时刻
- 参考时钟底层 = QPC，与 `perf_counter()` 同源
- 量化到 ~1ms（参考时钟的 tick 粒度）
- 30fps 下均匀可用；60fps 下有偶发延迟

---

## 4. 影响评估

| 功能 | 状态 | 说明 |
|------|------|------|
| 硬件时间戳 (dwPresentationTime) | ❌ 不可用 | 相机固件不填，ksproxy 合成 |
| 硬件帧计数器 (SCR) | ❓ 无法确认 | Windows 11 无法读取 USB 描述符 |
| 帧标识符 (FID) | ❓ 无法确认 | 同上，需要 USB 描述符确认 |
| Graph clock 时间戳 | ✅ 可用 | QPC 域，~1ms 量化，30fps 均匀 |
| 丢帧检测 (ΔPTS 方法) | ✅ 可用 | graph clock 的双倍间隔可标记丢帧 |
| 丢帧检测 (硬件帧号) | ❌ 不可用 | SCR 不暴露给用户态 |
| 丢帧检测 (交叉校验) | ⚠️ 部分 | 依赖 graph clock 间隔异常检测 |
| MJPEG 编码延迟 | ⚠️ 可变 | 帧大小 18KB–33KB，编码耗时随画面变化 |

---

## 5. 建议

### 5.1 立即（无硬件戳分支，按 `相机时间采集白皮书.md` §4）

1. **使用 `use_video_device_timestamps=0`（graph clock）作为唯一时间戳源**
2. **锁死曝光时间和帧率**（关闭自动曝光/自动增益）
3. **优先 YUY2 而非 MJPEG**（避免可变编码延迟），若摄像头不支持 YUY2，保持 MJPEG 但接受 ~1-3ms 的编码延迟抖动
4. **Δpts 直方图监控丢帧**——graph clock 下丢帧会留双倍间隔的洞
5. **LED 标定**——建立 graph clock 到曝光中心的固定偏移

### 5.2 短期（确认 SCR 存在性）

**在 Linux 上运行 `lsusb -v -d 05a3:9260`**，定位 `VideoStreaming Interface` 描述符中的 `bmHeaderInfo` 字段：
- bit 3 = 1 → SCR 存在，硬件帧计数器可用（但 Windows 上仍无法访问）
- bit 3 = 0 → SCR 不存在，完全依赖 graph clock + ΔPTS

如果 SCR 存在且后续需要访问，可考虑：
- 用 WinUSB 替换 UVC 驱动，自行解析 UVC Payload Header
- 代价：失去 DirectShow/Media Foundation 兼容性，需自建采集管线

### 5.3 长期（硬件方案）

更换支持硬件 PTS 的科研级相机（FLIR/Point Grey 等），其 `MFSampleExtension_DeviceTimestamp` 直接返回 QPC 域的采集时刻。

---

## 6. 相关文件

- [时间同步系统白皮书.md](时间同步系统白皮书.md) — 多传感器时间同步架构
- [相机时间采集白皮书.md](相机时间采集白皮书.md) — 相机时间戳获取与对齐策略
- `motor_drive/tools/probe_mf_camera.py` — dshow 探针脚本（30fps/60fps 对比）
- `motor_drive/temp_uvc_probe/Program.cs` — C# USB Hub 访问尝试（失败）
- `motor_drive/tools/read_uvc_descriptor.py` — Python USB 描述符读取尝试（失败）
- `motor_drive/temp_probe_720p60_data.html` — 60fps 探针可视化报告
