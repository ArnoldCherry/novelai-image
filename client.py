from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence
from PIL import Image
import base64
import asyncio
import binascii
import json
import logging
import os
import re

import httpx
from urllib.parse import urlparse

from .exceptions import NovelAIError
from .parser import DrawRequest

logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_RESPONSE_BYTES = 84 * 1024 * 1024
# 连接层失败的重试间隔（秒），随尝试次数递增。
_RETRY_BACKOFF_SECONDS = 2.0
OFFICIAL_URL = "https://image.novelai.net"


def resolve_api_token() -> str:
    """Return the process token, or the current Windows user's saved token."""
    token = os.environ.get("NOVELAI_API_TOKEN", "").strip()
    if token:
        return token

    try:
        import winreg
    except ImportError:
        return ""

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            token, _ = winreg.QueryValueEx(key, "NOVELAI_API_TOKEN")
    except OSError:
        return ""
    return str(token).strip()


def _normalize_proxy(proxy: Optional[str]) -> str:
    """校验并规范化显式代理地址；空值表示直连。

    只接受带主机名的 http/https 地址。该代理会看到 Authorization 头，
    因此必须由管理员显式配置，而不是从环境变量隐式继承。
    """
    value = str(proxy or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
    except ValueError:
        raise NovelAIError("配置错误：api.proxy 不是有效的地址。") from None
    if parsed.scheme.lower() not in ("http", "https") or not hostname:
        raise NovelAIError("配置错误：api.proxy 必须是 http:// 或 https:// 开头的地址。")
    return value

def validate_image(value: Any, max_bytes: int = MAX_IMAGE_BYTES) -> str:
    if not isinstance(value, str):
        raise NovelAIError("图片数据格式错误。")
    if value.startswith("data:"):
        prefix, separator, value = value.partition(",")
        if not separator or prefix not in ("data:image/png;base64", "data:image/jpeg;base64", "data:image/webp;base64"):
            raise NovelAIError("图片数据前缀无效。")
    if len(value) > ((max_bytes + 2) // 3) * 4:
        raise NovelAIError("图片超过大小限制。")
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise NovelAIError("图片 Base64 无效。") from None
    if not data or len(data) > max_bytes:
        raise NovelAIError("图片为空或超过大小限制。")
    if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff") or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")):
        raise NovelAIError("图片格式仅支持 PNG、JPEG、WebP。")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > 16_777_216:
                raise NovelAIError("图片像素数量超过限制。")
            image.verify()
    except NovelAIError:
        raise
    except Exception:
        raise NovelAIError("图片损坏，无法发送。") from None
    return base64.b64encode(data).decode("ascii")

