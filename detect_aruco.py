import os
import cv2
import numpy as np
import socket
import json
import threading
import time
from urllib.parse import urlparse

# =========================================================================
# 🔒 全局变量：实时相机内参多线程同步
# =========================================================================
matrix_lock = threading.Lock()
raw_intrinsics = None  # 存储格式：{"fx": ..., "fy": ..., "cx": ..., "cy": ..., "w": ..., "h": ...}
intrinsics_connected = False


def intrinsics_reader_thread(ip, port):
    """
    后台线程：实时连接手机 TCP 服务端，接收并解析相机实时标定数据。
    数据格式示例：{"fx": 1420.5, "fy": 1420.5, "cx": 960.0, "cy": 540.0, "w": 1920, "h": 1080}
    """
    global raw_intrinsics, intrinsics_connected
    print(f"[Intrinsics Thread] 正在尝试连接相机内参 TCP 服务器 -> {ip}:{port}")
    
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect((ip, port))
            with matrix_lock:
                intrinsics_connected = True
            print(f"[Intrinsics Thread] ✅ 成功连接到内参服务器 {ip}:{port}")
            
            # 使用 makefile 机制按行读取 TCP stream 中的 JSON 字符
            f = s.makefile('r', encoding='utf-8')
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    with matrix_lock:
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
            with matrix_lock:
                intrinsics_connected = False
                raw_intrinsics = None
            print(f"[Intrinsics Thread] ⚠️ 连接断开或超时 ({e})，将在 2 秒后自动重试...")
            time.sleep(2.0)


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
    
    # 图像 Y 轴向下，因此计算夹角需修正 90 度以符合“正上为 0 度”的标准
    angle = np.degrees(np.arctan2(vec_up[1], vec_up[0])) + 90.0
    if angle > 180:
        angle -= 360
    elif angle < -180:
        angle += 360
    return angle, top_center


def save_image_async(img, filename):
    """
    异步保存图片，避免主线程写磁盘发生 I/O 阻塞从而导致视频流延迟增加
    """
    cv2.imwrite(filename, img)


def get_scaled_intrinsics(width, height, fallback_matrix):
    """
    获取当前已同步的相机内参，并根据实际显示的 RTSP 分辨率进行等比例缩放
    """
    with matrix_lock:
        current_raw = raw_intrinsics
        connected = intrinsics_connected

    if current_raw is None:
        return fallback_matrix, "Estimated", connected, None

    # 获取采集端的分辨率
    cap_w = current_raw["w"] if current_raw["w"] > 0 else 1920
    cap_h = current_raw["h"] if current_raw["h"] > 0 else 1080

    # 动态检测横竖屏方向是否调换并计算缩放比
    is_swapped = (width > height) != (cap_w > cap_h)
    if is_swapped:
        scale_x = width / cap_h
        scale_y = height / cap_w
    else:
        scale_x = width / cap_w
        scale_y = height / cap_h

    active_matrix = np.array([
        [current_raw["fx"] * scale_x, 0, current_raw["cx"] * scale_x],
        [0, current_raw["fy"] * scale_y, current_raw["cy"] * scale_y],
        [0, 0, 1]
    ], dtype=np.float32)

    return active_matrix, "Real-time", connected, current_raw


# =========================================================================
# 🎨 画面显示渲染函数 (Visualization & HUD)
# =========================================================================

