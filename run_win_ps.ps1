Write-Host "请选择连接模式:"
Write-Host "[1] USB 有线连接 (推荐 - 低延迟/TCP/自动端口转发)"
Write-Host "[2] Wi-Fi 无线连接 (UDP)"
$choice = Read-Host "输入选项 (1 或 2, 默认 1)"

$startForward = $true
if ($choice -eq "2") {
    $transport = "udp"
    $startForward = $false
    Write-Host "已选择 Wi-Fi 模式 (UDP)..."
} else {
    $transport = "tcp"
    Write-Host "已选择 USB 模式 (TCP)..."
}

$env:OPENCV_FFMPEG_CAPTURE_OPTIONS = "rtsp_transport;$transport|fflags;nobuffer|max_delay;100000|probesize;32|analyzeduration;100000"

$jobRTSP = $null
$jobInt = $null

if ($startForward) {
    Write-Host "正在启动 USB 端口转发 (8554 和 8555)..."
    $jobRTSP = Start-Job -ScriptBlock { pymobiledevice3 usbmux forward 8554 8554 }
    $jobInt = Start-Job -ScriptBlock { pymobiledevice3 usbmux forward 8555 8555 }
    Start-Sleep -Seconds 1
}

try {
    python detect_aruco.py
} finally {
    if ($startForward) {
        Write-Host "正在关闭 USB 端口转发..."
        if ($jobRTSP) { Stop-Job $jobRTSP -ErrorAction SilentlyContinue; Remove-Job $jobRTSP -ErrorAction SilentlyContinue }
        if ($jobInt) { Stop-Job $jobInt -ErrorAction SilentlyContinue; Remove-Job $jobInt -ErrorAction SilentlyContinue }
        # Extra safety check for any orphaned pymobiledevice3 processes
        Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*pymobiledevice3*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    }
}

Read-Host -Prompt "Press Enter to exit"
