import sys
import builtins

# =========================================================================
# ⚠️ MCP 必须拦截 stdout 以防止日志破坏 JSON-RPC 通信通道
# =========================================================================
# 将所有系统和库的 print 调用默认重定向到 sys.stderr
_original_print = builtins.print
def custom_print(*args, **kwargs):
    if 'file' not in kwargs or kwargs['file'] is None:
        kwargs['file'] = sys.stderr
    _original_print(*args, **kwargs)
builtins.print = custom_print

import json
import asyncio
import urllib.request
import urllib.error
import base64

# MCP SDK 相关导入
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.lowlevel import NotificationOptions
import mcp.types as types
from mcp.server.stdio import stdio_server

# FastAPI 定位网关的基本 URL
BASE_URL = "http://127.0.0.1:8000"

def get_http_json(path):
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=5.0) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.URLError as e:
        raise ConnectionError(
            f"❌ 无法连接到本地定位服务 ({url})。\n"
            f"请先运行 `scripts/run_server_mac.sh` 或启动 `detect_server.py` 确保本地服务已开启。"
        ) from e

def post_http_json(path, data_dict):
    url = f"{BASE_URL}{path}"
    data_bytes = json.dumps(data_dict).encode('utf-8')
    req = urllib.request.Request(
        url, 
        data=data_bytes, 
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'}
    )
    try:
        with urllib.request.urlopen(req, timeout=60.0) as response:  # 针对 VLM 推理使用较长超时
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.URLError as e:
        raise ConnectionError(
            f"❌ 无法连接到本地定位服务 ({url})。\n"
            f"请先运行 `scripts/run_server_mac.sh` 或启动 `detect_server.py` 确保本地服务已开启。"
        ) from e

def get_http_bytes(path):
    url = f"{BASE_URL}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5.0) as response:
            return response.read()
    except urllib.error.URLError as e:
        raise ConnectionError(
            f"❌ 无法连接到本地定位服务 ({url})。\n"
            f"请先运行 `scripts/run_server_mac.sh` 或启动 `detect_server.py` 确保本地服务已开启。"
        ) from e

def check_server_and_phone_connectivity():
    try:
        # 尝试从本地服务获取连通性状态
        return get_http_json("/connectivity")
    except ConnectionError as e:
        # 本地服务本身未运行或不可达
        return {
            "status": "error",
            "message": (
                "❌ 无法连接到本地定位服务。\n"
                "请先在电脑终端运行 `scripts/run_server_mac.sh` 或启动 `detect_server.py` 确保本地服务已开启。"
            ),
            "camera_connected": False,
            "intrinsics_connected": False
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"❌ 检查连通性时发生未知错误: {e}",
            "camera_connected": False,
            "intrinsics_connected": False
        }


# =========================================================================
# 🛠️ MCP 服务器定义
# =========================================================================

mcp_server = Server("iphone-aruco-tracker")

@mcp_server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_localization",
            description="获取当前摄像头中所有已识别 of ArUco 标记的 6D 姿态和 3D/2D 位置坐标信息。注意：此工具依赖外部的 detect_server.py 服务，必须由用户在终端手动启动（如运行 scripts/ 中的启动脚本），大模型请勿自动启动该服务。",
            inputSchema={"type": "object", "properties": {}}
        ),
        types.Tool(
            name="locate_object_3d",
            description=(
                "使用 Florence-2 视觉大模型在画面中定位指定的日常物体，并计算其在以 ID 0 为原点的 3D 物理桌面坐标系下的坐标。"
                "注意：此工具依赖外部的 detect_server.py 服务，必须由用户在终端手动启动（如运行 scripts/ 中的启动脚本），大模型请勿自动启动该服务。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要定位的物体描述标签（英文，如 'mug', 'pen'）。"},
                    "mode": {
                        "type": "string",
                        "enum": ["segment", "bbox"],
                        "default": "segment",
                        "description": "检测模式：segment (高精度多边形轮廓) 或 bbox (边界框模式)。"
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="get_camera_intrinsics",
            description="获取相机当前的实时内参参数（fx, fy, cx, cy）及连接状态。注意：此工具依赖外部的 detect_server.py 服务，必须由用户在终端手动启动，大模型请勿自动启动该服务。",
            inputSchema={"type": "object", "properties": {}}
        ),
        types.Tool(
            name="get_latest_frame",
            description="获取摄像头当前最新的单张 JPEG 原始视频帧并以图片形式返回，供大模型进行视觉观察与分析。注意：此工具依赖外部的 detect_server.py 服务，必须由用户在终端手动启动，大模型请勿自动启动该服务。",
            inputSchema={"type": "object", "properties": {}}
        )
    ]


@mcp_server.list_prompts()
async def handle_list_prompts() -> list[types.Prompt]:
    return [
        types.Prompt(
            name="tracker_guide",
            description="获取关于 iPhone ArUco Tracker 系统的整体背景说明、硬件连接指导、坐标系定义以及工具链最佳实践指导。",
            arguments=[]
        )
    ]