def draw_hud(frame, matrix_source, active_matrix, connected, current_raw, tracker_mode, width, height):
    """
    在图像画面左上角绘制 HUD 数据，展示当前系统工作状态
    """
    # 1. 绘制当前的内参矩阵源和核心参数
    info_matrix = f"Intrinsics: {matrix_source} | fx:{active_matrix[0,0]:.1f} fy:{active_matrix[1,1]:.1f} cx:{active_matrix[0,2]:.1f} cy:{active_matrix[1,2]:.1f}"
    cv2.putText(frame, info_matrix, (15, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0) if connected else (0, 0, 255), 2)
    
    # 2. 绘制 TCP 连接信息
    if not connected or current_raw is None:
        cv2.putText(frame, "Intrinsics TCP: Disconnected (Port 8555)", (15, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
    else:
        raw_w = current_raw["w"]
        raw_h = current_raw["h"]
        cv2.putText(frame, f"Intrinsics TCP: Connected (Src: {raw_w}x{raw_h} -> RTSP: {width}x{height})", (15, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        
    # 3. 绘制运行模式
    mode_text = f"Mode: {tracker_mode} (Press 'm' to toggle)"
    cv2.putText(frame, mode_text, (15, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)


# =========================================================================
# 🚀 主程序入口 (Main Execution)
# =========================================================================

def main():
    # 📌 配置项：请替换为手机屏幕上显示的实际 RTSP 视频流链接
    rtsp_url = "rtsp://172.22.39.171:8554/"
    
    # 📌 配置项：ArUco 字典类型（要与您打印的规格保持一致）
    aruco_dict_type = cv2.aruco.DICT_4X4_50
    
    # 📌 配置项：打印出的 ArUco 标记黑色正方形的外框物理尺寸（单位：米）
    marker_length = 0.027
    
    # 初始化 ArUco 算法模块
    print(f"正在加载 ArUco 字典: {aruco_dict_type}...")
    aruco_dict = cv2.aruco.getPredefinedDictionary(aruco_dict_type)
    detector_params = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(aruco_dict, detector_params)
    
    # 解析连接 IP，启动 TCP 标定内参同步子线程
    try:
        parsed_url = urlparse(rtsp_url)
        phone_ip = parsed_url.hostname or rtsp_url.split("//")[1].split(":")[0]
    except Exception:
        phone_ip = "127.0.0.1"
    
    t = threading.Thread(target=intrinsics_reader_thread, args=(phone_ip, 8555), daemon=True)
    t.start()
    
    # 开启视频流采集
    print(f"正在建立 RTSP 连接 -> {rtsp_url}")
    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print("\n❌ 错误：无法打开视频流。请确保：")
        print("1. 手机端 App 正在正常开启并能显示摄像头画面；")
        print("2. 电脑和手机局域网内网通路无阻断（尝试 ping 手机 IP）；")
        print("3. 本机未有防火墙拦截 UDP 通讯。")
        return

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    print(f"✅ 视频连接成功！分辨率: {width}x{height} | 帧率: {fps} FPS\n")
 
    # 估算默认备用相机内参矩阵 (若 TCP 未连通则自动作为 fallback 使用)
    focal_length = width * 0.8
    fallback_camera_matrix = np.array([
        [focal_length, 0, width / 2.0],
        [0, focal_length, height / 2.0],
        [0, 0, 1]
    ], dtype=np.float32)
    
    dist_coeffs = np.zeros((5, 1), dtype=np.float32)
 
    # 初始化 ArUco 四角在标记局部坐标系（中心为原点）下的 3D 实界坐标点集
    marker_points = np.array([
        [-marker_length / 2.0,  marker_length / 2.0, 0],
        [ marker_length / 2.0,  marker_length / 2.0, 0],
        [ marker_length / 2.0, -marker_length / 2.0, 0],
        [-marker_length / 2.0, -marker_length / 2.0, 0]
    ], dtype=np.float32)
 
    tracker_mode = "3D"
    # 📌 配置项：是否开启无窗口极速定位模式（Headless Mode）
    # 设为 True 时，不创建 GUI 窗口，不进行画面标注和图像渲染，仅在终端输出定位坐标和保存测试图，延迟最低。
    headless_mode = False
 
    if not headless_mode:
        cv2.namedWindow("iPhone ArUco Tracker", cv2.WINDOW_AUTOSIZE)
    
    # 初始化用于延迟测试的定期非阻塞图片保存变量
    last_save_time = 0.0
    save_interval = 1.0  # 默认每秒保存一次
 
    while True:
        ret, frame = cap.read()
        if not ret:
            continue
            
        # 安全获取并动态缩放当前使用的相机内参矩阵
        active_matrix, matrix_source, connected, current_raw = get_scaled_intrinsics(
            width, height, fallback_camera_matrix
        )
            
        # 1. 图像检测二维码角点和 ID
        corners, ids, rejected = detector.detectMarkers(frame)
        
        # 2. 对检测成功的对象进行定位解算和标注
        if ids is not None and len(ids) > 0:
            if not headless_mode:
                cv2.aruco.drawDetectedMarkers(frame, corners, ids)
            
            for i in range(len(ids)):
                c2d = corners[i][0].astype(np.float32)
                
                # PnP 求解三维位姿矩阵
                success, rvec, tvec = cv2.solvePnP(
                    marker_points, c2d, active_matrix, dist_coeffs, flags=cv2.SOLVEPNP_IPPE_SQUARE
                )
                
                if success:
                    # 计算姿态数据
                    roll, pitch, yaw = compute_euler_angles(rvec)  # 3D 旋转
                    angle_2d, top_center = compute_2d_angle(c2d)  # 2D 角度
                    center_x, center_y = np.mean(c2d, axis=0)      # 2D 中心
                    
                    text_x, text_y = int(c2d[0][0]), int(c2d[0][1]) - 10
                    
                    # ----------------- 3D 模式绘制 -----------------
                    if tracker_mode == "3D":
                        if not headless_mode:
                            # 绘制 3D 坐标轴线
                            cv2.drawFrameAxes(frame, active_matrix, dist_coeffs, rvec, tvec, marker_length * 1.0, 2)
                        
                        x_cm = tvec[0][0] * 100
                        y_cm = tvec[1][0] * 100
                        z_cm = tvec[2][0] * 100
                        distance = np.linalg.norm(tvec) * 100
                        
                        info_pos = f"ID:{ids[i][0]} X:{x_cm:.1f} Y:{y_cm:.1f} Z:{z_cm:.1f} D:{distance:.1f}cm"
                        info_rot = f"Roll:{roll:.1f} Pitch:{pitch:.1f} Yaw:{yaw:.1f}"
                        
                        if not headless_mode:
                            cv2.putText(frame, info_pos, (text_x, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)
                            cv2.putText(frame, info_rot, (text_x, text_y + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)
                        
                        print(f"[{matrix_source} Intrinsics] [ID: {ids[i][0]}] "
                              f"Pos -> X:{x_cm:6.1f}cm Y:{y_cm:6.1f}cm Z:{z_cm:6.1f}cm Dist:{distance:5.1f}cm | "
                              f"Rot -> Roll:{roll:6.1f} Pitch:{pitch:6.1f} Yaw:{yaw:6.1f}")
                    
                    # ----------------- 2D 模式绘制 -----------------
                    else:
                        if not headless_mode:
                            # 绘制红中心圆点
                            cv2.circle(frame, (int(center_x), int(center_y)), 5, (0, 0, 255), -1)
                            # 绘制绿朝向轴线
                            cv2.line(frame, (int(center_x), int(center_y)), (int(top_center[0]), int(top_center[1])), (0, 255, 0), 2)
                        
                        info_pos_2d = f"ID:{ids[i][0]} Center:({center_x:.1f}, {center_y:.1f})px"
                        info_rot_2d = f"Angle:{angle_2d:.1f}deg (Roll:{roll:.1f}deg)"
                        
                        if not headless_mode:
                            cv2.putText(frame, info_pos_2d, (text_x, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)
                            cv2.putText(frame, info_rot_2d, (text_x, text_y + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)
                        
                        print(f"[2D Mode] [ID: {ids[i][0]}] Center -> U: {center_x:6.1f}px | V: {center_y:6.1f}px | 2D Angle: {angle_2d:5.1f}° | Roll:{roll:5.1f}°")

        # 叠加显示全局 HUD 属性
        if not headless_mode:
            draw_hud(frame, matrix_source, active_matrix, connected, current_raw, tracker_mode, width, height)

        # 定期非阻塞保存临时图像，用于延迟测试
        current_time = time.time()
        if current_time - last_save_time >= save_interval:
            last_save_time = current_time
            
            # 复制当前帧图片，防止多线程写冲突
            frame_to_save = frame.copy()
            
            # 获取并格式化当前精确到毫秒的系统时间戳
            time_struct = time.localtime(current_time)
            milliseconds = int((current_time - int(current_time)) * 1000)
            timestamp_str = f"{time.strftime('%H:%M:%S', time_struct)}.{milliseconds:03d}"
            
            # 在保存的图片右下角绘制红色的系统时间戳
            cv2.putText(
                frame_to_save, 
                timestamp_str, 
                (width - 240, height - 20), 
                cv2.FONT_HERSHEY_SIMPLEX, 
                0.8, 
                (0, 0, 255), 
                2
            )
            
            # 启动线程异步保存图片为唯一的覆写文件 temp_latency.jpg，防止文件堆积
            t_save = threading.Thread(
                target=save_image_async, 
                args=(frame_to_save, "temp_latency.jpg"), 
                daemon=True
            )
            t_save.start()

        if not headless_mode:
            cv2.imshow("iPhone ArUco Tracker", frame)
            
            # 键盘热键监听
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                break
            elif key == ord('m'):
                tracker_mode = "2D" if tracker_mode == "3D" else "3D"
                print(f"\n切换当前模式为: {tracker_mode}\n")
        else:
            # Headless 模式下仅进行微小睡眠或利用 pollKey 维持底层管道泵送，防死锁并能及时响应 Ctrl+C 中断
            cv2.waitKey(1)
 
    cap.release()
    if not headless_mode:
        cv2.destroyAllWindows()
    print("\n👋 程序已安全退出。")


if __name__ == "__main__":
    main()
