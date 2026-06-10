import os
import cv2
import numpy as np
import socket
import json
import threading
import time
import asyncio
import io
from urllib.parse import urlparse
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# =========================================================================
# ⚠️ ML 依赖库导入与优雅回退机制
# =========================================================================
FLORENCE_AVAILABLE = False
try:
    import sys
    # 1. 强制伪装导入 flash_attn 以避免 Florence-2 内部代码运行时发生 ImportError
    sys.modules["flash_attn"] = None
    
    # 2. 对 Hugging Face transformers 动态模块解析进行 Monkey Patch，绕过环境依赖强检
    import transformers
    import transformers.dynamic_module_utils
    from transformers.dynamic_module_utils import get_imports
    
    def patched_get_imports(filename: str):
        imports = get_imports(filename)
        if "flash_attn" in imports:
            imports.remove("flash_attn")
        return imports
        
    transformers.dynamic_module_utils.get_imports = patched_get_imports

    import torch
    from transformers import AutoProcessor, AutoModelForCausalLM
    from PIL import Image
    import io
    FLORENCE_AVAILABLE = True
    print("[ML Setup] ✅ 成功加载机器学习核心依赖库并且绕过了 flash_attn 强检")
except ImportError:
    FLORENCE_AVAILABLE = False
    print("[ML Setup] ⚠️ 未检测到机器学习环境依赖（transformers/torch/timm/einops/pillow）。"
          "定位接口 /locate_object 将不可用或运行在报错模式，但这不影响基础 ArUco 视频流服务。")


# =========================================================================
# ⚠️ Hugging Face 镜像源与 Mac MPS 优化设置
# =========================================================================
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"  # 允许 Mac MPS (Metal) 在遇到个别未实现算子时自动降级回 CPU，保证 100% 运行成功

# =========================================================================
# 📌 全局服务配置参数
# =========================================================================
RTSP_URL = os.environ.get("RTSP_URL", "rtsp://127.0.0.1:8554/")
ARUCO_DICT_TYPE = cv2.aruco.DICT_4X4_50
MARKER_LENGTH = 0.027  # 单位：米
BOARD_CONFIG_FILE = "custom_board_config.json"

# =========================================================================
# 🔒 全局状态与线程安全锁
# =========================================================================
state_lock = threading.Lock()
raw_intrinsics = None       # 接收自手机的原始内参 {"fx", "fy", "cx", "cy", "w", "h"}
intrinsics_connected = False
camera_connected = False    # 摄像头 RTSP 视频流连接状态
last_frame_time = 0.0       # 最近一次成功接收视频帧的时间戳

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

# 桌面定位平面参数 Ax + By + Cz + D = 0 与实时画面缓存
latest_table_plane = None   # {"normal": [A, B, C], "d": D, "tvec": [...], "rvec": [...], "timestamp": ...}
latest_raw_frame = None     # 存储最新一帧的原始 BGR 图像（不带画线HUD标注），供 Florence-2 识别

# 异步流订阅队列集合
active_video_queues = set()
active_loc_queues = set()

# =========================================================================
# 🤖 Florence-2 模型全局变量
# =========================================================================
florence_model = None
florence_processor = None
florence_device = "cpu"
florence_loading_status = "Not loaded"

# =========================================================================
# 📋 ArUco Board 全局变量
# =========================================================================
BOARD_AVAILABLE = False
board = None
board_base_id = 0

