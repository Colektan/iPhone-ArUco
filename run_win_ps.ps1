$env:OPENCV_FFMPEG_CAPTURE_OPTIONS = "rtsp_transport;udp|fflags;nobuffer|max_delay;100000|probesize;32|analyzeduration;100000"
python detect_aruco.py
Read-Host -Prompt "Press Enter to exit"
