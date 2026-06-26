Set-Location -Path "$PSScriptRoot\.."
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$env:OPENCV_FFMPEG_CAPTURE_OPTIONS = "rtsp_transport;udp|fflags;nobuffer|max_delay;100000|probesize;32|analyzeduration;100000"

Write-Host "========================================="
Write-Host " 馃寪 iPhone ArUco Tracker 娴嬭瘯瀹㈡埛绔惎鍔ㄥ櫒 (PowerShell)"
Write-Host "========================================="
Write-Host "姝ｅ湪鍚姩瀹㈡埛绔?.."

if (Test-Path ".venv\Scripts\python.exe") {
    & .venv\Scripts\python.exe test_client.py
} else {
    python test_client.py
}

Read-Host -Prompt "Press Enter to exit"

