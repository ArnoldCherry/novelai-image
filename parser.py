from dataclasses import dataclass
from typing import Dict, Optional, Tuple
import math
import secrets
import shlex

from .exceptions import NovelAIError

SIZE_PRESETS = {"方图": (1024, 1024), "竖图": (832, 1216), "横图": (1216, 832)}
# V5 的 Small 档：Opus 的无限生成只覆盖这一档画布，超出就按尺寸×步数计 Anlas。
SMALL_SIZE_PRESETS = {"方图": (640, 640), "竖图": (512, 768), "横图": (768, 512)}
MODEL_ALIASES = {"普通": "nai-diffusion-4-5-full", "高清": "nai-diffusion-5-full"}
ALIASES = {"比例": "ratio", "种子": "seed", "步数": "steps", "负面": "negative", "数量": "count", "模型": "model"}
ALIASES.update({"引导": "scale", "采样器": "sampler", "角色强度": "character-strength", "角色保真度": "character-fidelity", "画风强度": "style-strength", "画风保真度": "style-fidelity", "参考模式": "reference-mode", "自动参数": "auto", "画风权重": "style-weight"})
SAMPLERS = ("k_euler", "k_euler_ancestral", "k_dpmpp_2s_ancestral", "k_dpmpp_2m", "k_dpmpp_sde", "ddim_v3")
ALIASES.update({v: v for v in list(ALIASES.values())})

@dataclass(frozen=True)
class DrawRequest:
    prompt: str
    negative: str
    model: str
    width: int
    height: int
    steps: int
    seed: int
    count: int
    scale: Optional[float] = None
    sampler: Optional[str] = None
    character_strength: Optional[float] = None
    character_fidelity: Optional[float] = None
    style_strength: Optional[float] = None
    style_fidelity: Optional[float] = None
    reference_mode: Optional[str] = None
    auto: Optional[bool] = None
    style_weight: Optional[float] = None
    explicit: tuple[str, ...] = ()

def parse_args(text: str, *, default_model: str = MODEL_ALIASES["普通"], default_ratio: str = "竖图", default_steps: int = 28, default_negative: str = "low quality, blurry", max_images: int = 2, max_prompt_length: int = 3000, model_aliases: Optional[Dict[str, str]] = None, size_presets: Optional[Dict[str, Tuple[int, int]]] = None) -> DrawRequest:
    """选项从 -- 开始，负面文本保留空格直到下一个选项；支持引号。"""
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        raise NovelAIError("参数引号不匹配。") from None
    prompt, options, current = [], {}, None
    for token in tokens:
        if token.startswith("--"):
            key = ALIASES.get(token[2:])
            if key is None:
                raise NovelAIError("存在未知参数，请查看 /画图帮助。")
            if key in options:
                raise NovelAIError("同一参数不能重复。")
            options[key], current = [], key
        elif current is None:
            prompt.append(token)
        else:
            options[current].append(token)
    values = {k: " ".join(v) for k, v in options.items()}
    if any(not v for v in values.values()):
        raise NovelAIError("参数缺少值。")
    text_prompt = " ".join(prompt).strip()
    if not text_prompt or len(text_prompt) > max_prompt_length:
        raise NovelAIError(f"提示词不能为空且不能超过 {max_prompt_length} 个字符。")
    sizes = size_presets if size_presets is not None else SIZE_PRESETS
    ratio = values.get("ratio", default_ratio)
    if ratio not in sizes:
        raise NovelAIError("比例只能是方图、竖图或横图。")
    def number(key: str, default: int, low: int, high: int) -> int:
        raw = values.get(key, str(default))
        if not raw.isascii() or not raw.isdecimal() or not low <= int(raw) <= high:
            raise NovelAIError(f"{key} 必须为 {low}～{high} 的整数。")
        return int(raw)
    models = model_aliases if model_aliases is not None else MODEL_ALIASES
    model = default_model
    if "model" in values:
        if values["model"] not in models:
            raise NovelAIError("模型必须是配置中的模型别名。")
        model = models[values["model"]]
    negative = values.get("negative", default_negative)
    if len(negative) > max_prompt_length:
        raise NovelAIError("负面提示词过长。")
    width, height = sizes[ratio]
    def decimal(key: str, high: float) -> Optional[float]:
        if key not in values:
            return None
        try:
            value = float(values[key])
        except ValueError:
            raise NovelAIError(f"{key} 必须为 0～{high:g} 的数字。") from None
        if not math.isfinite(value) or not 0 <= value <= high:
            raise NovelAIError(f"{key} 必须为 0～{high:g} 的数字。")
        return value
    sampler = values.get("sampler")
    if sampler is not None and sampler not in SAMPLERS:
        raise NovelAIError("采样器须为：" + "、".join(SAMPLERS))
    modes = {"自动": "auto", "角色": "character", "角色画风": "character&style", "关闭": "off", "auto": "auto", "character": "character", "character&style": "character&style", "off": "off"}
    mode = values.get("reference-mode")
    if mode is not None and mode not in modes:
        raise NovelAIError("参考模式只能是自动、角色、角色画风、关闭。")
    switches = {"on": True, "off": False, "开启": True, "关闭": False, "true": True, "false": False}
    auto = values.get("auto")
    if auto is not None and auto not in switches:
        raise NovelAIError("自动参数只能是 on 或 off（开启或关闭）。")
    return DrawRequest(text_prompt, negative, model, width, height, number("steps", default_steps, 1, 50), number("seed", secrets.randbelow(2**32), 0, 2**32-1), number("count", 1, 1, min(max_images, 2)), scale=decimal("scale", 10), sampler=sampler, character_strength=decimal("character-strength", 1), character_fidelity=decimal("character-fidelity", 1), style_strength=decimal("style-strength", 1), style_fidelity=decimal("style-fidelity", 1), reference_mode=modes.get(mode), auto=switches.get(auto), style_weight=decimal("style-weight", 2), explicit=tuple(values))
