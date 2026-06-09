#!/bin/bash
echo "请选择连接模式:"
echo "[1] USB 有线连接 (推荐 - 低延迟/TCP/自动端口转发)"
echo "[2] Wi-Fi 无线连接 (UDP)"
read -p "输入选项 (1 或 2, 默认 1): " choice

start_forward=true
if [ "$choice" = "2" ]; then
    TRANSPORT="udp"
    start_forward=false
    echo "已选择 Wi-Fi 模式 (UDP)..."
else
    TRANSPORT="tcp"
    echo "已选择 USB 模式 (TCP)..."
fi

export OPENCV_FFMPEG_CAPTURE_OPTIONS="rtsp_transport;${TRANSPORT}|fflags;nobuffer|max_delay;100000|probesize;32|analyzeduration;100000"

if [ "$start_forward" = true ]; then
    echo "正在启动 USB 端口转发 (8554 和 8555)..."
    pymobiledevice3 usbmux forward 8554 8554 >/dev/null 2>&1 &
    PID_RTSP=$!
    pymobiledevice3 usbmux forward 8555 8555 >/dev/null 2>&1 &
    PID_INT=$!
    
    # Register trap to kill background processes on script exit
    trap 'echo "正在关闭 USB 端口转发..."; kill $PID_RTSP $PID_INT >/dev/null 2>&1' EXIT
    sleep 1
fi

python detect_aruco.py
