import os
import cv2
import numpy as np
import socket
import json
import threading
import time
import asyncio
from urllib.parse import urlparse
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

# =========================================================================
# ⚠️ RTSP 极速低延迟优化设置
# =========================================================================
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
    "rtsp_transport;udp|"
    "fflags;nobuffer|"
    "max_delay;100000|"
    "probesize;32|"
    "analyzeduration;100000"
)

# =========================================================================
# 📌 全局服务配置参数
# =========================================================================
RTSP_URL = "rtsp://172.22.39.171:8554/"
ARUCO_DICT_TYPE = cv2.aruco.DICT_4X4_50
MARKER_LENGTH = 0.027  # 单位：米

# =========================================================================
# 🔒 全局状态与线程安全锁
# =========================================================================
state_lock = threading.Lock()
raw_intrinsics = None       # 接收自手机的原始内参 {"fx", "fy", "cx", "cy", "w", "h"}
intrinsics_connected = False

latest_intrinsics_data = {
    "source": "Estimated (Default)",
    "connected": False,
    "fx": 0.0, "fy": 0.0, "cx": 0.0, "cy": 0.0,
    "width": 0, "height": 0
}

latest_localization_data = {
    "detected": False,
    "timestamp": 0.0,
    "markers": []
}

# 异步流订阅队列集合
active_video_queues = set()
active_loc_queues = set()


# =========================================================================
# 🧮 辅助计算函数 (Mathematics & Helpers)
# =========================================================================

def compute_euler_angles(rvec):
    """
    将 Rodrigues 旋转向量转换为标准的 ZYX 顺序欧拉角 (Roll, Pitch, Yaw)
    """
    R, _ = cv2.Rodrigues(rvec)
    sy = np.sqrt(R[0, 0]**2 + R[1, 0]**2)
    singular = sy < 1e-6
    
    if not singular:
        roll = np.arctan2(R[2, 1], R[2, 2])
        pitch = np.arctan2(-R[2, 0], sy)
        yaw = np.arctan2(R[1, 0], R[0, 0])
    else:
        roll = np.arctan2(-R[1, 2], R[1, 1])
        pitch = np.arctan2(-R[2, 0], sy)
        yaw = 0
        
    return np.degrees(roll), np.degrees(pitch), np.degrees(yaw)


def compute_2d_angle(c2d):
    """
    根据二维码 2D 轮廓计算其在投影平面内的 2D 旋转角
    以垂直正上方（12点钟方向）为 0 度，顺时针为正值，逆时针为负值
    """
    top_center = (c2d[0] + c2d[1]) / 2.0
    bottom_center = (c2d[2] + c2d[3]) / 2.0
    vec_up = top_center - bottom_center
    
    angle = np.degrees(np.arctan2(vec_up[1], vec_up[0])) + 90.0
    if angle > 180:
        angle -= 360
    elif angle < -180:
        angle += 360
    return angle


# =========================================================================
# 📞 后台线程：内参 TCP 接收器
# =========================================================================

def intrinsics_reader_thread(ip, port):
    """
    后台线程：实时连接手机 TCP 服务端，接收并解析相机实时标定数据。
    """
    global raw_intrinsics, intrinsics_connected
    print(f"[Intrinsics Thread] 正在尝试连接相机内参 TCP 服务器 -> {ip}:{port}")
    
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect((ip, port))
            with state_lock:
                intrinsics_connected = True
            print(f"[Intrinsics Thread] ✅ 成功连接到内参服务器 {ip}:{port}")
            
            f = s.makefile('r', encoding='utf-8')
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    with state_lock:
                        raw_intrinsics = {
                            "fx": float(data["fx"]),
                            "fy": float(data["fy"]),
                            "cx": float(data["cx"]),
                            "cy": float(data["cy"]),
                            "w": int(data.get("w", 0)),
                            "h": int(data.get("h", 0))
                        }
                except Exception as e:
                    print(f"[Intrinsics Thread] 解析 JSON 数据失败: {e}")
            
            f.close()
            s.close()
        except (socket.error, socket.timeout) as e:
            with state_lock:
                intrinsics_connected = False
                raw_intrinsics = None
            print(f"[Intrinsics Thread] ⚠️ 连接断开或超时 ({e})，将在 2 秒后自动重试...")
            time.sleep(2.0)


# =========================================================================
# 🎥 后台线程：RTSP 图像抓取与 ArUco 姿态计算主循环
# =========================================================================

