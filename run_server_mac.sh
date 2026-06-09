#!/bin/bash
export OPENCV_FFMPEG_CAPTURE_OPTIONS="rtsp_transport;udp|fflags;nobuffer|max_delay;100000|probesize;32|analyzeduration;100000"
python detect_server.py