@mcp_server.get_prompt()
async def handle_get_prompt(
    name: str,
    arguments: dict[str, str] | None
) -> types.GetPromptResult:
    if name == "tracker_guide":
        guide_text = (
            "### iPhone ArUco Tracker 系统整体背景与指南\n\n"
            "本 MCP 服务提供了一个基于 iPhone 摄像头的低延迟桌面空间定位和语义识别系统。\n\n"
            "#### 1. 硬件与网络拓扑\n"
            "- **视频源 (RTSP)**: iPhone 通过 H264 视频流（默认端口 8554）向电脑推送实时视频。\n"
            "- **相机内参 (TCP)**: iPhone 的推流 App 在端口 8555 实时同步相机的焦距、中心点等物理光学参数，用于 3D 精准投影解算。\n"
            "- **网络模式**: 默认通过 USB 有线连接并使用 `pymobiledevice3` 将手机端口映射至本地 `127.0.0.1` 以消除网络抖动和延迟；也可以通过 Wi-Fi 直连。\n\n"
            "#### 2. 桌面标定与 3D 物理坐标系\n"
            "- **基准平面**: 在桌面上放置由多个 ArUco 二维码标记（通常是 4x4 格式，如 ID 0, ID 1 等）构成的 ArUco Board（标定板）。\n"
            "- **原点定义**: 系统以 **ArUco Board 的基准码 ID 0 的中心** 作为物理坐标系的原点 (0, 0, 0)。\n"
            "- **轴向定义**: X 轴与 Y 轴构成桌面物理平面，Z 轴垂直桌面向上。物理长度单位全部为 **厘米 (cm)**。\n"
            "- **标定机制**: 必须在画面中检测到标定板，系统才能求解出当前相机相对于桌面平面的位置，从而建立 3D 射线投影的交点计算。若标定板未被追踪（Lost），则无法获取物体的 3D 物理桌面坐标，只能获取 2D 画面接触点。\n\n"
            "#### 3. 语义物体定位原理\n"
            "- 系统整合了 **Florence-2 视觉大语言模型**。\n"
            "- 针对给定的物体英文标签（如 'battery', 'mug', 'pen'），模型在当前最新视频帧中进行 2D 的多边形分割（segment）或边界框（bbox）识别，找到物体与桌面的物理“接触点”（通常选取物体包围框的底边中心点或多边形底边缘的均值点）。\n"
            "- 随后，系统会从相机中心向该 2D 接触点射出一条 3D 射线，求其与已标定桌面平面的交点，最终解算出物体在以 ID 0 为原点的桌面坐标系中的 3D 坐标 (X_cm, Y_cm, Z_cm)。\n\n"
            "#### 4. 模型调用工具的最佳实践步骤\n"
            "当你需要协助用户定位物体或检查状态时，请遵循以下流程：\n"
            "注意：本地检测定位服务端（detect_server.py）应当由用户在外部终端运行，大模型【不需要也不应该】自己尝试运行终端命令去启动该服务端。若连接失败，请直接提示并引导用户手动运行 `scripts/` 目录下的相应启动脚本。\n"
            "1. **检查连接**: 运行 `get_camera_intrinsics` 验证相机内参和推流通道是否成功连接。\n"
            "2. **确认标定**: 运行 `get_localization` 确认当前视野内是否有 ArUco 标记并且桌面标定板处于已追踪状态 (Tracked)。如果未追踪，提醒用户把标定板放入视野。\n"
            "3. **执行定位**: 使用 `locate_object_3d` 传入物体描述和检测模式，解算出 3D 坐标，并可向用户展示最新的画面帧（`get_latest_frame`）来确认识别边界。"
        )
        return types.GetPromptResult(
            description="iPhone ArUco Tracker 总体背景与使用手册",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(
                        type="text",
                        text=guide_text
                    )
                )
            ]
        )
    raise ValueError(f"未知 Prompt: {name}")


