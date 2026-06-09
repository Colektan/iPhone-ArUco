# iPhone ArUco Tracker Client

本模块是配合 iPhone H264 RTSP 视频流服务使用的低延迟定位解算客户端。

主程序为 `detect_aruco.py`，实现低延迟 ArUco 标记 2D/3D 位姿定位与实时相机内参同步。

---

## 🛠️ 环境准备与安装

在运行程序之前，请确保已连接您的 iPhone 并且电脑上已准备好 Python 环境。

### 1. 安装依赖包
在当前文件夹路径下，使用以下命令一键安装所有必要的依赖库（包含 OpenCV、NumPy 以及跨平台有线端口转发工具 `pymobiledevice3`）：
```bash
pip install -r requirements.txt
```

---

## 🚀 一键智能启动脚本

为了实现有线 USB 无感连接和零缓冲，我们对启动脚本进行了完全的自动化和交互式重构。

当您启动以下任意脚本时，系统会**提示您选择连接模式**：
- **`[1] USB 有线连接 (推荐)`**：脚本将自动在后台开启 `pymobiledevice3` 端口转发服务（将 iPhone 的 `8554` 和 `8555` 端口映射到电脑的 `127.0.0.1`），然后强制 FFmpeg 使用 TCP 传输协议连接本地流。在主程序退出时，脚本会**自动清理并关闭后台转发进程**，不会占用系统资源。
- **`[2] Wi-Fi 无线连接`**：直接通过局域网以 UDP 模式低延迟拉流。

请双击或在终端中运行与您的操作系统相匹配的脚本：

### 1. Windows (Command Prompt - CMD)
```cmd
run_win_cmd.bat
```

### 2. Windows (PowerShell)
```powershell
.\run_win_ps.ps1
```

### 3. macOS / Linux
```bash
./run_mac.sh
```
*(注：Mac 脚本已赋予执行权限，若失效可重新运行 `chmod +x run_mac.sh`)*

---

## ⚙️ 环境变量优化参数详解

脚本会在启动 Python 前自动注入 `OPENCV_FFMPEG_CAPTURE_OPTIONS`。参数定义如下：

| 参数 | 选项值 | 优化效果说明 |
| :--- | :--- | :--- |
| `rtsp_transport` | `tcp` / `udp` | **传输协议。** 有线 USB (iproxy) 必须强制定为 `tcp` 以穿透 USB 管道；Wi-Fi 模式定为 `udp`。 |
| `fflags` | `nobuffer` | **禁用内部帧缓冲区。** FFMPEG 一收到视频帧便会立即输出给 OpenCV，彻底消除累积延迟。 |
| `max_delay` | `100000` | **限制抖动缓冲区延迟。** 设置为 100ms，防止音视频同步造成的过度等待。 |
| `probesize` | `32` | **限制流探测大小。** 设置为极小值 32 字节，让连接瞬间打开而不用等待流特征解析。 |
| `analyzeduration` | `100000` | **限制流分析时间。** 设置为 100ms，缩短握手分析时长。 |
