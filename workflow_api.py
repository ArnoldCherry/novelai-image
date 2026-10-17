"""Helpers for NovelAI image augmentation and inpainting requests."""

from base64 import b64decode, b64encode
from copy import deepcopy
from io import BytesIO
from typing import Any, Dict, List

from PIL import Image

from .client import NovelAIClient, validate_image
from .exceptions import NovelAIError


_DIRECTOR_TOOL_TYPES = {
    "表情": "emotion",
    "去背景": "bg-removal",
    "线稿": "lineart",
    "上色": "colorize",
    "emotion": "emotion",
    "removebg": "bg-removal",
    "bg-removal": "bg-removal",
    "lineart": "lineart",
    "colorize": "colorize",
}
# NovelAI公开前端模型枚举（2026-10-17）：1kcqqvyilqyxf.js。
_INPAINT_MODELS = {
    "nai-diffusion-4-5-full": "nai-diffusion-4-5-full-inpainting",
    "nai-diffusion-5-full": "nai-diffusion-5-full-inpainting",
    "nai-diffusion-4-5-full-inpainting": "nai-diffusion-4-5-full-inpainting",
    "nai-diffusion-5-full-inpainting": "nai-diffusion-5-full-inpainting",
}


def build_inpaint(payload: Dict[str, Any], source_base64: str, mask_base64: str, strength: float) -> Dict[str, Any]:
    """Build the documented image-generation infill request from a generation payload."""
    if not isinstance(payload, dict) or not isinstance(payload.get("parameters"), dict):
        raise NovelAIError("局部修改参数格式无效。")
    if payload.get("model") not in _INPAINT_MODELS:
        raise NovelAIError("当前模型不支持局部修改。")
    if isinstance(strength, bool) or not isinstance(strength, (int, float)) or not 0 <= strength <= 1:
        raise NovelAIError("局部修改强度须为 0～1。")

    source = validate_image(source_base64)
    mask = validate_image(mask_base64)
    try:
        with Image.open(BytesIO(b64decode(source))) as source_image:
            source_size = source_image.size
        with Image.open(BytesIO(b64decode(mask))) as mask_image:
            rgba_mask = mask_image.convert("RGBA")
            mask_alpha = rgba_mask.getchannel("A")
            if mask_alpha.getextrema() == (255, 255):
                # 旧式黑白遮罩没有 alpha 信息，退回用亮度当遮罩。
                mask_alpha = rgba_mask.convert("L")
            mask_size = rgba_mask.size
    except Exception:
        raise NovelAIError("原图或遮罩无法读取。") from None
    if source_size != mask_size:
        raise NovelAIError("遮罩尺寸必须与原图一致。")
    expected_size = (payload["parameters"].get("width"), payload["parameters"].get("height"))
    if expected_size != source_size:
        raise NovelAIError("原图尺寸与任务记录不一致。")
    # NovelAI 的遮罩只认 alpha：不透明=重绘，透明=保留。旧版发的是整张不透明的
    # 1-bit 黑白图，服务端会把整块矩形当成"面板"重绘，边界处留下方框和半透明层。
    alpha_mask = Image.new("RGBA", mask_size, (255, 255, 255, 0))
    alpha_mask.putalpha(mask_alpha)
    mask_buffer = BytesIO()
    alpha_mask.save(mask_buffer, format="PNG")
    mask = b64encode(mask_buffer.getvalue()).decode("ascii")

    result = deepcopy(payload)
    result["model"] = _INPAINT_MODELS[payload["model"]]
    params = result["parameters"]
    params["image"] = source
    params["mask"] = mask
    params["img2img"] = {"strength": float(strength)}
    params["n_samples"] = 1
    result["action"] = "infill"
    return result


async def director(client: NovelAIClient, image: str, tool: str, prompt: str = "") -> List[Dict[str, Any]]:
    """Run one of the supported Director Tools through NovelAI's augment-image API."""
    if tool not in _DIRECTOR_TOOL_TYPES:
        raise NovelAIError("导演工具仅支持 emotion、removebg、lineart、colorize。")
    if not isinstance(prompt, str) or len(prompt) > 500:
        raise NovelAIError("导演工具提示词格式无效或过长。")
    if _DIRECTOR_TOOL_TYPES[tool] == "emotion" and not prompt.strip():
        raise NovelAIError("emotion 工具需要填写表情标签，例如 happy。")
    if _DIRECTOR_TOOL_TYPES[tool] == "emotion":
        # 该端点的表情字段是类别而非任意标签串；happy, smile 会被服务端拒绝。
        for emotion, terms in (
            ("happy", r"happy|smil|cheerful|joy|grin|开心|高兴|微笑"),
            ("sad", r"sad|cry|tear|sorrow|难过|伤心|哭"),
            ("angry", r"angry|anger|furious|frown|生气|愤怒"),
            ("surprised", r"surpris|shock|惊讶|吃惊"),
        ):
            import re
            if re.search(terms, prompt, re.I):
                prompt = emotion
                break

    encoded_image = validate_image(image)
    try:
        with Image.open(BytesIO(b64decode(encoded_image))) as source:
            width, height = source.size
            original_size = source.size
            if max(source.size) > 768:
                prepared = source.convert("RGBA" if "A" in source.getbands() else "RGB")
                prepared.thumbnail((768, 768), Image.Resampling.LANCZOS)
                width, height = prepared.size
                buffer = BytesIO()
                prepared.save(buffer, "PNG")
                encoded_image = b64encode(buffer.getvalue()).decode("ascii")
    except Exception:
        raise NovelAIError("导演工具原图无法读取。") from None
    request = {
        "image": encoded_image,
        "prompt": prompt.strip(),
        "req_type": _DIRECTOR_TOOL_TYPES[tool],
        "defry": 0,
        "width": width,
        "height": height,
    }
    images = await client.augment(request)
    # 导演端处理大图不稳定；内部使用小图，结果恢复源画布，后续局部编辑仍可沿用尺寸。
    for image in images:
        with Image.open(BytesIO(b64decode(image["image"]))) as result:
            if result.size != original_size:
                buffer = BytesIO()
                result.resize(original_size, Image.Resampling.LANCZOS).save(buffer, "PNG")
                image["image"] = b64encode(buffer.getvalue()).decode("ascii")
    return images
