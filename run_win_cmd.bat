@echo off
chcp 65001 >nul
echo 请选择连接模式:
echo [1] USB 有线连接 (推荐 - 低延迟/TCP/自动端口转发)
echo [2] Wi-Fi 无线连接 (UDP)
set /p choice="输入选项 (1 或 2, 默认 1): "

if "%choice%"=="2" (
    set TRANSPORT=udp
    echo 已选择 Wi-Fi 模式 (UDP)...
    set START_FORWARD=0
) else (
    set TRANSPORT=tcp
    echo 已选择 USB 模式 (TCP)...
    set START_FORWARD=1
)

if "%START_FORWARD%"=="1" (
    echo 正在启动 USB 端口转发 (8554 和 8555)...
    start /B pymobiledevice3 usbmux forward 8554 8554 >nul 2>&1
    start /B pymobiledevice3 usbmux forward 8555 8555 >nul 2>&1
    :: 给端口转发程序一些启动时间
    timeout /t 1 >nul
)

set OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;%TRANSPORT%^|fflags;nobuffer^|max_delay;100000^|probesize;32^|analyzeduration;100000
python detect_aruco.py

if "%START_FORWARD%"=="1" (
    echo 正在关闭 USB 端口转发...
    powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*pymobiledevice3*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
)
pause
