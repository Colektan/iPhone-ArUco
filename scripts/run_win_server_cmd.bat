@echo off
cd /d "%~dp0.."
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
echo =========================================
echo  iPhone ArUco Tracker 网关服务端启动器 (Windows CMD)
echo =========================================
echo 请选择连接模式:
echo [1] USB 有线连接 (推荐 - 低延�?TCP/自动端口转发)
echo [2] Wi-Fi 无线连接 (UDP)
set /p choice="输入选项 (1 �?2, 默认 1): "

set START_FORWARD=1
if "%choice%"=="2" (
    goto WIFI_MODE
) else (
    goto USB_MODE
)

:USB_MODE
set TRANSPORT=tcp
set RTSP_URL=rtsp://127.0.0.1:8554/
echo 已选择 USB 模式 (TCP)...
goto CONFIG_ML

:WIFI_MODE
set TRANSPORT=udp
set START_FORWARD=0
echo 已选择 Wi-Fi 模式 (UDP)...

:: 自动推导手机（网关）IP 地址作为默认�?set DEFAULT_IP=172.22.39.171
powershell -Command "Get-NetIPConfiguration | Where-Object InterfaceAlias -match 'WLAN|Wi-Fi|无线' | Select-Object -ExpandProperty IPv4DefaultGateway | Select-Object -ExpandProperty NextHop" > temp_ip.txt
set /p DETECTED_IP=<temp_ip.txt
del temp_ip.txt
if not "%DETECTED_IP%"=="" set DEFAULT_IP=%DETECTED_IP%
if not "%DETECTED_IP%"=="" echo 💡 检测到当前处于热点/Wi-Fi网络，已自动推导手机（网关）IP �? %DETECTED_IP%

set /p phone_ip="请输入手�?IP 地址 (默认 %DEFAULT_IP%): "
if "%phone_ip%"=="" set phone_ip=%DEFAULT_IP%
set RTSP_URL=rtsp://%phone_ip%:8554/
goto CONFIG_ML

:CONFIG_ML
echo.
echo 请选择是否启用大语言视觉物体定位服务 (Florence-2):
echo [1] 仅启动基础 ArUco 视频流与物理定位服务 (推荐 - 极速启�?超低延迟)
echo [2] 启动全部服务 (包含 Florence-2 语义 3D 定位，首次运行将下载 ~1GB 模型权重)
set /p ml_choice="输入选项 (1 �?2, 默认 1): "
if "%ml_choice%"=="2" (
    set DISABLE_ML=0
    echo 已选择启动全部服务...
) else (
    set DISABLE_ML=1
    echo 已选择仅启动基础 ArUco 视频流服�?..
)
echo.

echo 视频流连接设置为 -> %RTSP_URL%
set OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;%TRANSPORT%^|fflags;nobuffer^|max_delay;100000^|probesize;32^|analyzeduration;100000

:: 检测并清理端口占用 (8000 for FastAPI, 8554 �?8555 for USB 转发)
powershell -Command "Stop-Process -Id (Get-NetTCPConnection -LocalPort 8000,8554,8555 -ErrorAction SilentlyContinue).OwningProcess -Force -ErrorAction SilentlyContinue" >nul 2>&1

if "%START_FORWARD%"=="1" (
    echo 正在启动 USB 端口转发 (8554 �?8555)...
    start /B .venv\Scripts\pymobiledevice3 usbmux forward 8554 8554 >nul 2>&1
    start /B .venv\Scripts\pymobiledevice3 usbmux forward 8555 8555 >nul 2>&1
    rem 给端口转发程序一些启动时�?    ping 127.0.0.1 -n 2 >nul
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

