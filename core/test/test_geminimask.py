import httpx
import base64
import os # 用于处理 API 密钥
import asyncio
import json
import re
# python -m core.test.test_geminimask
from ..utils import get_image_message, safe_get

# --- 请替换为您的实际值 ---
MODEL_NAME = "gemini-2.5-flash-preview-04-17"
API_KEY = os.environ.get("GEMINI_API_KEY") # 从环境变量读取密钥
IMAGE_PATH = os.environ.get("IMAGE_PATH")
# --------------------------

# 检查 API 密钥是否存在
if not API_KEY:
    raise ValueError("请设置 GOOGLE_API_KEY 环境变量或在代码中提供 API 密钥。")

# 确定图片的 MIME 类型
# 您可以根据文件扩展名进行猜测，或者使用更可靠的库如 python-magic
if IMAGE_PATH.lower().endswith(".png"):
    IMAGE_MIME_TYPE = "image/png"
elif IMAGE_PATH.lower().endswith(".jpg") or IMAGE_PATH.lower().endswith(".jpeg"):
    IMAGE_MIME_TYPE = "image/jpeg"
# 添加其他您需要支持的图片类型
else:
    raise ValueError(f"不支持的图片格式: {IMAGE_PATH}")

# 读取图片文件并进行 Base64 编码
try:
    with open(IMAGE_PATH, "rb") as image_file:
        image_data = image_file.read()
        base64_encoded_image = base64.b64encode(image_data).decode("utf-8")
        # print(base64_encoded_image)
except FileNotFoundError:
    print(f"错误：找不到图片文件 '{IMAGE_PATH}'")
    exit()

# image_message = get_image_message(base64_encoded_image, "gemini")
image_message = asyncio.run(get_image_message(f"data:{IMAGE_MIME_TYPE};base64," + base64_encoded_image, "gemini"))

# 构建请求 URL
url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"

prompt = "Give the segmentation masks for the Search box. Output a JSON list of segmentation masks where each entry contains the 2D bounding box in \"box_2d\" and the mask in \"mask\"."
# 定义查询参数 (API Key)
params = {"key": API_KEY}

# 定义请求头
headers = {"Content-Type": "application/json"}

# 定义请求体 (JSON payload)
payload = {
    "contents": [
        {
            "parts": [
                {
                    "text": prompt
                },
                image_message
            ]
        }
    ],
    "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}},
}

# 发送 POST 请求
try:
    with httpx.Client() as client:
        response = client.post(url, params=params, headers=headers, json=payload, timeout=60.0) # 增加超时时间
        response.raise_for_status() # 如果状态码不是 2xx，则抛出异常

    # 您可以在这里添加代码来解析 response.json() 并提取分割掩码
    text = safe_get(response.json(), "candidates", 0, "content", "parts", 0, "text")
    # print(text)
    # 例如: segmentation_masks = response.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text')

except httpx.HTTPStatusError as exc:
    print(f"HTTP 错误发生: {exc.response.status_code} - {exc.response.text}")
except httpx.RequestError as exc:
    print(f"发送请求时出错: {exc}")
except Exception as e:
    print(f"发生意外错误: {e}")


regex_pattern = r'(\[\s*\{.*?\}\s*\])' # 匹配包含至少一个对象的数组

# 使用 re.search 查找第一个匹配项，re.DOTALL 使点号能匹配换行符
match = re.search(regex_pattern, text, re.DOTALL)

if match:
    # 提取匹配到的整个 JSON 数组字符串 (group 1 因为模式中有括号)
    json_string = match.group(1)

    try:
        # 使用 json.loads() 解析字符串
        parsed_data = json.loads(json_string)
        # 使用 json.dumps 美化打印输出
        print(json.dumps(parsed_data, indent=2, ensure_ascii=False))

        # 例如，获取第一个元素的 label
        if isinstance(parsed_data, list) and len(parsed_data) > 0:
            first_item = parsed_data[0]
            if isinstance(first_item, dict):
                label = first_item.get('label')
                print(f"\n第一个元素的 label 是: {label}")

    except json.JSONDecodeError as e:
        print(f"JSON 解析错误: {e}")
        print(f"出错的字符串是: {json_string}")
else:
    print("在文本中未找到匹配的 JSON 数组。")


import io
from PIL import Image, ImageDraw, ImageFont # pip install Pillow

