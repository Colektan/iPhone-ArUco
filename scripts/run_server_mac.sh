#!/bin/bash
# 自动切换到项目根目录
cd "$(dirname "$0")/.."

source .venv/bin/activate

echo "========================================="
echo " 🌐 iPhone ArUco Tracker 网关服务端启动器"
echo "========================================="
echo "请选择连接模式:"
echo "[1] USB 有线连接 (推荐 - 低延迟/TCP/自动端口转发)"
echo "[2] Wi-Fi 无线连接 (UDP)"
read -p "输入选项 (1 或 2, 默认 1): " choice

start_forward=true
if [ "$choice" = "2" ]; then
    TRANSPORT="udp"
    start_forward=false
    echo "已选择 Wi-Fi 模式 (UDP)..."
    read -p "请输入手机 IP 地址 (默认 172.22.39.171): " phone_ip
    if [ -z "$phone_ip" ]; then
        phone_ip="172.22.39.171"
    fi
    export RTSP_URL="rtsp://${phone_ip}:8554/"
    echo "视频流连接设置为 -> ${RTSP_URL}"
else
    TRANSPORT="tcp"
    export RTSP_URL="rtsp://127.0.0.1:8554/"
    echo "已选择 USB 模式 (TCP)..."
fi

export OPENCV_FFMPEG_CAPTURE_OPTIONS="rtsp_transport;${TRANSPORT}|fflags;nobuffer|max_delay;100000|probesize;32|analyzeduration;100000"

if [ "$start_forward" = true ]; then
    echo "正在启动 USB 端口转发 (8554 和 8555)..."
    pymobiledevice3 usbmux forward 8554 8554 >/dev/null 2>&1 &
    PID_RTSP=$!
    pymobiledevice3 usbmux forward 8555 8555 >/dev/null 2>&1 &
    PID_INT=$!
    
    # 退出脚本时自动清理后台端口转发进程
    trap 'echo "正在关闭 USB 端口转发..."; kill $PID_RTSP $PID_INT >/dev/null 2>&1' EXIT
    sleep 1
fi

echo "正在拉起本地定位服务..."
python detect_server.py