def init_aruco_board():
    global BOARD_AVAILABLE, board, board_base_id
    if os.path.exists(BOARD_CONFIG_FILE):
        try:
            with open(BOARD_CONFIG_FILE, "r") as f:
                config = json.load(f)
            board_ids = []
            board_points = []
            board_base_id = config.get("base_id", 0)
            
            for m in config["markers"]:
                board_ids.append(m["id"])
                board_points.append(np.array(m["corners_3d"], dtype=np.float32))
                
            board_ids = np.array(board_ids, dtype=np.int32)
            aruco_dict = cv2.aruco.getPredefinedDictionary(ARUCO_DICT_TYPE)
            board = cv2.aruco.Board(board_points, aruco_dict, board_ids)
            BOARD_AVAILABLE = True
            print(f"[Board Setup] ✅ 成功加载自定义桌面 ArUco Board 配置，原点 ID: {board_base_id}")
        except Exception as e:
            print(f"[Board Setup] ⚠️ 加载 Board 配置文件失败: {e}")
            BOARD_AVAILABLE = False
    else:
        print("[Board Setup] ℹ️ 未检测到 custom_board_config.json 配置文件，系统运行在单码检测与纯 2D 识别回退模式。")

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
# 🎥 后台线程：RTSP 图像抓取与 ArUco/Board 姿态计算主循环
# =========================================================================

