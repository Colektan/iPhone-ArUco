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
raw_intrinsics = None
intrinsics_connected = False

def intrinsics_reader_thread(ip, port):
    global raw_intrinsics, intrinsics_connected
    print(f"[Intrinsics] 正在连接内参服务器 -> {ip}:{port}")
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect((ip, port))
            with matrix_lock:
                intrinsics_connected = True
            print(f"[Intrinsics] ✅ 成功连接到内参服务器 {ip}:{port}")
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
                    print(f"[Intrinsics] 解析 JSON 数据失败: {e}")
            f.close()
            s.close()
        except (socket.error, socket.timeout):
            with matrix_lock:
                intrinsics_connected = False
                raw_intrinsics = None
            time.sleep(2.0)

def get_scaled_intrinsics(width, height, fallback_matrix):
    with matrix_lock:
        current_raw = raw_intrinsics
    if current_raw is None:
        return fallback_matrix
    cap_w = current_raw["w"] if current_raw["w"] > 0 else 1920
    cap_h = current_raw["h"] if current_raw["h"] > 0 else 1080
    is_swapped = (width > height) != (cap_w > cap_h)
    if is_swapped:
        scale_x = width / cap_h
        scale_y = height / cap_w
    else:
        scale_x = width / cap_w
        scale_y = height / cap_h
    return np.array([
        [current_raw["fx"] * scale_x, 0, current_raw["cx"] * scale_x],
        [0, current_raw["fy"] * scale_y, current_raw["cy"] * scale_y],
        [0, 0, 1]
    ], dtype=np.float32)

