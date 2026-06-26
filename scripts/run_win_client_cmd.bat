@echo off
cd /d "%~dp0.."
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;udp^|fflags;nobuffer^|max_delay;100000^|probesize;32^|analyzeduration;100000

echo =========================================
echo  iPhone ArUco Tracker 测试客户端启动器 (CMD)
echo =========================================
echo 正在启动客户�?..

if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe test_client.py
) else (
    python test_client.py
)
pause