def extract_box_and_mask_py(parsed_data):
    """
    从已解析的 JSON 数据中提取边界框 (box_2d) 和 Base64 编码的掩码 (mask) 数据。

    Args:
        parsed_data (list): 一个包含字典的列表，每个字典至少包含 'box_2d' 和 'mask' 键。
                            例如: [{'box_2d': [y1, x1, y2, x2], 'mask': 'data:image/png;base64,...'}, ...]

    Returns:
        list: 一个包含字典的列表，每个字典包含 'box' (坐标列表)
              和 'mask_base64' (Base64 字符串)。
              例如: [{'box': [y1, x1, y2, x2], 'mask_base64': '...'}, ...]
              坐标系假定为 0-1000 范围。
    """
    # 不再需要正则表达式
    results = []
    # 检查 parsed_data 是否为列表
    if not isinstance(parsed_data, list):
        print(f"Error: Input data is not a list. Received type: {type(parsed_data)}")
        return results

    for item in parsed_data:
        if not isinstance(item, dict):
            print(f"Skipping non-dictionary item in list: {item}")
            continue

        try:
            box = item.get('box_2d')
            mask_data_uri = item.get('mask')

            # 检查 'box_2d' 和 'mask' 是否存在且不为 None
            if box is None or mask_data_uri is None:
                print(f"Skipping item due to missing 'box_2d' or 'mask': {item}")
                continue

            # 从 mask 数据 URI 中提取 Base64 部分
            # 格式: "data:image/[^;]+;base64,..."
            if isinstance(mask_data_uri, str) and mask_data_uri.startswith('data:image/') and ';base64,' in mask_data_uri:
                mask_b64 = mask_data_uri.split(';base64,', 1)[1]
            else:
                print(f"Skipping item due to invalid mask format: {mask_data_uri}")
                continue

            # 验证 box 数据
            if isinstance(box, list) and len(box) == 4 and all(isinstance(n, int) for n in box):
                 results.append({"box": box, "mask_base64": mask_b64})
            else:
                 print(f"Skipping invalid box format: {box}")

        # 捕捉可能的 KeyError 或其他在字典访问/处理中发生的错误
        except Exception as e:
            print(f"Error processing item: {item}. Error: {e}")

    return results

