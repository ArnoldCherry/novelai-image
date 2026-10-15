"""通过本机 Ollama 将中文绘图描述翻译为英文提示词。"""

from typing import Any, Dict, List, Optional
import asyncio
import json
import logging
import re
import shutil
import subprocess

import httpx

from .exceptions import NovelAIError
from .glossary import apply_glossary, merge_tags

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "huihui_ai/qwen3-abliterated:4b"
DEFAULT_FALLBACK_MODEL = "huihui_ai/qwen3-abliterated:1.7b"
DEFAULT_NUM_GPU = -1
DEFAULT_REPEAT_PENALTY = 1.3
DEFAULT_KEEP_ALIVE = 0
MAX_PROMPT_LENGTH = 3000
_CHINESE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_THINKING = re.compile(r"<\s*/?\s*think\s*>|^\s*(?:analysis|thinking|reasoning)\s*:", re.I | re.M)
_REFUSAL = re.compile(r"\b(?:i (?:cannot|can't|won't)|i am unable to|as an ai|sorry,? i (?:cannot|can't))\b", re.I)
_SYSTEM_PROMPT = (
    "You translate Chinese image descriptions into English comma-separated danbooru tags.\n"
    "Output ONLY a JSON object with exactly the same keys as the input, and nothing else.\n"
    "Translate every visual detail the input states, including any part already written in English.\n"
    "Never add people, objects, actions, or style that the input does not state.\n"
    "Preserve actions in progress: tying hair means hands actively tying the hair, not only a finished ponytail.\n"
    "Preserve exact clothing types and already translated English tags; never replace a one-piece swimsuit with a bikini.\n"
    'Example input: {"positive": "\u4e00\u4e2a\u7ea2\u82f9\u679c"}\n'
    'Example output: {"positive": "red apple"}\n'
    'Example input: {"negative": "\u6a21\u7cca"}\n'
    'Example output: {"negative": "blurry"}\n'
    "Translate the input the same way."
)


def contains_chinese(text: str) -> bool:
    return bool(_CHINESE.search(text))


def validate_english_prompt(text: Any, *, negative: bool = False) -> str:
    if not isinstance(text, str):
        raise NovelAIError("提示词翻译返回格式错误，请稍后重试。")
    result = " ".join(part.strip() for part in text.splitlines()).strip()
    if not result:
        raise NovelAIError("提示词翻译没有返回提示词，请修改描述后重试。")
    if len(result) > MAX_PROMPT_LENGTH:
        raise NovelAIError("提示词翻译结果过长，请缩短描述后重试。")
    if contains_chinese(result):
        raise NovelAIError("提示词翻译结果仍包含中文，请改用英文或简化描述。")
    if _THINKING.search(result):
        raise NovelAIError("提示词翻译返回了思考内容，请稍后重试。")
    if _REFUSAL.search(result):
        raise NovelAIError("提示词翻译未能处理这段描述，请修改后重试。")
    tags = [tag.strip() for tag in result.split(",") if tag.strip()]
    # 句末标点说明模型写成了句子；冒号只在后面跟空白时才算（写成「xx: yy」的说明）。
    # 标签内部的冒号是合法的：Danbooru 里 re:zero、sin:_nanatsu_no_taizai 这类名字很多，
    # 一律拒绝会让凡涉及这些作品的中文提示词整条失败。
    if not tags or any(re.search(r"[.!?;]|:\s|(?:\*\*|__|```)|^\s*[-*>#]", tag) for tag in tags):
        raise NovelAIError("提示词翻译未按逗号分隔视觉标签格式返回，请修改描述后重试。")
    if tags and re.match(r"^(?:positive|negative|prompt|translation|english)\s*:", tags[0], re.I):
        raise NovelAIError("提示词翻译包含了说明标签，请修改描述后重试。")
    if negative and any(re.match(r"^(?:no|not|don't|do not|without|avoid)\b", tag, re.I) for tag in tags):
        raise NovelAIError("负面提示词必须使用排除对象标签，请稍后重试。")
    return ", ".join(tags)