@mcp_server.call_tool()
async def handle_call_tool(
    name: str,
    arguments: dict | None
) -> list[types.TextContent | types.ImageContent]:
    
    # 检查本地服务和手机连接的连通性
    conn = check_server_and_phone_connectivity()
    if conn.get("status") == "error":
        return [types.TextContent(type="text", text=conn.get("message"))]
        
    if name in ["get_localization", "locate_object_3d", "get_latest_frame"]:
        if not conn.get("camera_connected"):
            return [types.TextContent(
                type="text", 
                text="❌ 手机摄像头视频流未连通。\n"
                     "请确认：\n"
                     "1. 手机端视频推流服务已开启，且 RTSP 视频流连接正常。\n"
                     "2. 手机与电脑在同一局域网下，或者 USB 有线连接端口转发服务已启动。\n"
                     "请检查设备连通性后重试。"
            )]
            
    if name == "get_camera_intrinsics":
        if not conn.get("intrinsics_connected"):
            return [types.TextContent(
                type="text", 
                text="❌ 手机相机内参同步通道未连通。\n"
                     "请确认：\n"
                     "1. 手机端内参同步服务（端口 8555）已开启。\n"
                     "2. 手机与电脑的连接正常。\n"
                     "请检查设备连通性后重试。"
            )]

    try:
        # ----------------- 1. get_localization -----------------
        if name == "get_localization":
            data = get_http_json("/localization")
            
            if not data.get("detected"):
                return [types.TextContent(type="text", text="当前视野内未检测到任何 ArUco 标记。")]
            
            markers_info = []
            board_status = "已追踪 (Tracked)" if data.get("board_tracked") else "未追踪 (Lost)"
            markers_info.append(f"桌面标定状态: {board_status}\n")
            
            for marker in data["markers"]:
                pos = marker["position_3d"]
                markers_info.append(
                    f"- [ID {marker['id']}]:\n"
                    f"  * 3D物理坐标(相对于相机): X={pos['x']:.2f}cm, Y={pos['y']:.2f}cm, Z={pos['z']:.2f}cm\n"
                    f"  * 旋转角: Roll={marker['roll_3d']:.1f}°, Pitch={marker['pitch_3d']:.1f}°, Yaw={marker['yaw_3d']:.1f}°\n"
                    f"  * 直线距离: {marker['distance']:.2f}cm\n"
                    f"  * 2D中心点: U={marker['center_2d']['u']:.1f}, V={marker['center_2d']['v']:.1f}"
                )
            return [types.TextContent(type="text", text="\n".join(markers_info))]

        # ----------------- 2. locate_object_3d -----------------
        elif name == "locate_object_3d":
            query = arguments.get("query")
            mode = arguments.get("mode", "segment")
            
            res_data = post_http_json("/locate_object", {"query": query, "mode": mode})
            
            if res_data.get("status") == "loading":
                return [types.TextContent(type="text", text=f"模型正在加载中：{res_data.get('message')}")]
            
            if res_data.get("status") != "success":
                return [types.TextContent(type="text", text=f"算法定位出错: {res_data.get('message')}")]
            
            if not res_data.get("detected"):
                return [types.TextContent(type="text", text=f"在画面中找不到物体：'{query}'。")]
            
            pos = res_data.get("position_3d")
            label = res_data.get("label", query)
            
            if pos:
                text_result = (
                    f"✅ 成功定位物体 [{label}]：\n"
                    f"- 桌面物理坐标 (以 ID 0 为原点): X={pos['x_cm']:.2f} cm, Y={pos['y_cm']:.2f} cm, Z={pos['z_cm']:.2f} cm\n"
                    f"- 相机物理距离: {pos['distance_cm']:.2f} cm\n"
                    f"- 2D 画面接触点坐标: U={res_data['contact_point_2d']['u']:.1f}, V={res_data['contact_point_2d']['v']:.1f}"
                )
            else:
                text_result = (
                    f"⚠️ 已识别到物体 [{label}]，但当前桌面尚未标定（未追踪到标定板）。\n"
                    f"- 2D 画面接触点坐标: U={res_data['contact_point_2d']['u']:.1f}, V={res_data['contact_point_2d']['v']:.1f}\n"
                    f"- 提示: 请让摄像头看到 ArUco Board 后再进行 3D 定位。"
                )
                
            results = [types.TextContent(type="text", text=text_result)]
            
            img_base64 = res_data.get("image_base64")
            if img_base64:
                results.append(
                    types.ImageContent(
                        type="image",
                        data=img_base64,
                        mimeType="image/jpeg"
                    )
                )
            return results

        # ----------------- 3. get_camera_intrinsics -----------------
        elif name == "get_camera_intrinsics":
            data = get_http_json("/intrinsics")
            status_conn = "已连接 (Connected)" if data.get("connected") else "未连接 (Disconnected)"
            info = (
                f"相机内参数据来源: {data.get('source')}\n"
                f"内参数据接收通道: {status_conn}\n"
                f"- 焦距 fx: {data.get('fx')}\n"
                f"- 焦距 fy: {data.get('fy')}\n"
                f"- 中心 cx: {data.get('cx')}\n"
                f"- 中心 cy: {data.get('cy')}\n"
                f"- 视频流物理分辨率: {data.get('width')}x{data.get('height')}"
            )
            return [types.TextContent(type="text", text=info)]

        # ----------------- 4. get_latest_frame -----------------
        elif name == "get_latest_frame":
            img_bytes = get_http_bytes("/latest_frame")
            img_b64 = base64.b64encode(img_bytes).decode('utf-8')
            return [
                types.ImageContent(
                    type="image",
                    data=img_b64,
                    mimeType="image/jpeg"
                )
            ]

        else:
            raise ValueError(f"未知工具: {name}")
            
    except ConnectionError as ce:
        return [types.TextContent(type="text", text=str(ce))]
    except Exception as e:
        return [types.TextContent(type="text", text=f"❌ MCP 内部执行错误: {str(e)}")]

async def run_mcp_main():
    async with stdio_server() as (read_stream, write_stream):
        await mcp_server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="iphone-aruco-tracker",
                server_version="1.1.0",
                capabilities=mcp_server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

if __name__ == "__main__":
    asyncio.run(run_mcp_main())