def display_image_with_bounding_boxes_and_masks_py(
    original_image_path,
    box_and_mask_data,
    output_overlay_path="overlay_image.png",
    output_compare_dir="comparison_outputs"
):
    """
    在原始图像上绘制边界框和掩码，并生成裁剪区域与掩码的对比图。

    Args:
        original_image_path (str): 原始图像的文件路径。
        box_and_mask_data (list): extract_box_and_mask_py 的输出列表。
        output_overlay_path (str): 保存带有叠加效果的图像的路径。
        output_compare_dir (str): 保存对比图像的目录路径。
    """
    try:
        img_original = Image.open(original_image_path).convert("RGBA")
        img_width, img_height = img_original.size
    except FileNotFoundError:
        print(f"Error: Original image not found at {original_image_path}")
        return
    except Exception as e:
        print(f"Error opening original image: {e}")
        return

    # 创建一个副本用于绘制叠加效果
    img_overlay = img_original.copy()
    draw = ImageDraw.Draw(img_overlay, "RGBA") # 使用 RGBA 模式以支持透明度

    # 定义颜色列表
    colors_hex = ['#FF0000', '#00FF00', '#0000FF', '#FFFF00', '#FF00FF', '#00FFFF']
    # 将十六进制颜色转换为 RGBA 元组 (用于绘制)
    colors_rgba = []
    for hex_color in colors_hex:
        h = hex_color.lstrip('#')
        rgb = tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
        colors_rgba.append(rgb + (255,)) # (R, G, B, Alpha) - 边框完全不透明

    # 创建输出目录（如果不存在）
    import os
    os.makedirs(output_compare_dir, exist_ok=True)

    print(f"Found {len(box_and_mask_data)} box/mask pairs to process.")

    for i, data in enumerate(box_and_mask_data):
        box_0_1000 = data['box'] # [ymin, xmin, ymax, xmax] in 0-1000 range
        mask_b64 = data['mask_base64']
        color_index = i % len(colors_rgba)
        outline_color = colors_rgba[color_index]
        # 叠加掩码时使用半透明颜色
        mask_fill_color = outline_color[:3] + (int(255 * 0.7),) # 70% Alpha

        # --- 1. 坐标转换与验证 ---
        # 将 0-1000 坐标转换为图像像素坐标 (left, top, right, bottom)
        # 假设 box 是 [ymin, xmin, ymax, xmax]
        try:
            ymin_norm, xmin_norm, ymax_norm, xmax_norm = [c / 1000.0 for c in box_0_1000]

            left   = int(xmin_norm * img_width)
            top    = int(ymin_norm * img_height)
            right  = int(xmax_norm * img_width)
            bottom = int(ymax_norm * img_height)

            # 确保坐标在图像范围内且有效
            left = max(0, left)
            top = max(0, top)
            right = min(img_width, right)
            bottom = min(img_height, bottom)

            box_width_px = right - left
            box_height_px = bottom - top

            if box_width_px <= 0 or box_height_px <= 0:
                print(f"Skipping box {i+1} due to zero or negative dimensions after conversion.")
                continue

        except Exception as e:
            print(f"Error processing coordinates for box {i+1}: {box_0_1000}. Error: {e}")
            continue

        print(f"Processing Box {i+1}: Pixels(L,T,R,B)=({left},{top},{right},{bottom}) Color={colors_hex[color_index]}")

        # --- 2. 在叠加图像上绘制边界框 ---
        try:
            draw.rectangle([left, top, right, bottom], outline=outline_color, width=5)
        except Exception as e:
             print(f"Error drawing rectangle for box {i+1}: {e}")
             continue

        # --- 3. 处理并绘制掩码 ---
        try:
            # 解码 Base64 掩码数据
            mask_bytes = base64.b64decode(mask_b64)
            mask_img_raw = Image.open(io.BytesIO(mask_bytes)).convert("RGBA")

            # 将掩码图像缩放到边界框的像素尺寸
            mask_img_resized = mask_img_raw.resize((box_width_px, box_height_px), Image.Resampling.NEAREST)

            # 创建一个纯色块，应用掩码的 alpha 通道
            color_block = Image.new('RGBA', mask_img_resized.size, mask_fill_color)

            # 将带有透明度的颜色块粘贴到叠加图像上，使用掩码的 alpha 通道作为粘贴蒙版
            # mask_img_resized.split()[-1] 提取 alpha 通道
            img_overlay.paste(color_block, (left, top), mask=mask_img_resized.split()[-1])

        except base64.binascii.Error:
             print(f"Error: Invalid Base64 data for mask {i+1}.")
             continue
        except Exception as e:
             print(f"Error processing or drawing mask for box {i+1}: {e}")
             continue

        # --- 4. 生成对比图 ---
        try:
            # 从原始图像中裁剪出边界框区域
            img_crop = img_original.crop((left, top, right, bottom))

            # 准备掩码预览图（使用原始解码后的掩码，调整大小以匹配裁剪区域）
            # 这里直接使用缩放后的 mask_img_resized 的 RGB 部分可能更直观
            mask_preview = mask_img_resized.convert("RGB") # 转换为 RGB 以便保存为常见格式

            # 保存裁剪图和掩码预览图
            crop_filename = os.path.join(output_compare_dir, f"compare_{i+1}_crop.png")
            mask_filename = os.path.join(output_compare_dir, f"compare_{i+1}_mask.png")
            img_crop.save(crop_filename)
            mask_preview.save(mask_filename)
            print(f" - Saved comparison: {crop_filename}, {mask_filename}")

        except Exception as e:
            print(f"Error creating or saving comparison images for box {i+1}: {e}")

    # --- 5. 保存最终的叠加图像 ---
    try:
        img_overlay.save(output_overlay_path)
        print(f"\nOverlay image saved to: {output_overlay_path}")
        print(f"Comparison images saved in: {output_compare_dir}")
    except Exception as e:
        print(f"Error saving the final overlay image: {e}")


extracted_data = extract_box_and_mask_py(parsed_data)

if extracted_data:
    # 确保原始图像存在
    import os
    if os.path.exists(IMAGE_PATH):
            display_image_with_bounding_boxes_and_masks_py(
                IMAGE_PATH,
                extracted_data,
                output_overlay_path="python_overlay_output.png", # 输出带叠加效果的图片名
                output_compare_dir="python_comparison_outputs" # 输出对比图的文件夹名
            )
    else:
            print(f"Error: Cannot proceed with visualization, image file not found: {IMAGE_PATH}")
            print("Please update the 'IMAGE_PATH' variable in the script.")
else:
    print("No valid box and mask data found in the response text.")
