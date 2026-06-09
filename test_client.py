import cv2
import threading
import json
import asyncio
import websockets

# =========================================================================
# 📌 本地网关服务器连接配置
# =========================================================================
WS_URL = "ws://127.0.0.1:8000/ws/localization"
VIDEO_URL = "http://127.0.0.1:8000/video_feed"

# 用于优雅退出的全局标志位
running = True


async def websocket_listener():
    """
    异步子任务：通过 WebSocket 实时订阅服务端的定位解算结果并在终端输出。
    """
    global running
    print(f"[WS Client] 正在连接定位数据接口: {WS_URL}...")
    
    while running:
        try:
            async with websockets.connect(WS_URL) as websocket:
                print("[WS Client] ✅ 成功订阅实时定位数据流！")
                while running:
                    # 接收并反序列化 JSON 格式的位姿数据
                    message = await websocket.recv()
                    data = json.loads(message)
                    
                    if data["detected"]:
                        for m in data["markers"]:
                            pos = m["position_3d"]
                            rot = m["roll_3d"]
                            print(f"[WS Client] 检测到 ID: {m['id']} | "
                                  f"位置 -> X:{pos['x']:6.1f}cm, Y:{pos['y']:6.1f}cm, Z:{pos['z']:6.1f}cm (距离: {m['distance']:.1f}cm) | "
                                  f"旋转角 -> Roll:{rot:6.1f}°")
                    else:
                        print("[WS Client] ⚠️ 当前未检测到任何 ArUco 标记...")
        except websockets.exceptions.ConnectionClosed:
            print("[WS Client] ⚠️ 连接断开，将在 2 秒后自动尝试重新连接...")
            await asyncio.sleep(2.0)
        except Exception as e:
            if running:
                print(f"[WS Client] ❌ 连接错误 ({e})，将在 2 秒后自动重试...")
                await asyncio.sleep(2.0)


def start_websocket_thread():
    """
    在独立线程中启动 asyncio 事件循环以运行 WebSocket 监听任务。
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(websocket_listener())
    except Exception as e:
        print(f"[WS Client] 异常退出: {e}")


def main():
    global running
    
    # 1. 启动后台 WebSocket 接收线程
    ws_thread = threading.Thread(target=start_websocket_thread, daemon=True)
    ws_thread.start()
    
    # 2. 主线程启动 OpenCV 订阅服务端的 HTTP MJPEG 视频流
    # OpenCV 能够原生解析 HTTP MJPEG 边界并当作普通摄像头流读取，极具兼容性
    print(f"\n[Video Client] 正在连接视频流接口: {VIDEO_URL}...")
    cap = cv2.VideoCapture(VIDEO_URL)
    
    if not cap.isOpened():
        print("[Video Client] ❌ 错误：无法打开视频流，请检查网关服务端是否已运行。")
        running = False
        return

    print("[Video Client] ✅ 成功拉取视频流！按键盘 'q' 键退出预览。")
    cv2.namedWindow("Gateway Client Feed Preview", cv2.WINDOW_AUTOSIZE)

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("[Video Client] 读取帧失败，视频流已中断。")
                break
                
            # 在主线程渲染画面窗口（macOS 限制窗口渲染必须在主线程执行）
            cv2.imshow("Gateway Client Feed Preview", frame)
            
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
    except KeyboardInterrupt:
        print("\n[Client] 检测到键盘中断信号...")
    finally:
        running = False
        cap.release()
        cv2.destroyAllWindows()
        print("[Client] 👋 已断开连接，测试客户端安全退出。")


if __name__ == "__main__":
    main()