def main():
    # 配置参数
    rtsp_url = "rtsp://172.22.39.171:8554/"  # 默认与 detect_server 一致
    aruco_dict_type = cv2.aruco.DICT_4X4_50
    marker_length = 0.027  # 二维码物理边长（米）
    base_id = 0            # 桌面原点基准码

    print(f"载入 ArUco 字典 DICT_4X4_50 ...")
    aruco_dict = cv2.aruco.getPredefinedDictionary(aruco_dict_type)
    detector = cv2.aruco.ArucoDetector(aruco_dict, cv2.aruco.DetectorParameters())

    # 解析 IP 启动内参接收线程
    try:
        parsed_url = urlparse(rtsp_url)
        phone_ip = parsed_url.hostname or rtsp_url.split("//")[1].split(":")[0]
    except Exception:
        phone_ip = "127.0.0.1"

    t = threading.Thread(target=intrinsics_reader_thread, args=(phone_ip, 8555), daemon=True)
    t.start()

    # 启动视频采集
    print(f"正在连接视频流 -> {rtsp_url}")
    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print("❌ 错误：无法打开视频流。")
        return

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"视频流已连接，分辨率: {width}x{height}")

    # 默认备用内参
    focal_length = width * 0.8
    fallback_camera_matrix = np.array([
        [focal_length, 0, width / 2.0],
        [0, focal_length, height / 2.0],
        [0, 0, 1]
    ], dtype=np.float32)
    dist_coeffs = np.zeros((5, 1), dtype=np.float32)

    # 构造单码局部坐标系角点 (中心为原点)
    local_corners = np.array([
        [-marker_length / 2.0,  marker_length / 2.0, 0],
        [ marker_length / 2.0,  marker_length / 2.0, 0],
        [ marker_length / 2.0, -marker_length / 2.0, 0],
        [-marker_length / 2.0, -marker_length / 2.0, 0]
    ], dtype=np.float32)

    relative_transforms = {}

    cv2.namedWindow("ArUco Board Self-Calibration", cv2.WINDOW_AUTOSIZE)
    print("\n=========================================")
    print(" 🛠️  桌面多二维码自标定程序已启动")
    print(f" 基准码 ID: {base_id}")
    print(" 请将相机对准桌面，让 ID 0 和其他码同时出现在视野中")
    print(" 在不同角度和距离下缓缓移动相机，以便收集足够样本过滤噪声")
    print(" 按下 's' 键：保存标定数据并退出")
    print(" 按下 'q' 键：取消并退出")
    print("=========================================\n")

    while True:
        ret, frame = cap.read()
        if not ret:
            continue

        active_matrix = get_scaled_intrinsics(width, height, fallback_camera_matrix)
        corners, ids, _ = detector.detectMarkers(frame)

        if ids is not None and len(ids) > 1:
            ids_list = ids.flatten().tolist()
            if base_id in ids_list:
                base_idx = ids_list.index(base_id)
                # 计算基准码位姿
                success_base, r_base, t_base = cv2.solvePnP(
                    local_corners, corners[base_idx][0], active_matrix, dist_coeffs, flags=cv2.SOLVEPNP_IPPE_SQUARE
                )
                if success_base:
                    R_base, _ = cv2.Rodrigues(r_base)
                    T_0 = np.eye(4)
                    T_0[:3, :3] = R_base
                    T_0[:3, 3] = t_base.flatten()
                    T_0_inv = np.linalg.inv(T_0)

                    for idx, target_id in enumerate(ids_list):
                        if target_id == base_id:
                            continue

                        success_tgt, r_tgt, t_tgt = cv2.solvePnP(
                            local_corners, corners[idx][0], active_matrix, dist_coeffs, flags=cv2.SOLVEPNP_IPPE_SQUARE
                        )
                        if success_tgt:
                            R_tgt, _ = cv2.Rodrigues(r_tgt)
                            T_i = np.eye(4)
                            T_i[:3, :3] = R_tgt
                            T_i[:3, 3] = t_tgt.flatten()

                            # 计算目标码相对于基准码的相对变换
                            # T_rel = T_0^-1 * T_i
                            T_rel = T_0_inv @ T_i
                            if target_id not in relative_transforms:
                                relative_transforms[target_id] = []
                            relative_transforms[target_id].append(T_rel)

                # 画图反馈，显示哪些码正在参与标定
                cv2.aruco.drawDetectedMarkers(frame, corners, ids)
                for idx, target_id in enumerate(ids_list):
                    if target_id != base_id and target_id in relative_transforms:
                        count = len(relative_transforms[target_id])
                        text_pos = (int(corners[idx][0][0][0]), int(corners[idx][0][0][1]) - 10)
                        cv2.putText(frame, f"Calibrating: {count} frames", text_pos,
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

        # 渲染 HUD 状态
        status_text = f"Intrinsics: {'Connected' if raw_intrinsics is not None else 'Disconnected'}"
        cv2.putText(frame, status_text, (15, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        cv2.putText(frame, "Press 's' to Save & Exit | 'q' to Quit", (15, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

        cv2.imshow("ArUco Board Self-Calibration", frame)
        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            print("❌ 标定已取消。")
            break
        elif key == ord('s'):
            # 保存标定数据
            board_config = {
                "marker_length": marker_length,
                "base_id": base_id,
                "markers": []
            }
            # 基础码（ID 0）在桌面系下直接就是局部角点
            board_config["markers"].append({
                "id": base_id,
                "corners_3d": local_corners.tolist()
            })

            # 计算其他码的平均相对位姿
            for target_id, transforms in relative_transforms.items():
                if len(transforms) < 5:
                    print(f"⚠️ 警告: ID {target_id} 帧数不足 5 帧 ({len(transforms)} 帧)，将被忽略。")
                    continue

                # 平移取均值，旋转转换为四元数取均值，或直接 SVD 正交化均值矩阵
                avg_T = np.mean(transforms, axis=0)
                # 重新正交化 R 矩阵以防发生畸变
                U, _, Vt = np.linalg.svd(avg_T[:3, :3])
                avg_R = U @ Vt

                # 计算目标码在桌面系（即基准码坐标系）下的 4 个角点坐标
                world_corners = []
                for pt in local_corners:
                    pt_w = avg_R @ pt + avg_T[:3, 3]
                    world_corners.append(pt_w.tolist())

                board_config["markers"].append({
                    "id": int(target_id),
                    "corners_3d": world_corners
                })
                print(f"✅ 成功标定 ID {target_id} (样本量: {len(transforms)} 帧)")

            if len(board_config["markers"]) > 1:
                with open("custom_board_config.json", "w") as f:
                    json.dump(board_config, f, indent=4)
                print("\n🎉 自标定成功！配置文件已保存为 custom_board_config.json")
            else:
                print("\n❌ 错误：未收集到除基准码以外的其他码的有效位姿，保存失败。")
            break

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