def build_payload(req: DrawRequest, scale: float, sampler: str, reference: Optional[str] = None, reference_strength: float = 0.75, reference_fidelity: float = 0.6, reference_type: str = "character", extra_references: Optional[List[Dict[str, Any]]] = None, char_captions: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    def caption(text: str, characters: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        return {"caption": {"base_caption": text, "char_captions": characters or []}, "use_coords": False, "use_order": True}
    # 4.5 的 char_captions 用来单独锚定角色；角色写在独立 caption 里比混在 base 里还原度高。
    # 多角色必须一人一条：挤进同一条，模型会把两个人的发色瞳色混成一个。
    characters = [{"char_caption": text, "centers": [{"x": 0.5, "y": 0.5}]}
                  for text in (char_captions or []) if text]
    payload = {"action": "generate", "input": req.prompt, "model": req.model, "parameters": {"width": req.width, "height": req.height, "steps": req.steps, "scale": scale, "seed": req.seed, "n_samples": req.count, "sampler": sampler, "negative_prompt": req.negative, "qualityToggle": True, "v4_prompt": caption(req.prompt, characters), "v4_negative_prompt": caption(req.negative)}}
    references = ([{"image": reference, "type": reference_type, "strength": reference_strength, "fidelity": reference_fidelity}] if reference else []) + (extra_references or [])
    if references:
        if req.model != "nai-diffusion-4-5-full" or len(references) > 2:
            raise NovelAIError("参考图仅支持 4.5 Full，最多两张独立参考图。")
        for item in references:
            if item["type"] not in ("character", "style", "character&style"):
                raise NovelAIError("参考类型须为 character、style 或 character&style。")
            if any(not isinstance(item[key], (int, float)) or isinstance(item[key], bool) or not 0 <= item[key] <= 1 for key in ("strength", "fidelity")):
                raise NovelAIError("参考图强度和保真度须为 0～1。")
        payload["parameters"].update({"director_reference_images": [item["image"] for item in references], "director_reference_descriptions": [caption(item["type"]) for item in references], "director_reference_information_extracted": [1.0] * len(references), "director_reference_strength_values": [item["strength"] for item in references], "director_reference_secondary_strength_values": [item["fidelity"] for item in references]})
    return payload

class NovelAIClient:
    def __init__(self, base_url: str = OFFICIAL_URL, timeout: float = 180, transport: Optional[httpx.AsyncBaseTransport] = None, proxy: Optional[str] = None, retry_attempts: int = 3):
        if base_url.rstrip("/") != OFFICIAL_URL:
            raise NovelAIError("配置错误：仅允许官方 HTTPS 地址 https://image.novelai.net。")
        if not 5 <= timeout <= 180:
            raise NovelAIError("配置错误：请求超时必须为 5～180 秒。")
        self.timeout = timeout
        self.retry_attempts = max(1, min(int(retry_attempts), 8))
        if transport is not None:
            # 显式传入的 transport 优先，供测试与上层注入。
            self.transport = transport
        elif explicit_proxy := _normalize_proxy(proxy):
            self.transport = httpx.AsyncHTTPTransport(proxy=explicit_proxy)
        else:
            self.transport = None

    async def generate(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        token = resolve_api_token()
        if not token:
            raise NovelAIError("未配置 NOVELAI_API_TOKEN，请管理员设置环境变量并重启 MaiBot。")
        # 代理到 NovelAI 的线路实测约三分之一成功率（deepseek 同一代理是 6/6），
        # 所以连接层错误值得重试。注意 RemoteProtocolError / 读超时时请求可能已经送达，
        # 重试有极小概率多出一张图、多扣一次 Anlas；把这个次数调到 1 即可关闭重试。
        data: bytearray = bytearray()
        attempts = max(1, self.retry_attempts)
        for attempt in range(1, attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout), transport=self.transport, follow_redirects=False, trust_env=False) as client:
                    async with client.stream("POST", OFFICIAL_URL + "/ai/generate-image", json=payload, headers={"Authorization": "Bearer " + token, "Accept": "application/json", "Content-Type": "application/json"}) as response:
                        # 先把响应体读完再判断状态码：带着未读数据退出 stream 时 httpx 会抛连接
                        # 错误，把真实的 4xx/5xx 掩盖成「网络连接失败」。
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > MAX_RESPONSE_BYTES:
                                raise NovelAIError("响应超过大小限制。")
                        if not 200 <= response.status_code < 300:
                            messages = {400: "参数或模型不兼容。", 401: "令牌无效、过期或已被替换。", 402: "请检查 NovelAI 订阅或 Anlas 余额。", 403: "账户或该功能没有权限。", 422: "参数或模型不兼容。", 429: "请求频率过高，请稍后再试。"}
                            message = messages.get(response.status_code, "NovelAI 服务异常，请稍后再试。")
                            correlation = response.headers.get("x-correlation-id", "")
                            if response.status_code >= 500 and re.fullmatch(r"[A-Za-z0-9]{6}", correlation):
                                message += " 报告编号：" + correlation
                            raise NovelAIError(message)
                break
            except NovelAIError:
                raise
            except httpx.TimeoutException as exc:
                # 读超时可能只是响应慢，也可能请求已经送达；仍然重试，但记下类型。
                if attempt >= attempts:
                    logger.error("NovelAI 请求超时，已尝试 %d 次", attempt)
                    raise NovelAIError("NovelAI 请求超时，请稍后再试。") from None
                logger.warning("NovelAI 第 %d/%d 次超时（%s），重试", attempt, attempts, type(exc).__name__)
            except httpx.HTTPError as exc:
                if attempt >= attempts:
                    # 只记录类型；异常文本可能带 URL 或代理细节。
                    logger.error("NovelAI 请求失败，已尝试 %d 次，末次异常类型：%s", attempt, type(exc).__name__)
                    raise NovelAIError("NovelAI 网络连接失败，请检查网络、DNS 或 TLS。") from None
                logger.warning("NovelAI 第 %d/%d 次连接失败（%s），重试", attempt, attempts, type(exc).__name__)
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS * attempt)
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError):
            raise NovelAIError("NovelAI 返回了无效 JSON。") from None
        images = result.get("images") if isinstance(result, dict) else None
        count = payload["parameters"]["n_samples"]
        if not isinstance(images, list) or len(images) != count:
            raise NovelAIError("NovelAI 返回图片数量不匹配或为空。")
        validated = []
        for image in images:
            if not isinstance(image, dict) or isinstance(image.get("seed"), bool) or not isinstance(image.get("seed"), int) or not 0 <= image["seed"] < 2**32:
                raise NovelAIError("NovelAI 返回图片元数据无效。")
            validated.append({"image": validate_image(image.get("image")), "seed": image["seed"]})
        return validated
