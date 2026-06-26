Set-Location -Path "$PSScriptRoot\.."
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
Write-Host "========================================="
Write-Host " 馃寪 iPhone ArUco Tracker 缃戝叧鏈嶅姟绔惎鍔ㄥ櫒 (Windows PowerShell)"
Write-Host "========================================="
Write-Host "璇烽€夋嫨杩炴帴妯″紡:"
Write-Host "[1] USB 鏈夌嚎杩炴帴 (鎺ㄨ崘 - 浣庡欢杩?TCP/鑷姩绔彛杞彂)"
Write-Host "[2] Wi-Fi 鏃犵嚎杩炴帴 (UDP)"
$choice = Read-Host "杈撳叆閫夐」 (1 鎴?2, 榛樿 1)"

$startForward = $true
if ($choice -eq "2") {
    $transport = "udp"
    $startForward = $false
    Write-Host "宸查€夋嫨 Wi-Fi 妯″紡 (UDP)..."
    
    # 鑷姩鎺㈡祴鐑偣缃戝叧 IP 浣滀负榛樿鍊?    $default_ip = "172.22.39.171"
    try {
        $wlan_gw = (Get-NetIPConfiguration | Where-Object InterfaceAlias -match "WLAN|Wi-Fi|鏃犵嚎" | Select-Object -ExpandProperty IPv4DefaultGateway -First 1).NextHop
        if ($wlan_gw) {
            $default_ip = $wlan_gw
            Write-Host "馃挕 妫€娴嬪埌褰撳墠澶勪簬鐑偣/Wi-Fi缃戠粶锛屽凡鑷姩鎺ㄥ鎵嬫満锛堢綉鍏筹級IP 涓? $default_ip"
        }
    } catch {}

    $phone_ip = Read-Host "璇疯緭鍏ユ墜鏈?IP 鍦板潃 (榛樿 $default_ip)"
    if ([string]::IsNullOrEmpty($phone_ip)) {
        $phone_ip = $default_ip
    }
    $env:RTSP_URL = "rtsp://${phone_ip}:8554/"
} else {
    $transport = "tcp"
    $env:RTSP_URL = "rtsp://127.0.0.1:8554/"
    Write-Host "宸查€夋嫨 USB 妯″紡 (TCP)..."
}

Write-Host "瑙嗛娴佽繛鎺ヨ缃负 -> $env:RTSP_URL"
$env:OPENCV_FFMPEG_CAPTURE_OPTIONS = "rtsp_transport;$transport|fflags;nobuffer|max_delay;100000|probesize;32|analyzeduration;100000"

Write-Host ""
Write-Host "璇烽€夋嫨鏄惁鍚敤澶ц瑷€瑙嗚鐗╀綋瀹氫綅鏈嶅姟 (Florence-2):"
Write-Host "[1] 浠呭惎鍔ㄥ熀纭€ ArUco 瑙嗛娴佷笌鐗╃悊瀹氫綅鏈嶅姟 (鎺ㄨ崘 - 鏋侀€熷惎鍔?瓒呬綆寤惰繜)"
Write-Host "[2] 鍚姩鍏ㄩ儴鏈嶅姟 (鍖呭惈 Florence-2 璇箟 3D 瀹氫綅锛岄娆¤繍琛屽皢涓嬭浇 ~1GB 妯″瀷鏉冮噸)"
$ml_choice = Read-Host "杈撳叆閫夐」 (1 鎴?2, 榛樿 1)"
if ($ml_choice -eq "2") {
    $env:DISABLE_ML = "0"
    Write-Host "宸查€夋嫨鍚姩鍏ㄩ儴鏈嶅姟..."
} else {
    $env:DISABLE_ML = "1"
    Write-Host "宸查€夋嫨浠呭惎鍔ㄥ熀纭€ ArUco 瑙嗛娴佹湇鍔?.."
}
Write-Host ""

# 妫€娴嬪苟娓呯悊绔彛鍗犵敤 (8000 for FastAPI, 8554 鍜?8555 for USB 杞彂)
foreach ($port in @(8000, 8554, 8555)) {
    $conn = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
    if ($conn) {
        $pids = $conn | Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($pidToKill in $pids) {
            Write-Host "妫€娴嬪埌绔彛 $port 宸茶杩涚▼ $pidToKill 鍗犵敤锛屾鍦ㄩ噴鏀剧鍙?.."
            Stop-Process -Id $pidToKill -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 500
    }
}

$jobRTSP = $null
$jobInt = $null

if ($startForward) {
    Write-Host "姝ｅ湪鍚姩 USB 绔彛杞彂 (8554 鍜?8555)..."
    $pymobiledevice3Path = Resolve-Path "$PSScriptRoot\..\.venv\Scripts\pymobiledevice3.exe"
    $jobRTSP = Start-Job -ScriptBlock { & $using:pymobiledevice3Path usbmux forward 8554 8554 }
    $jobInt = Start-Job -ScriptBlock { & $using:pymobiledevice3Path usbmux forward 8555 8555 }
    Start-Sleep -Seconds 1
}

Write-Host "姝ｅ湪鎷夎捣鏈湴瀹氫綅鏈嶅姟..."
try {
    if (Test-Path ".venv\Scripts\python.exe") {
        & .venv\Scripts\python.exe detect_server.py
    } else {
        python detect_server.py
    }
} finally {
    if ($startForward) {
        Write-Host "姝ｅ湪鍏抽棴 USB 绔彛杞彂..."
        if ($jobRTSP) { Stop-Job $jobRTSP -ErrorAction SilentlyContinue; Remove-Job $jobRTSP -ErrorAction SilentlyContinue }
        if ($jobInt) { Stop-Job $jobInt -ErrorAction SilentlyContinue; Remove-Job $jobInt -ErrorAction SilentlyContinue }
        # Extra safety check for any orphaned pymobiledevice3 processes
        Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*pymobiledevice3*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    }
}

Read-Host -Prompt "Press Enter to exit"