def video_processing_thread(loop, phone_ip):
    """
    独立后台线程：拉取视频流，进行 ArUco 姿态解算，并利用 loop.call_soon_threadsafe 
    将视频帧 (JPEG) 和定位数据实时分发给 FastAPI 的异步订阅队列。
    """
    global latest_intrinsics_data, latest_localization_data
    
    # 启动内参接收子线程
    threading.Thread(target=intrinsics_reader_thread, args=(phone_ip, 8555), daemon=True).start()

    print(f"[Video Thread] 正在建立 RTSP 连接 -> {RTSP_URL}")
    cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print("[Video Thread] ❌ 错误：无法打开视频流。")
        return

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    print(f"[Video Thread] ✅ 视频连接成功！分辨率: {width}x{height} | 帧率: {fps} FPS")

    # 1. 估算默认备用内参矩阵
    focal_length = width * 0.8
    fallback_matrix = np.array([
        [focal_length, 0, width / 2.0],
        [0, focal_length, height / 2.0],
        [0, 0, 1]
    ], dtype=np.float32)
    dist_coeffs = np.zeros((5, 1), dtype=np.float32)

    # 2. 建立 ArUco 3D 物理边界坐标点集
    marker_points = np.array([
        [-MARKER_LENGTH / 2.0,  MARKER_LENGTH / 2.0, 0],
        [ MARKER_LENGTH / 2.0,  MARKER_LENGTH / 2.0, 0],
        [ MARKER_LENGTH / 2.0, -MARKER_LENGTH / 2.0, 0],
        [-MARKER_LENGTH / 2.0, -MARKER_LENGTH / 2.0, 0]
    ], dtype=np.float32)

    # 初始化 ArUco 检测器
    aruco_dict = cv2.aruco.getPredefinedDictionary(ARUCO_DICT_TYPE)
    detector_params = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(aruco_dict, detector_params)

    while True:
        ret, frame = cap.read()
        if not ret:
            continue

        # ----------------- A. 相机内参缩放 -----------------
        with state_lock:
            curr_raw = raw_intrinsics
            conn = intrinsics_connected

        if curr_raw is not None:
            # 缩放内参
            cap_w = curr_raw["w"] if curr_raw["w"] > 0 else 1920
            cap_h = curr_raw["h"] if curr_raw["h"] > 0 else 1080
            is_swapped = (width > height) != (cap_w > cap_h)
            if is_swapped:
                scale_x, scale_y = width / cap_h, height / cap_w
            else:
                scale_x, scale_y = width / cap_w, height / cap_h

            fx_s, fy_s = curr_raw["fx"] * scale_x, curr_raw["fy"] * scale_y
            cx_s, cy_s = curr_raw["cx"] * scale_x, curr_raw["cy"] * scale_y
            
            active_matrix = np.array([
                [fx_s, 0, cx_s],
                [0, fy_s, cy_s],
                [0, 0, 1]
            ], dtype=np.float32)
            intrinsics_source = "Real-time"
        else:
            active_matrix = fallback_matrix
            fx_s, fy_s = fallback_matrix[0,0], fallback_matrix[1,1]
            cx_s, cy_s = fallback_matrix[0,2], fallback_matrix[1,2]
            intrinsics_source = "Estimated (Default)"

        # 更新全局内参状态
        with state_lock:
            latest_intrinsics_data = {
                "source": intrinsics_source,
                "connected": conn,
                "fx": float(fx_s),
                "fy": float(fy_s),
                "cx": float(cx_s),
                "cy": float(cy_s),
                "width": width,
                "height": height
            }

        # ----------------- B. ArUco 姿态估算 -----------------
        corners, ids, rejected = detector.detectMarkers(frame)
        
        detected_markers = []
        if ids is not None and len(ids) > 0:
            for i in range(len(ids)):
                c2d = corners[i][0].astype(np.float32)
                success, rvec, tvec = cv2.solvePnP(
                    marker_points, c2d, active_matrix, dist_coeffs, flags=cv2.SOLVEPNP_IPPE_SQUARE
                )
                if success:
                    roll, pitch, yaw = compute_euler_angles(rvec)
                    angle_2d = compute_2d_angle(c2d)
                    center_x, center_y = np.mean(c2d, axis=0)
                    
                    x_cm = float(tvec[0][0] * 100)
                    y_cm = float(tvec[1][0] * 100)
                    z_cm = float(tvec[2][0] * 100)
                    distance = float(np.linalg.norm(tvec) * 100)
                    
                    detected_markers.append({
                        "id": int(ids[i][0]),
                        "center_2d": {"u": float(center_x), "v": float(center_y)},
                        "angle_2d": float(angle_2d),
                        "roll_3d": float(roll),
                        "pitch_3d": float(pitch),
                        "yaw_3d": float(yaw),
                        "position_3d": {"x": x_cm, "y": y_cm, "z": z_cm},
                        "distance": distance
                    })

        # 更新全局定位状态
        loc_payload = {
            "detected": len(detected_markers) > 0,
            "timestamp": time.time(),
            "markers": detected_markers
        }
        with state_lock:
            latest_localization_data = loc_payload

        # ----------------- C. 数据异步分发 -----------------
        # 1. 广播定位 JSON 数据到所有的 WebSocket 订阅队列
        for q in list(active_loc_queues):
            loop.call_soon_threadsafe(q.put_nowait, loc_payload)

        # 2. 单次 JPEG 编码并分发到所有的视频流队列
        if len(active_video_queues) > 0:
            # 适当限制 JPEG 编码质量以减少带宽并提高帧率
            ret_enc, jpeg_buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ret_enc:
                jpeg_bytes = jpeg_buf.tobytes()
                for q in list(active_video_queues):
                    loop.call_soon_threadsafe(q.put_nowait, jpeg_bytes)


