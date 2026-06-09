# iPhone ArUco Tracker Client

本模块是配合 iPhone H264 RTSP 视频流服务使用的低延迟定位解算客户端。

主程序为 `detect_aruco.py`，实现低延迟 ArUco 标记 2D/3D 位姿定位与实时相机内参同步。

为了解决内置的 OpenCV FFmpeg 引擎在不同平台（特别是 Windows）下默认强制 TCP 导致的卡顿以及网络环境隔离问题，视频流传输必须配置低延迟 UDP 环境变量。

---

## 🚀 极速启动脚本

请根据您的操作系统和终端环境，选择以下命令来启动脚本。它们会在启动 Python 进程前注入优化环境变量：

### 1. Windows (Command Prompt - CMD)
打开 CMD，进入当前文件夹并运行：
```cmd
set OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;udp^|fflags;nobuffer^|max_delay;100000^|probesize;32^|analyzeduration;100000
python detect_aruco.py
```
*(注：`^|` 是 CMD 中转义管道符 `|` 的写法)*

### 2. Windows (PowerShell)
打开 PowerShell，进入当前文件夹并运行：
```powershell
$env:OPENCV_FFMPEG_CAPTURE_OPTIONS = "rtsp_transport;udp|fflags;nobuffer|max_delay;100000|probesize;32|analyzeduration;100000"
python detect_aruco.py
```

### 3. macOS / Linux (Bash / Zsh)
打开终端，进入当前文件夹并运行：
```bash
export OPENCV_FFMPEG_CAPTURE_OPTIONS="rtsp_transport;udp|fflags;nobuffer|max_delay;100000|probesize;32|analyzeduration;100000"
python detect_aruco.py

# 或者直接单行运行：
OPENCV_FFMPEG_CAPTURE_OPTIONS="rtsp_transport;udp|fflags;nobuffer|max_delay;100000|probesize;32|analyzeduration;100000" python detect_aruco.py
```

---

## 🛠️ 环境变量优化参数详解

在上述启动指令中注入的 `OPENCV_FFMPEG_CAPTURE_OPTIONS` 各项参数具有如下优化效果：

| 参数 | 选项值 | 优化效果说明 |
| :--- | :--- | :--- |
| `rtsp_transport` | `udp` | **强制使用 UDP 传输。** iOS 服务端只开发了 UDP 端口的视频流发送，强制此项可省去 30 秒的 TCP 失败重试过程，实现秒连。 |
| `fflags` | `nobuffer` | **禁用内部帧缓冲区。** FFmpeg 底层缓冲区一收到视频帧便会立即输出给 OpenCV，不会积压帧，彻底干掉因累积缓冲区导致的累积延迟。 |
| `max_delay` | `100000` | **限制抖动缓冲区延迟。** 设置为 100ms (100,000微秒)，防止音视频同步造成的过度等待。 |
| `probesize` | `32` | **限制流探测大小。** 设置为极小值 32 字节，让连接瞬间打开而不用等待流特征解析。 |
| `analyzeduration` | `100000` | **限制流分析时间。** 设置为 100ms (100,000微秒)，缩短握手分析时长。 |
