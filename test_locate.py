import requests
import json
import sys
import cv2
import numpy as np
import os

def main():
    if len(sys.argv) < 2:
        print("用法: python test_locate.py <物体名称> [模式: segment/bbox]")
        print("例如: ")
        print("  python test_locate.py keyboard          (默认轮廓分割模式)")
        print("  python test_locate.py keyboard bbox     (边界框检测模式)")
        return

    query = sys.argv[1]
    
    # 默认使用 segment 模式，支持通过第三个参数指定为 bbox 模式
    mode = "segment"
    if len(sys.argv) >= 3:
        arg_mode = sys.argv[2].lower()
        if arg_mode in ["segment", "bbox"]:
            mode = arg_mode

    url_locate = "http://127.0.0.1:8000/locate_object"
    url_frame = "http://127.0.0.1:8000/latest_frame"
    
    headers = {"Content-Type": "application/json"}
    payload = {
        "query": query,
        "mode": mode
    }

    print(f"1. 发送语义定位请求 -> 物体: '{query}' | 模式: '{mode}'")
    try:
        # 发起定位请求
        response = requests.post(url_locate, headers=headers, json=payload)
        if response.status_code != 200:
            print(f"请求错误，状态码: {response.status_code}")
            return
            
        result = response.json()
        print("\n=== 服务端返回结果 ===")
        # 截断打印 polygons 和 image_base64 数据，防止终端刷屏
        print_result = result.copy()
        if "polygons" in print_result and result["polygons"] is not None:
            print_result["polygons"] = f"<Polygons data: {len(result['polygons'])} elements>"
        if "image_base64" in print_result and result["image_base64"] is not None:
            print_result["image_base64"] = f"<Base64 image data: {len(result['image_base64'])} chars>"
        print(json.dumps(print_result, indent=4, ensure_ascii=False))
        
        # 打印精致的耗时分析报告
        if "timing_ms" in result:
            t_info = result["timing_ms"]
            print("\n⏱️  --- 耗时分析报告 (Latency Breakdown) ---")
            print(f"  ├─ 1. 预处理 (图像缩放/文本 Token 编码):  {t_info.get('preprocess', 0):.2f} ms")
            print(f"  ├─ 2. 大模型推理 (Model.generate 方法):   {t_info.get('model_inference', 0):.2f} ms")
            print(f"  ├─ 3. 后处理 (文本解码/多边形解析):      {t_info.get('postprocess', 0):.2f} ms")
            print(f"  ├─ 4. 模型端总耗时 (Pipeline Latency):   {t_info.get('total_ml', 0):.2f} ms")
            print(f"  └─ 5. 接口端到端总时间 (Total Endpoint):   {t_info.get('total_endpoint', 0):.2f} ms")
            print("  -----------------------------------------")
            
        if not result.get("detected"):
            print(f"❌ 画面中未检测到 '{query}'。")
            return
            
        # 2. 直接从响应数据中解析并解码大模型进行推理时使用的那一帧图片，解决时差偏移问题
        print("\n2. 从响应数据中解码同步的原始帧图片...")
        img_b64 = result.get("image_base64")
        if not img_b64:
            print("错误：响应数据中未包含同步的图片 base64。")
            return
            
        import base64
        img_data = base64.b64decode(img_b64)
        img_arr = np.frombuffer(img_data, np.uint8)
        img = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)
        if img is None:
            print("解析同步图片失败。")
            return
            
        # 3. 根据返回的数据模式进行渲染
        bbox = result["bbox_2d"]  # [xmin, ymin, xmax, ymax]
        pt1 = (int(bbox[0]), int(bbox[1]))
        pt2 = (int(bbox[2]), int(bbox[3]))
        
        polygons = result.get("polygons")
        
        if mode == "segment" and polygons:
            # 创建半透明蒙版图层
            overlay = img.copy()
            # 绘制精确物体轮廓 (Polygons)
            for poly_list in polygons:
                for poly in poly_list:
                    pts = np.array(poly, dtype=np.int32).reshape((-1, 1, 2))
                    # 填充半透明绿色面罩
                    cv2.fillPoly(overlay, [pts], (0, 255, 0))
                    # 绘制亮绿色外轮廓线
                    cv2.polylines(img, [pts], isClosed=True, color=(0, 255, 0), thickness=2)
            # 混合蒙版图层 (35% 蒙版透明度)
            cv2.addWeighted(overlay, 0.35, img, 0.65, 0, img)
            # 绘制黄色细包围框作为辅助
            cv2.rectangle(img, pt1, pt2, (0, 255, 255), 1)
        else:
            # 仅绘制粗体绿色边界框 (BBox 模式)
            cv2.rectangle(img, pt1, pt2, (0, 255, 0), 2)
            
        # 4. 绘制接触点 (Contact Point 2D)
        contact = result["contact_point_2d"]
        cu, cv = int(contact["u"]), int(contact["v"])
        # 绘制红色圆圈和中心实心点
        cv2.circle(img, (cu, cv), 8, (0, 0, 255), 2)
        cv2.circle(img, (cu, cv), 3, (0, 0, 255), -1)
        
        # 5. 标注标签文字与3D坐标信息
        label_text = f"[{mode.upper()}] {result['label']}"
        pos_3d = result.get("position_3d")
        if pos_3d:
            info_text = f"3D: X:{pos_3d['x_cm']:.1f} Y:{pos_3d['y_cm']:.1f} Z:{pos_3d['z_cm']:.1f} cm"
        else:
            info_text = "3D Position: N/A (No Table Calib)"
            
        cv2.putText(img, label_text, (pt1[0], pt1[1] - 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        cv2.putText(img, info_text, (pt1[0], pt1[1] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)
        
        # 6. 保存标注图
        os.makedirs("tmp", exist_ok=True)
        save_path = os.path.join("tmp", "grounding_result.jpg")
        cv2.imwrite(save_path, img)
        print(f"\n✅ 渲染图片已成功保存至: {save_path}")
        
        # 7. 弹窗显示结果
        try:
            cv2.imshow("Florence-2 Locate Result", img)
            print("按任意键关闭预览窗口...")
            cv2.waitKey(0)
            cv2.destroyAllWindows()
        except Exception as gui_err:
            print(f"提示：由于未检测到 GUI 显示环境，跳过弹窗预览。({gui_err})")
            
    except Exception as e:
        print(f"连接或执行异常: {e}")

if __name__ == "__main__":
    main()