# =========================================================================
# 🌐 FastAPI Web 服务接口
# =========================================================================

app = FastAPI(
    title="iPhone ArUco Tracker Local Server",
    description="支持多客户端复用的超低延迟定位解算和视频流 API 本地网关",
    version="1.0.0"
)

@app.on_event("startup")
def startup_event():
    """
    网关启动入口：获取 asyncio event loop 并启动后台线程进行视频捕获与姿态计算。
    """
    loop = asyncio.get_running_loop()
    
    # 从配置的 RTSP URL 解析手机 IP
    try:
        parsed_url = urlparse(RTSP_URL)
        phone_ip = parsed_url.hostname or RTSP_URL.split("//")[1].split(":")[0]
    except Exception:
        phone_ip = "127.0.0.1"
        
    threading.Thread(target=video_processing_thread, args=(loop, phone_ip), daemon=True).start()


@app.get("/intrinsics")
def get_intrinsics():
    """
    HTTP 接口：获取相机当前的实时内参各项参数及 TCP 连接状态。
    """
    with state_lock:
        return latest_intrinsics_data


@app.get("/localization")
def get_localization():
    """
    HTTP 接口：获取最近一次的 ArUco 定位与 6D 姿态求解结果。
    """
    with state_lock:
        return latest_localization_data


async def mjpeg_stream_generator():
    """
    视频流生成器：订阅后台视频队列，动态清空旧帧以实现绝对超低延迟的 MJPEG 输出。
    """
    queue = asyncio.Queue(maxsize=5)
    active_video_queues.add(queue)
    try:
        while True:
            # 挂起等待下一帧
            jpeg_bytes = await queue.get()
            # 丢弃缓冲队列中堆积的旧帧，保证传输绝对实时
            while queue.qsize() > 0:
                jpeg_bytes = queue.get_nowait()
                
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg_bytes + b'\r\n')
    except asyncio.CancelledError:
        pass
    finally:
        active_video_queues.remove(queue)


@app.get("/video_feed")
async def get_video_feed():
    """
    HTTP 接口：多路复用的极速 MJPEG 实时视频流端点。
    """
    return StreamingResponse(
        mjpeg_stream_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.websocket("/ws/localization")
async def ws_localization(websocket: WebSocket):
    """
    WebSocket 接口：在每帧姿态解算完成后，主动、高频地向连接客户端推送最新的定位数据 JSON。
    """
    await websocket.accept()
    queue = asyncio.Queue(maxsize=5)
    active_loc_queues.add(queue)
    try:
        while True:
            loc_data = await queue.get()
            # 丢弃堆积的旧帧数据
            while queue.qsize() > 0:
                loc_data = queue.get_nowait()
            await websocket.send_json(loc_data)
    except WebSocketDisconnect:
        pass
    finally:
        active_loc_queues.remove(queue)


if __name__ == "__main__":
    import uvicorn
    # 绑定 127.0.0.1 在本地暴露服务
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
