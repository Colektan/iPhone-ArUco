Set-Location -Path "$PSScriptRoot\.."
Write-Host "========================================="
Write-Host " 🌐 iPhone ArUco Tracker 网关服务端启动器 (Windows PowerShell)"
Write-Host "========================================="
Write-Host "请选择连接模式:"
Write-Host "[1] USB 有线连接 (推荐 - 低延迟/TCP/自动端口转发)"
Write-Host "[2] Wi-Fi 无线连接 (UDP)"
$choice = Read-Host "输入选项 (1 或 2, 默认 1)"

$startForward = $true
if ($choice -eq "2") {
    $transport = "udp"
    $startForward = $false
    Write-Host "已选择 Wi-Fi 模式 (UDP)..."
    $phone_ip = Read-Host "请输入手机 IP 地址 (默认 172.22.39.171)"
    if ([string]::IsNullOrEmpty($phone_ip)) {
        $phone_ip = "172.22.39.171"
    }
    $env:RTSP_URL = "rtsp://$phone_ip:8554/"
} else {
    $transport = "tcp"
    $env:RTSP_URL = "rtsp://127.0.0.1:8554/"
    Write-Host "已选择 USB 模式 (TCP)..."
}

Write-Host "视频流连接设置为 -> $env:RTSP_URL"
$env:OPENCV_FFMPEG_CAPTURE_OPTIONS = "rtsp_transport;$transport|fflags;nobuffer|max_delay;100000|probesize;32|analyzeduration;100000"

# 检测并清理端口占用 (8000 for FastAPI, 8554 和 8555 for USB 转发)
foreach ($port in @(8000, 8554, 8555)) {
    $conn = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
    if ($conn) {
        $pids = $conn | Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($pidToKill in $pids) {
            Write-Host "检测到端口 $port 已被进程 $pidToKill 占用，正在释放端口..."
            Stop-Process -Id $pidToKill -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 500
    }
}

$jobRTSP = $null
$jobInt = $null

if ($startForward) {
    Write-Host "正在启动 USB 端口转发 (8554 和 8555)..."
    $jobRTSP = Start-Job -ScriptBlock { pymobiledevice3 usbmux forward 8554 8554 }
    $jobInt = Start-Job -ScriptBlock { pymobiledevice3 usbmux forward 8555 8555 }
    Start-Sleep -Seconds 1
}

Write-Host "正在拉起本地定位服务..."
try {
    if (Test-Path ".venv\Scripts\python.exe") {
        & .venv\Scripts\python.exe detect_server.py
    } else {
        python detect_server.py
    }
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