class OllamaTranslator:
    """连接固定的本机 Ollama 服务，不使用代理或重定向。"""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, model: str = DEFAULT_MODEL, timeout: float = 60, transport: Optional[httpx.AsyncBaseTransport] = None, num_gpu: int = DEFAULT_NUM_GPU, repeat_penalty: float = DEFAULT_REPEAT_PENALTY, fallback_model: str = DEFAULT_FALLBACK_MODEL, keep_alive: int = DEFAULT_KEEP_ALIVE):
        if base_url.rstrip("/") != DEFAULT_BASE_URL:
            raise NovelAIError("配置错误：翻译服务仅允许本机 Ollama http://127.0.0.1:11434。")
        if not model or len(model) > 128:
            raise NovelAIError("配置错误：Ollama 翻译模型名称无效。")
        if len(fallback_model) > 128:
            raise NovelAIError("配置错误：Ollama 备用模型名称过长。")
        if not 1 <= timeout <= 60:
            raise NovelAIError("配置错误：翻译超时必须为 1～60 秒。")
        if not -1 <= num_gpu <= 999:
            raise NovelAIError("配置错误：num_gpu 必须为 -1（交由 Ollama 自动决定）或 0～999。")
        if not 1.0 <= repeat_penalty <= 2.0:
            raise NovelAIError("配置错误：repeat_penalty 必须为 1.0～2.0。")
        if keep_alive < -1:
            raise NovelAIError("配置错误：keep_alive 必须为 -1（常驻）或 >= 0 的秒数（0 表示用完立即卸载）。")
        self.base_url, self.model = DEFAULT_BASE_URL, model
        self.timeout, self.transport = timeout, transport
        self.num_gpu, self.repeat_penalty = num_gpu, repeat_penalty
        self.keep_alive = keep_alive
        # 备用模型与主模型相同（或为空）时不启用降级。
        self.fallback_model = "" if fallback_model == model else fallback_model

    async def _post(self, path: str, payload: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout or self.timeout), transport=self.transport, follow_redirects=False, trust_env=False) as client:
                response = await client.post(self.base_url + path, json=payload)
                if not 200 <= response.status_code < 300:
                    raise NovelAIError("本机 Ollama 请求失败，请检查服务和模型状态。")
                result = response.json()
        except NovelAIError:
            raise
        except httpx.TimeoutException:
            raise NovelAIError("本地翻译超时，请稍后重试。") from None
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise NovelAIError("无法连接本机 Ollama 翻译服务，请管理员检查服务状态。") from None
        if not isinstance(result, dict):
            raise NovelAIError("本机 Ollama 返回格式错误，请检查服务状态。")
        return result

    async def _ensure_daemon(self) -> None:
        try:
            if await self._tags_available():
                return
        except NovelAIError:
            pass
        executable = shutil.which("ollama.exe") or shutil.which("ollama")
        if not executable:
            raise NovelAIError("找不到 Ollama 程序，无法启动本地翻译服务。")
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen([executable, "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, creationflags=creationflags)
        except OSError:
            # 另一个 MaiBot 进程可能刚好启动了 Ollama。
            pass
        for _ in range(20):
            await asyncio.sleep(0.5)
            try:
                if await self._tags_available(timeout=1):
                    return
            except NovelAIError:
                continue
        raise NovelAIError("Ollama 服务未能在 10 秒内启动，请管理员检查本地服务。")

    async def _tags_available(self, timeout: float = 2) -> bool:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), transport=self.transport, follow_redirects=False, trust_env=False) as client:
                response = await client.get(self.base_url + "/api/tags")
            if not 200 <= response.status_code < 300:
                return False
            result = response.json()
            return isinstance(result, dict) and isinstance(result.get("models"), list)
        except (httpx.HTTPError, ValueError, UnicodeError):
            return False

    def _generation_options(self, *, num_predict: int = 1) -> Dict[str, Any]:
        """构造 Ollama 推理参数。

        ``repeat_penalty`` 是必需的：默认 1.1 对这批小模型不足，在 temperature
        0.1 下会陷入重复退化，把输出填满到 num_predict 上限。``num_gpu`` 为负时
        不发送该字段，交由 Ollama 按显存自动决定层数。
        """
        options: Dict[str, Any] = {"num_ctx": 2048, "num_predict": num_predict, "temperature": 0.1, "repeat_penalty": self.repeat_penalty}
        if self.num_gpu >= 0:
            options["num_gpu"] = self.num_gpu
        return options

    async def preload(self) -> None:
        """确认 Ollama 在线；keep_alive 为 0 时不做常驻加载。"""
        try:
            async with asyncio.timeout(self.timeout):
                await self._ensure_daemon()
                if self.keep_alive == 0:
                    return
                await self._post("/api/generate", {"model": self.model, "prompt": "", "stream": False, "keep_alive": self.keep_alive, "options": self._generation_options()})
        except TimeoutError:
            raise NovelAIError("翻译模型加载超时，请稍后重试。") from None

    async def unload(self) -> None:
        try:
            async with asyncio.timeout(self.timeout):
                await self._post("/api/generate", {"model": self.model, "prompt": "", "stream": False, "keep_alive": 0, "options": self._generation_options()})
        except TimeoutError:
            raise NovelAIError("翻译模型卸载超时，请稍后检查本机服务。") from None

    async def translate_many(self, prompts: Dict[str, str]) -> Dict[str, str]:
        """翻译中文描述。

        先套词表：命中的术语直接采用固定英文标签，避免小模型误译或编造；
        剩余仍含中文的文本才交给本机模型，最后按原文顺序合并去重。
        """
        relevant = {key: text for key, text in prompts.items() if contains_chinese(text)}
        if not relevant:
            return {}
        glossary_hits: Dict[str, List[str]] = {}
        to_model: Dict[str, str] = {}
        for key, text in relevant.items():
            substituted, hits = apply_glossary(text)
            if hits:
                glossary_hits[key] = hits
            if contains_chinese(substituted):
                to_model[key] = substituted
        model_tags = await self._translate_with_fallback(to_model) if to_model else {}
        merged: Dict[str, str] = {}
        for key in relevant:
            tags = merge_tags(glossary_hits.get(key, []), model_tags.get(key, []))
            if not tags:
                raise NovelAIError("本地翻译没有返回提示词，请修改描述后重试。")
            merged[key] = validate_english_prompt(", ".join(tags), negative=key == "negative")
        return merged

    async def _translate_with_fallback(self, pending: Dict[str, str]) -> Dict[str, List[str]]:
        """先用主模型翻译；失败且配置了备用模型时改用备用模型重试。

        备用模型只在这次调用中被拉起（``keep_alive=0``），返回后立即卸载，
        因此不会长期占用显存。
        """
        try:
            return await self._translate_via_model(pending, self.model, self.keep_alive)
        except NovelAIError as primary_error:
            if not self.fallback_model:
                raise
            logger.warning("主翻译模型 %s 失败，改用备用模型 %s：%s", self.model, self.fallback_model, primary_error)
            return await self._translate_via_model(pending, self.fallback_model, 0)

    async def _translate_via_model(self, pending: Dict[str, str], model: str, keep_alive: int) -> Dict[str, List[str]]:
        """把仍需翻译的文本交给指定模型，返回按 key 分组的标签列表。"""
        try:
            async with asyncio.timeout(self.timeout):
                await self._ensure_daemon()
                result = await self._post("/api/chat", {
                    "model": model,
                    "format": "json",
                    "stream": False,
                    "think": False,
                    "keep_alive": keep_alive,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(pending, ensure_ascii=False)},
                    ],
                    "options": self._generation_options(num_predict=512),
                })
                message = result.get("message")
                if result.get("done_reason") == "length":
                    raise NovelAIError("本地翻译输出达到长度上限，请缩短描述后重试。")
                if not isinstance(message, dict) or message.get("role") != "assistant" or message.get("thinking"):
                    raise NovelAIError("本地翻译返回了无效内容，请稍后重试。")
                content = message.get("content")
                if not isinstance(content, str):
                    raise NovelAIError("本地翻译返回格式错误，请稍后重试。")
                try:
                    translated = json.loads(content)
                except (ValueError, TypeError):
                    raise NovelAIError("本地翻译未返回有效结果，请修改描述后重试。") from None
                if not isinstance(translated, dict) or set(translated) != set(pending):
                    raise NovelAIError("本地翻译结果与输入描述不匹配，请稍后重试。")
                return {key: validate_english_prompt(value, negative=key == "negative").split(", ") for key, value in translated.items()}
        except TimeoutError:
            raise NovelAIError("本地翻译超时，请稍后重试。") from None

    async def translate(self, text: str) -> str:
        """翻译单条描述；英文内容直接返回。"""
        if not contains_chinese(text):
            return text
        return (await self.translate_many({"prompt": text}))["prompt"]
