@echo off
cd /d "%~dp0.."
chcp 65001 >nul
echo =========================================
echo  🌐 iPhone ArUco Tracker 网关服务端启动器 (Windows CMD)
echo =========================================
echo 请选择连接模式:
echo [1] USB 有线连接 (推荐 - 低延迟/TCP/自动端口转发)
echo [2] Wi-Fi 无线连接 (UDP)
set /p choice="输入选项 (1 或 2, 默认 1): "

set START_FORWARD=1
if "%choice%"=="2" (
    set TRANSPORT=udp
    set START_FORWARD=0
    echo 已选择 Wi-Fi 模式 (UDP)...
    set /p phone_ip="请输入手机 IP 地址 (默认 172.22.39.171): "
    if "%phone_ip%"=="" set phone_ip=172.22.39.171
    set RTSP_URL=rtsp://%phone_ip%:8554/
) else (
    set TRANSPORT=tcp
    set RTSP_URL=rtsp://127.0.0.1:8554/
    echo 已选择 USB 模式 (TCP)...
)

echo 视频流连接设置为 -> %RTSP_URL%
set OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;%TRANSPORT%^|fflags;nobuffer^|max_delay;100000^|probesize;32^|analyzeduration;100000

:: 检测并清理端口占用 (8000 for FastAPI, 8554 和 8555 for USB 转发)
for %%p in (8000 8554 8555) do (
    powershell -Command "$pidToKill = Get-NetTCPConnection -LocalPort %%p -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique; if ($pidToKill) { Write-Host '检测到端口 %%p 已被进程' $pidToKill '占用，正在关闭进程...'; Stop-Process -Id $pidToKill -Force -ErrorAction SilentlyContinue }" >nul 2>&1
)

if "%START_FORWARD%"=="1" (
    echo 正在启动 USB 端口转发 (8554 和 8555)...
    start /B pymobiledevice3 usbmux forward 8554 8554 >nul 2>&1
    start /B pymobiledevice3 usbmux forward 8555 8555 >nul 2>&1
    :: 给端口转发程序一些启动时间
    timeout /t 1 >nul
)

echo 正在拉起本地定位服务...
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe detect_server.py
) else (
    python detect_server.py
)

if "%START_FORWARD%"=="1" (
    echo 正在关闭 USB 端口转发...
    powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*pymobiledevice3*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
)
pause