def video_processing_thread(loop, phone_ip):
    global latest_intrinsics_data, latest_localization_data, latest_table_plane, latest_raw_frame, camera_connected, last_frame_time
    
    # 启动内参接收子线程
    threading.Thread(target=intrinsics_reader_thread, args=(phone_ip, 8555), daemon=True).start()

    print(f"[Video Thread] 正在建立 RTSP 连接 -> {RTSP_URL}")
    cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print("[Video Thread] ❌ 错误：无法打开视频流。")
        with state_lock:
            camera_connected = False
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

    # 2. 建立单码 ArUco 3D 物理边界坐标点集
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
            with state_lock:
                if time.time() - last_frame_time > 3.0:
                    camera_connected = False
            continue

        # ----------------- A. 相机内参缩放 -----------------
        with state_lock:
            curr_raw = raw_intrinsics
            conn = intrinsics_connected
            camera_connected = True
            last_frame_time = time.time()

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
            # 缓存最新原始 BGR 帧，供大模型按需调用识别
            latest_raw_frame = frame.copy()

        # ----------------- B. ArUco 单码与 Board 姿态估算 -----------------
        corners, ids, rejected = detector.detectMarkers(frame)
        
        detected_markers = []
        board_tracked = False

        if ids is not None and len(ids) > 0:
            # 1. 尝试使用 Board 联合估算桌面平面 (如果配置可用)
            if BOARD_AVAILABLE and board is not None:
                obj_points, img_points = board.matchImagePoints(corners, ids)
                if len(obj_points) >= 4:
                    success_b, rvec_b, tvec_b = cv2.solvePnP(
                        obj_points, img_points, active_matrix, dist_coeffs, flags=cv2.SOLVEPNP_IPPE_SQUARE
                    )
                    if success_b:
                        # 求解桌面平面方程
                        # 桌面 Z=0 在 Board 坐标系。相机坐标系下的平面法向量为 R_b 的第三列(Z轴)
                        R_b, _ = cv2.Rodrigues(rvec_b)
                        normal = R_b[:, 2] # 法向量 [A, B, C]
                        d_val = -float(np.dot(normal, tvec_b.flatten())) # 截距 D
                        
                        with state_lock:
                            latest_table_plane = {
                                "normal": [float(normal[0]), float(normal[1]), float(normal[2])],
                                "d": d_val,
                                "tvec": [float(tvec_b[0][0]), float(tvec_b[1][0]), float(tvec_b[2][0])],
                                "rvec": [float(rvec_b[0][0]), float(rvec_b[1][0]), float(rvec_b[2][0])],
                                "timestamp": time.time()
                            }
                        board_tracked = True
                        
                        # 在实时输出流中绘制 Board 的 3D 坐标轴（绿色粗线，原点为 ID 0 码中心）
                        cv2.drawFrameAxes(frame, active_matrix, dist_coeffs, rvec_b, tvec_b, 0.1, 3)
                        # 在原点上方写字标注 Board 状态
                        cv2.putText(frame, "TABLE BOARD ORIGIN (ID 0)", (int(img_points[0][0][0]), int(img_points[0][0][1]) - 15),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
            
            # 如果没有成功追踪到 Board，将全局平面清除，以使定位接口准确回退
            if not board_tracked:
                with state_lock:
                    latest_table_plane = None

            # 2. 单码解算并回传（维持原有的 WebSocket 和 HTTP 数据流）
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
                    
                    # 绘制检测框和中心点
                    cv2.aruco.drawDetectedMarkers(frame, corners, ids)
        else:
            # 视野内没有任何二维码，清空平面状态
            with state_lock:
                latest_table_plane = None

        # 更新全局定位状态
        loc_payload = {
            "detected": len(detected_markers) > 0,
            "timestamp": time.time(),
            "board_tracked": board_tracked,
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
            # 在视频帧左上角画一个 HUD 状态
            status_info = f"Board Calibration: {'READY' if BOARD_AVAILABLE else 'NONE'} | Active: {'TRACKED' if board_tracked else 'LOST'}"
            cv2.putText(frame, status_info, (15, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0) if board_tracked else (255, 255, 255), 2)
            
            # 适当限制 JPEG 编码质量以减少带宽并提高帧率
            ret_enc, jpeg_buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ret_enc:
                jpeg_bytes = jpeg_buf.tobytes()
                for q in list(active_video_queues):
                    loop.call_soon_threadsafe(q.put_nowait, jpeg_bytes)


# =========================================================================
# 🤖 异步加载 Florence-2 开集大模型
# =========================================================================

def load_florence_model():
    global florence_model, florence_processor, florence_device, florence_loading_status
    if not FLORENCE_AVAILABLE:
        florence_loading_status = "Dependencies missing"
        return
    
    florence_loading_status = "Loading"
    try:
        print("[ML Model] 正在后台线程初始化 Florence-2 模型...")
        # 针对 M1/M2/M3/M4 Mac 优化选择 mps 加速设备
        if torch.backends.mps.is_available():
            florence_device = "mps"
        elif torch.cuda.is_available():
            florence_device = "cuda"
        else:
            florence_device = "cpu"
            
        print(f"[ML Model] 选择的硬件加速推理设备: {florence_device}")
        model_id = "microsoft/Florence-2-base"
        
        # 加载分词/图像处理器和模型权重
        florence_processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
        florence_model = AutoModelForCausalLM.from_pretrained(
            model_id, 
            trust_remote_code=True,
            attn_implementation="sdpa"  # 强制使用 PyTorch 内置的 SDPA 算子，不依赖外部 flash_attn
        ).to(florence_device)
        
        florence_loading_status = "Ready"
        print(f"[ML Model] ✅ Florence-2-base 成功加载到 [{florence_device}]，语义定位接口已准备就绪！")
    except Exception as e:
        florence_loading_status = f"Error: {e}"
        print(f"[ML Model] ❌ 自动加载 Florence-2 模型失败: {e}")


# =========================================================================
# 🌐 FastAPI Web 服务接口
# =========================================================================

app = FastAPI(
    title="iPhone ArUco Tracker Local Server",
    description="支持多客户端复用与 Florence-2 语义 3D 空间定位的 API 本地网关",
    version="1.1.0"
)


@app.on_event("startup")
def startup_event():
    """
    网关启动入口：初始化 ArUco Board，开启大模型后台加载，并运行视频处理线程。
    """
    init_aruco_board()
    
    # 后台异步加载 Florence-2 大模型，防止阻塞服务启动
    threading.Thread(target=load_florence_model, daemon=True).start()
    
    # 开启视频流采集
    loop = asyncio.get_running_loop()
    try:
        parsed_url = urlparse(RTSP_URL)
        phone_ip = parsed_url.hostname or RTSP_URL.split("//")[1].split(":")[0]
    except Exception:
        phone_ip = "127.0.0.1"
        
    threading.Thread(target=video_processing_thread, args=(loop, phone_ip), daemon=True).start()


@app.get("/connectivity")
def check_connectivity():
    """
    HTTP 接口：返回当前服务器与手机（包括视频流及内参服务）的连通性状态。
    """
    with state_lock:
        is_camera_alive = camera_connected and (time.time() - last_frame_time < 3.0)
        return {
            "status": "success",
            "camera_connected": is_camera_alive,
            "intrinsics_connected": intrinsics_connected,
            "last_frame_time_elapsed": time.time() - last_frame_time if last_frame_time > 0 else -1
        }


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
            jpeg_bytes = await queue.get()
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


@app.get("/latest_frame")
def get_latest_frame():
    """
    HTTP 接口：获取当前最新的单张 JPEG 原始视频帧。
    """
    with state_lock:
        if latest_raw_frame is None:
            return {"status": "error", "message": "No frame captured yet"}
        frame_copy = latest_raw_frame.copy()
        
    ret_enc, jpeg_buf = cv2.imencode('.jpg', frame_copy)
    if not ret_enc:
        return {"status": "error", "message": "Failed to encode frame"}
        
    return StreamingResponse(io.BytesIO(jpeg_buf.tobytes()), media_type="image/jpeg")


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
            while queue.qsize() > 0:
                loc_data = queue.get_nowait()
            await websocket.send_json(loc_data)
    except WebSocketDisconnect:
        pass
    finally:
        active_loc_queues.remove(queue)


# =========================================================================
# 🎯 新增接口：大模型物体识别与 3D 光线投影接口
# =========================================================================

class LocateRequest(BaseModel):
    query: str
    mode: str = "segment"  # 可选："segment"（返回轮廓） 或 "bbox"（仅返回边界框）

@app.post("/locate_object")
async def locate_object(req: LocateRequest):
    global florence_model, florence_processor, florence_device, florence_loading_status
    t_start = time.time()
    
    # 1. 拦截检查环境依赖与状态
    if not FLORENCE_AVAILABLE:
        return {
            "status": "error",
            "message": "本地未安装 ML 环境依赖包。请执行 'pip install -r requirements_ml.txt' 后重试。"
        }
        
    if florence_loading_status == "Loading":
        return {
            "status": "loading",
            "message": "Florence-2 正在后台异步载入中，请几秒钟后重试..."
        }
        
    if florence_loading_status != "Ready" or florence_model is None:
        return {
            "status": "error",
            "message": f"大模型初始化异常。状态: {florence_loading_status}"
        }

    # 验证输入模式
    req_mode = req.mode.lower()
    if req_mode not in ["segment", "bbox"]:
        req_mode = "bbox"

    # 2. 从多线程缓冲区复制当前最新的一帧画面及相机内参、桌面平面配置
    with state_lock:
        if latest_raw_frame is None:
            return {
                "status": "error",
                "message": "当前未捕获到视频帧，请确保手机 RTSP 视频流连接正常。"
            }
        frame_copy = latest_raw_frame.copy()
        intrinsics = latest_intrinsics_data.copy()
        plane = latest_table_plane.copy() if latest_table_plane is not None else None

    # 将送入大模型推理的这一帧图片进行 JPEG + Base64 编码，保证客户端渲染和推理的一致性，防止时差偏移
    ret_enc, jpeg_buf = cv2.imencode('.jpg', frame_copy)
    if ret_enc:
        import base64
        image_base64 = base64.b64encode(jpeg_buf.tobytes()).decode('utf-8')
    else:
        image_base64 = None

    # BGR 转换成 PIL 图像
    h, w, _ = frame_copy.shape
    rgb_image = Image.fromarray(cv2.cvtColor(frame_copy, cv2.COLOR_BGR2RGB))
    
    # 3. 根据请求模式设定不同的 Prompt 与 Task
    if req_mode == "segment":
        prompt = f"<REFERRING_EXPRESSION_SEGMENTATION> {req.query}"
        task_name = "<REFERRING_EXPRESSION_SEGMENTATION>"
    else:  # bbox
        prompt = f"<CAPTION_TO_PHRASE_GROUNDING> {req.query}"
        task_name = "<CAPTION_TO_PHRASE_GROUNDING>"
    
    try:
        t_preprocess_start = time.time()
        inputs = florence_processor(text=prompt, images=rgb_image, return_tensors="pt").to(florence_device)
        t_preprocess_end = time.time()
        
        # 关闭梯度，节约计算显存与耗时
        t_model_start = time.time()
        with torch.no_grad():
            generated_ids = florence_model.generate(
                input_ids=inputs["input_ids"],
                pixel_values=inputs["pixel_values"],
                max_new_tokens=1024,
                num_beams=3
            )
        t_model_end = time.time()
            
        t_postprocess_start = time.time()
        generated_text = florence_processor.batch_decode(generated_ids, skip_special_tokens=False)[0]
        parsed_answer = florence_processor.post_process_generation(
            generated_text, 
            task=task_name, 
            image_size=rgb_image.size
        )
        t_postprocess_end = time.time()
        
        raw_res = parsed_answer.get(task_name, {})
        
        preprocess_ms = (t_preprocess_end - t_preprocess_start) * 1000
        model_ms = (t_model_end - t_model_start) * 1000
        postprocess_ms = (t_postprocess_end - t_postprocess_start) * 1000
        total_ml_ms = (t_postprocess_end - t_preprocess_start) * 1000
        
        print(f"\n[Timing Check] Mode: {req_mode} | Query: '{req.query}'")
        print(f"  - Preprocess (Image/Text Tokenization): {preprocess_ms:.2f} ms")
        print(f"  - Model Inference (generate method): {model_ms:.2f} ms")
        print(f"  - Postprocess (Decode & Post-processing): {postprocess_ms:.2f} ms")
        print(f"  - Total ML Pipeline latency: {total_ml_ms:.2f} ms")
        
        # 4. 根据模式解析结果并计算 Bounding Box、Polygons 和 2D 接触点
        polygons = None
        
        if req_mode == "segment":
            polygons = raw_res.get("polygons", [])
            labels = raw_res.get("labels", [])
            
            if not polygons or len(polygons) == 0 or len(polygons[0]) == 0:
                total_endpoint_ms = (time.time() - t_start) * 1000
                print(f"  - Total Endpoint: {total_endpoint_ms:.2f} ms (Not Detected)")
                return {
                    "status": "success",
                    "detected": False,
                    "message": f"在当前画面中未识别到符合 '{req.query}' 的物体轮廓。",
                    "query": req.query,
                    "mode": req_mode,
                    "timing_ms": {
                        "preprocess": preprocess_ms,
                        "model_inference": model_ms,
                        "postprocess": postprocess_ms,
                        "total_ml": total_ml_ms,
                        "total_endpoint": total_endpoint_ms
                    }
                }
                
            # 汇总多边形顶点以求解 bounding box
            all_pts = []
            for poly_list in polygons:
                for poly in poly_list:
                    pts = np.array(poly, dtype=np.float32).reshape(-1, 2)
                    all_pts.append(pts)
            all_pts = np.concatenate(all_pts, axis=0)
            
            xmin = float(np.min(all_pts[:, 0]))
            ymin = float(np.min(all_pts[:, 1]))
            xmax = float(np.max(all_pts[:, 0]))
            ymax = float(np.max(all_pts[:, 1]))
            
            # 接触点选取：多边形底端边缘点 X 均值
            ymax_val = np.max(all_pts[:, 1])
            bottom_threshold = ymax_val - 5.0
            bottom_pts = all_pts[all_pts[:, 1] >= bottom_threshold]
            u_contact = float(np.mean(bottom_pts[:, 0]))
            v_contact = float(ymax_val)
            label = labels[0] if (labels and labels[0]) else req.query
            
        else:  # bbox
            bboxes = raw_res.get("bboxes", [])
            labels = raw_res.get("labels", [])
            
            if not bboxes or len(bboxes) == 0:
                total_endpoint_ms = (time.time() - t_start) * 1000
                print(f"  - Total Endpoint: {total_endpoint_ms:.2f} ms (Not Detected)")
                return {
                    "status": "success",
                    "detected": False,
                    "message": f"在当前画面中未识别到符合 '{req.query}' 的物体边界框。",
                    "query": req.query,
                    "mode": req_mode,
                    "timing_ms": {
                        "preprocess": preprocess_ms,
                        "model_inference": model_ms,
                        "postprocess": postprocess_ms,
                        "total_ml": total_ml_ms,
                        "total_endpoint": total_endpoint_ms
                    }
                }
                
            bbox = bboxes[0]  # 取第一个置信度最高的框
            xmin, ymin, xmax, ymax = [float(val) for val in bbox]
            
            # 接触点选取：包围框底边中点
            u_contact = float((xmin + xmax) / 2.0)
            v_contact = ymax
            label = labels[0] if (labels and labels[0]) else req.query

        total_endpoint_ms = (time.time() - t_start) * 1000
        print(f"  - Total Endpoint: {total_endpoint_ms:.2f} ms")

        payload = {
            "status": "success",
            "detected": True,
            "query": req.query,
            "mode": req_mode,
            "label": label,
            "bbox_2d": [xmin, ymin, xmax, ymax],
            "polygons": polygons,  # segment 模式下有数据，bbox 模式下为 null
            "contact_point_2d": {"u": u_contact, "v": v_contact},
            "table_plane_calibrated": plane is not None,
            "image_base64": image_base64,  # 返回当时送入大模型的原始图片
            "position_3d": None,
            "timing_ms": {
                "preprocess": preprocess_ms,
                "model_inference": model_ms,
                "postprocess": postprocess_ms,
                "total_ml": total_ml_ms,
                "total_endpoint": total_endpoint_ms
            }
        }
        
        # 5. 解算 3D 射线与平面交点（若已标定桌面）
        if plane is not None and intrinsics["fx"] > 0:
            fx = intrinsics["fx"]
            fy = intrinsics["fy"]
            cx = intrinsics["cx"]
            cy = intrinsics["cy"]
            
            # 相机坐标系下的单位投影射线 ray_dir = K^-1 * [u, v, 1]^T
            x_ray = (u_contact - cx) / fx
            y_ray = (v_contact - cy) / fy
            z_ray = 1.0
            ray_dir = np.array([x_ray, y_ray, z_ray], dtype=np.float32)
            
            normal = np.array(plane["normal"], dtype=np.float32)  # 平面法向量 [A, B, C]
            d_val = plane["d"]                                    # 平面截距 D
            
            # 射线交点距离 t = -D / (normal · ray_dir)
            denom = np.dot(normal, ray_dir)
            if abs(denom) > 1e-5:
                t = -d_val / denom
                P_cam = t * ray_dir  # 相机系下的 3D 点坐标
                
                # 转换到以 ID 0 为原点的桌面刚体物理坐标系下
                # P_cam = R_b * P_table + tvec_b  =>  P_table = R_b^T * (P_cam - tvec_b)
                rvec_b = np.array(plane["rvec"], dtype=np.float32)
                tvec_b = np.array(plane["tvec"], dtype=np.float32)
                R_b, _ = cv2.Rodrigues(rvec_b)
                
                P_table = R_b.T @ (P_cam - tvec_b)
                
                payload["position_3d"] = {
                    "x_cm": float(P_table[0] * 100),
                    "y_cm": float(P_table[1] * 100),
                    "z_cm": float(P_table[2] * 100),
                    "distance_cm": float(np.linalg.norm(P_cam) * 100)
                }
                
        return payload
    except Exception as e:
        return {
            "status": "error",
            "message": f"大语言视觉模型推理异常: {e}"
        }


if __name__ == "__main__":
    import uvicorn
    # 绑定 127.0.0.1 本地网关接口
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
