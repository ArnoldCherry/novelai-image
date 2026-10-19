from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import asyncio
import base64
import json
import logging
import math
import re
import time
import uuid

from PIL import Image, ImageDraw, ImageFont

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .adaptive import (
    DEFAULT_NEGATIVE_STYLE,
    DEFAULT_STYLE,
    HD_DEFAULT_DROP_NEGATIVES,
    HD_DEFAULT_NEGATIVE_STYLE,
    HD_DEFAULT_ONLY_NEGATIVE_STYLE,
    HD_DEFAULT_STYLE,
    STYLE_REQUEST,
    drop_haze,
    explicit_global_tags,
    explicit_negatives,
    explicit_tags,
    scene_keeps_haze,
    scene_negatives,
    style_tags,
)
from .client import NovelAIClient, build_payload, resolve_api_token
from .characters import identity_tags, load_characters, precise_reference, resolve_official_split, strip_names
from .exceptions import NovelAIError
from .default_style_policy import (
    multichar_realism_negative,
    multichar_style_core,
    normalize_default_multichar_style,
    normalize_default_style,
)
from .glossary import merge_tags
from .nanogpt import NANOGPT_BASE_URL, NANOGPT_MODEL, NanoGPTTranslator
from .multichar import caption_payloads, needs_character_split, pipe_sections
from .presets import (
    PRESETS,
    StylePreset,
    apply_preset_negatives,
    generation_tuning,
    match_artist,
    match_preset,
    preview_entries,
    preview_groups,
    preset_render_tags,
    strip_preset_keywords,
)
from .supplement import supplement_tags
from .tag_index import DanbooruTagIndex
from .parser import MODEL_ALIASES, SAMPLERS, SIZE_PRESETS, SMALL_SIZE_PRESETS, parse_args
from .references import prepare_uploaded_reference
from .translator import DEFAULT_BASE_URL, DEFAULT_FALLBACK_MODEL, DEFAULT_KEEP_ALIVE, DEFAULT_MODEL, DEFAULT_NUM_GPU, DEFAULT_REPEAT_PENALTY, OllamaTranslator, contains_chinese
from .workflow import WORKFLOW_HELP, WorkflowMixin, WorkflowPayload

logger = logging.getLogger(__name__)


_STYLE_WEIGHT_BLOCK = re.compile(r"(?P<weight>[+-]?(?:\d+(?:\.\d*)?|\.\d+))::(?P<tags>(?:(?!::)[\s\S])+?)::")
_EXPLICIT_SCENE_LIGHTING = re.compile(
    r"夜晚|夜景|夜间|黑夜|深夜|夜色|夜幕|夜里|黄昏|夕阳|日落|落日|日出|逆光|背光|侧光|顶光|正面光|顺光|霓虹|灯光|光照|光源|月光|阳光|烛光|冷光|暖光|柔光|硬光|"
    r"\b(?:night(?:time)?|sunset|sunrise|dusk|dawn|twilight|neon|moonlight|sunlight|daylight|candlelight|"
    r"backlight(?:ing)?|frontlight(?:ing)?|(?:rim|front|back|side|top)[- ](?:lit|light)|(?:\w+[- ])?lighting)\b", re.I)
_INHERITED_LIGHTING_TAGS = {
    "lighting", "soft lighting", "hard lighting", "warm lighting", "cool lighting", "cold lighting",
    "neutral lighting", "natural lighting", "diffused lighting", "diffuse lighting", "ambient lighting",
    "front lighting", "frontal lighting", "frontlighting", "front light", "front lit", "front-lit",
    "backlighting", "back lighting", "backlight", "backlit", "back-lit", "rim lighting", "rim light",
    "side lighting", "top lighting", "dramatic lighting", "atmospheric lighting", "even lighting",
    "ambient light", "warm ambient light", "cool ambient light", "gentle light", "high key", "low key",
}


def scene_priority_artist_tags(tags: List[str], request_text: str) -> List[str]:
    """明确场景光源优先于画师样图的默认照明，保留画法及独立权重块。"""
    if not _EXPLICIT_SCENE_LIGHTING.search(request_text):
        return tags

    def without_lighting(text: str) -> str:
        return ", ".join(part.strip() for part in text.split(",")
                         if part.strip() and part.strip().lower() not in _INHERITED_LIGHTING_TAGS)

    result: List[str] = []
    for tag in tags:
        end = 0
        for block in _STYLE_WEIGHT_BLOCK.finditer(tag):
            plain = without_lighting(tag[end:block.start()])
            if plain:
                result.append(plain)
            kept = without_lighting(block["tags"])
            if kept:
                result.append(f"{block['weight']}::{kept}::")
            end = block.end()
        plain = without_lighting(tag[end:])
        if plain:
            result.append(plain)
    return result


def weighted_style_tags(tags: List[str], weight: float) -> List[str]:
    """展开文字画风权重；已有权重相乘，绝不生成 NovelAI 不支持的嵌套块。"""
    plain: List[str] = []
    weighted: List[str] = []
    for tag in tags:
        end = 0
        for block in _STYLE_WEIGHT_BLOCK.finditer(tag):
            plain.extend(part.strip() for part in tag[end:block.start()].split(",") if part.strip())
            # 默认 1 倍原样保留已确认配方，手动权重则同时缩放画师与画法。
            weighted.append(block.group() if weight == 1 else
                            f"{float(block['weight']) * weight:g}::{block['tags']}::")
            end = block.end()
        plain.extend(part.strip() for part in tag[end:].split(",") if part.strip())
    if any("::" in tag for tag in plain):
        raise NovelAIError("文字画风权重格式不正确；请使用独立的 数字::标签::，不要嵌套权重。")
    head = [f"{weight:g}::" + ", ".join(plain) + " ::"] if plain else []
    return head + weighted


def preset_reference_image(preset: StylePreset, max_bytes: int) -> str:
    """把内置风格参考图读成官方 Precise Reference 画布；文件缺失时返回空串。"""
    path = preset.reference_path
    if not path.is_file():
        return ""
    return prepare_uploaded_reference(base64.b64encode(path.read_bytes()).decode("ascii"), max_bytes)


PREVIEW_WIDTH = 400
PREVIEW_QUALITY = 76


def size_table_for(req) -> Dict[str, Tuple[int, int]]:
    """判断这条请求用的是哪一档画布表。

    Small 与 Normal 的比例键相同、像素值不同，自动比例改画布时必须沿用原来那档，
    否则 /画小图 会被改回 832×1216，白跑一次计费。
    """
    for table in (SMALL_SIZE_PRESETS, SIZE_PRESETS):
        if (req.width, req.height) in table.values():
            return table
    return SIZE_PRESETS


def style_preview(entries: List[Tuple[str, Path]]) -> List[Tuple[str, str]]:
    """画风列表用的缩略图。十几张原图一起塞进一次转发会超出插件 IPC 载荷上限，
    所以先压成小 JPEG 再 base64。"""
    previews: List[Tuple[str, str]] = []
    for label, path in entries:
        if not path.is_file():
            continue
        with Image.open(path) as source:
            image = source.convert("RGB")
            if image.width > PREVIEW_WIDTH:
                image = image.resize((PREVIEW_WIDTH, round(image.height * PREVIEW_WIDTH / image.width)), Image.Resampling.LANCZOS)
            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=PREVIEW_QUALITY)
        previews.append((label, base64.b64encode(buffer.getvalue()).decode("ascii")))
    return previews


# NapCat 上传合并转发资源失败（"上传转发消息资源失败：N/M"）多半是图太大：
# 实测 base64 约 2.7 MiB 能过，6.5 MiB 必挂。转发前按这个上限压一遍。
FORWARD_MAX_BASE64 = int(2.5 * 1024 * 1024)
# 不能低于 60 秒：早先给得太短，转发还没传完就被判超时（测试里有这道守卫）。
# 从 120 秒收到 60 秒，最坏情况的空等减半，仍留足上传时间。
FORWARD_TIMEOUT_MS = 60_000


def shrink_for_forward(data: str, *, compact: bool = False) -> str:
    """把图片规范成 JPEG 并压到合并转发能上传的体积；失败时保留原图供恢复。

    逐张直发不受这个限制（NapCat 单图消息处理正常），所以只在转发前压。
    """
    try:
        with Image.open(BytesIO(base64.b64decode(data))) as source:
            image = source.convert("RGB")
        profile = ((832, 75), (768, 68), (640, 60)) if compact else (
            (1247, 88), (1024, 85), (832, 80), (768, 72), (640, 65))
        for width, quality in profile:
            candidate = image
            if candidate.width > width:
                candidate = candidate.resize(
                    (width, round(candidate.height * width / candidate.width)), Image.Resampling.LANCZOS)
            buffer = BytesIO()
            candidate.save(buffer, format="JPEG", quality=quality, optimize=True)
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            if len(encoded) <= FORWARD_MAX_BASE64:
                return encoded
    except Exception as exc:
        logger.warning("合并转发前压缩失败，改用原图：%s", _describe_error(exc))
    return data


def _wrap_line(line: str, font, limit: int, ruler: ImageDraw.ImageDraw) -> List[str]:
    """按像素宽度折行；能落在空格上就落空格，只有超长的英文长串才硬切。"""
    chunks: List[str] = []
    current = ""
    last_space = -1
    for char in line:
        if current and ruler.textlength(current + char, font=font) > limit:
            if last_space >= 0:
                chunks.append(current[:last_space].rstrip())
                current = current[last_space + 1:] + char
            else:
                chunks.append(current.rstrip())
                current = char
            last_space = -1
        else:
            current += char
            if char == " ":
                last_space = len(current) - 1
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


SHEET_COLUMNS = 6
SHEET_CELL = (700, 1022)
SHEET_QUALITY = 88
SHEET_LABEL_SIZE = 75
# 顶部给模型标注留的高度，以及那个标注的字号——两张拼接图的区别全靠它。
SHEET_HEADER_HEIGHT = 132
SHEET_HEADER_SIZE = 84
# 单张对照表的 base64 预算。实测出图那种约 2.7 MiB 的图能正常发出，而 6.5 MiB 的
# 对照表 NapCat 会报「上传转发消息资源失败」；超预算就切成几张，别让整条消息发不出去。
SHEET_MAX_BASE64 = 3 * 1024 * 1024
# 对照表单格尺寸按 832×1216 的比例缩放，避免参考图被拉变形。
SHEET_ASPECT = 1216 / 832


def _describe_error(exc: BaseException) -> str:
    """展开异常正文。

    RPCError 的正文里带着错误码（超时 / 未连接 / 帧过大），只记 type(exc).__name__
    等于什么都没记——线上排查过不去就是卡在这里。
    """
    detail = " ".join(str(part) for part in getattr(exc, "args", ()) if part)
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def model_tag(model: str) -> str:
    """模型 ID 的短标，用于图里和节点上的标注。"""
    if "4-5" in model:
        return "v4.5"
    if model.endswith("-5-full"):
        return "v5"
    return model


def style_table(media, artists) -> str:
    """合并转发里第一条的纯文字总览：先把名字列出来，再往下才是图。"""
    lines = [f"可用画风 {len(media) + len(artists)} 档", "", "【媒介】" + " / ".join(label for label, _ in media)]
    if artists:
        lines += ["", "【画师】" + " / ".join(label.removeprefix("画师·") for label, _ in artists)]
    lines += ["", "点名方式：在提示词里写上画风名，例如「少女坐在窗边，锐利线稿」或「……，wlop」。",
              "画师名支持模糊写法：分隔线、大小写、连写、去掉 (xxx) 后缀都认。"]
    return "\n".join(lines)


def style_sheet_image(entries, model: str = "", columns: int = SHEET_COLUMNS,
                      cell_width: int = SHEET_CELL[0], quality: int = SHEET_QUALITY) -> str:
    """把全部画风拼成一张对照表，带序号与名字；给了 model 就在顶部标出是哪一档生成的。

    格子尺寸可调：默认比旧版大一档，参考图缩得少、细节留得住。
    """
    available = [(label, path) for label, path in entries if path.is_file()]
    if not available:
        return ""
    cell = (cell_width, round(cell_width * SHEET_ASPECT))
    columns = max(1, min(columns, len(available)))
    rows = (len(available) + columns - 1) // columns
    header = SHEET_HEADER_HEIGHT if model else 0
    sheet = Image.new("RGB", (cell[0] * columns, header + cell[1] * rows), "#0b0b0b")
    draw = ImageDraw.Draw(sheet)
    try:
        # 名字要看得清：字号跟格子宽度走，并在名字下面垫一条深色底，压住参考图的亮部。
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", SHEET_LABEL_SIZE)
        head_font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", SHEET_HEADER_SIZE)
        sub_font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 32)
    except OSError:
        font = head_font = sub_font = ImageFont.load_default()
    if model:
        # 顶部单独留一段，模型标注要大而显眼——两张拼接图的区别全在这里。
        tag = model_tag(model)
        draw.text((28, 8), tag, font=head_font, fill="#ffd966")
        tag_width = draw.textlength(tag, font=head_font)
        draw.text((28 + tag_width + 20, 62), model, font=sub_font, fill="#8fa4bd")
        draw.line([(0, header - 4), (sheet.width, header - 4)], fill="#3a4354", width=3)
    text_limit = cell[0] - 20
    line_step = SHEET_LABEL_SIZE + 8

    def label_lines(text: str) -> List[str]:
        """按像素宽度折行，放不下就换行、不做省略——省略出来的名字根本认不出是哪一档。

        折行优先落在空格上，免得把 mika pikazo 劈成 pikaz / o。
        """
        return _wrap_line(text, font, text_limit, draw) or [""]

    # 先量一遍最大行数，让所有格子高度一致，图不会被挤成参差不齐。
    strip_lines = max(len(label_lines(f"{index + 1}. {label}")) for index, (label, _) in enumerate(available))
    label_height = 10 + line_step * strip_lines
    for index, (label, path) in enumerate(available):
        with Image.open(path) as source:
            image = source.convert("RGB")
            image.thumbnail((cell[0] - 12, cell[1] - label_height - 10), Image.Resampling.LANCZOS)
        x = (index % columns) * cell[0]
        y = header + (index // columns) * cell[1]
        sheet.paste(image, (x + (cell[0] - image.width) // 2, y + label_height))
        draw.rectangle([x + 1, y + 1, x + cell[0] - 2, y + label_height - 2], fill="#161b24")
        for row, line in enumerate(label_lines(f"{index + 1}. {label}")):
            draw.text((x + 9, y + 4 + row * line_step), line, font=font, fill="#ffd966")
    buffer = BytesIO()
    sheet.save(buffer, format="JPEG", quality=quality)
    return base64.b64encode(buffer.getvalue()).decode("ascii")



ADMIN_ID = "10000"
HELP = """🎨 NovelAI 绘图
/画图 <提示词>：默认 4.5 Full
/画图画风 <提示词>：5 分钟内在原聊天发送一张图片，仅参考画风
/画图检查 <提示词>（亦可 /画图预览）：检查最终计划，不生成图片；云端翻译可能计费
/高清画图检查、/高清画图预览：同档检查 5 Full 计划，不生成图片
/高清画图 <提示词>：默认 5 Full，正常画布（按尺寸计 Anlas）
/画小图 <提示词>：5 Full 的小图档（竖 512×768／横 768×512／方 640×640），走订阅无限额度，不计 Anlas
除 /画图 与 /画图检查 外，其余命令默认 5 Full；任意命令写 --模型 4.5 可切回 4.5。
/打开画图、/关闭画图：仅管理员，启停本插件绘图；Ollama 模式同时管理翻译模型
可选参数：--比例 方图|竖图|横图 --种子 0～4294967295
--步数 1～50 --数量 1～2 --负面 带空格的文本 --模型 普通|高清
--引导 0～10 --采样器 官方名称 --自动参数 on|off
--角色强度/--角色保真度 0～1 --画风强度/--画风保真度 0～1（图片参考仅4.5）
--参考模式 自动|角色|角色画风|关闭 --画风权重 0～2（文字画风权重）
默认自动规划；手动参数优先，免费检查显示实际计划。
英文别名：--scale --sampler --auto --character-strength --character-fidelity
--style-strength --style-fidelity --reference-mode --style-weight；--ratio --seed --steps --count --negative --model
中文描述自动翻译为 Danbooru 标签；NanoGPT 翻译会产生其账户用量。
匹配角色时英文描述也会合并角色资料。
多人描述自动拆栏；显式例：全局：两人站在花园-角色1：左侧伊蕾娜-角色2：右侧甘雨（最多6人）。
原样标签：/画图 原样把我的标签传入：<标签>，或 /画图 不要看这个提示词：<标签>。
原样模式跳过翻译、词库、知识库与自动画风；支持 | 分栏及手动参数。
4.5 单角色默认同图独立参考角色与画风，两项合计每图额外 10 Anlas（比单项多 5）。
--参考模式 角色画风 使用一项联合参考，每图额外 5 Anlas；5 仅文字资料。
点名特色画风可整档换风格，共 46 档：厚涂 / 赛璐璐 / 水彩 / 素描 / 像素 /
写实 / 锐利线稿，外加 39 款画师档（ciloranko、rella、wlop、sakimichan、
torino aqua、as109、lack 等）。画师名支持模糊写法：分隔符、大小写、连写、
去掉 (xxx) 后缀都认，中文与日文名也认。
/查看画风（别名 /画风列表）：合并转发列出全部画风，每条先发名字，再按模型分别给出参考图。
/查看画师 <画师名>：合并转发该画师的两套参考图（发原图，细节完整）；画师名支持模糊写法，
写一半也认（torino、mika、sakimi 都行）。
/画风预览（别名 /画风对照表）：两张完整拼接图逐条直发，图顶标明 4.5 与 5 模型。
对照表每格尺寸可用 [style] sheet_cell_width / sheet_columns / sheet_quality 调大。
不点名走默认柔光档；4.5 上每档附带内置参考图，每图额外 5 Anlas；5 不支持参考图，只走标签。
提示词写在参数前；负面词保留空格到下一个参数，亦支持引号。
例：/画图 castle under a starry sky --比例 横图 --种子 123456
生成可能消耗 Anlas。"""

class PluginSection(PluginConfigBase):
    enabled: bool = Field(default=True, description="启用绘图")
    config_version: str = Field(default="1.4.0", description="配置版本")

class ApiSection(PluginConfigBase):
    base_url: str = Field(default="https://image.novelai.net", description="仅允许官方 HTTPS 地址")
    model: str = Field(default=MODEL_ALIASES["普通"], description="普通命令模型 ID")
    hd_model: str = Field(default=MODEL_ALIASES["高清"], description="高清命令模型 ID，遵循绘图允许名单")
    timeout_seconds: int = Field(default=180, description="请求总超时（5～180 秒）")
    proxy: str = Field(default="", description="可选显式代理，如 http://127.0.0.1:7897；留空则直连。该代理会看到 API 令牌")
    retry_attempts: int = Field(default=3, ge=1, le=8, description="连接层失败的重试次数（1～8）；代理线路不稳时靠它兜住，设为 1 即关闭重试")
    model_aliases: Dict[str, str] = Field(default_factory=lambda: dict(MODEL_ALIASES), description="模型别名映射")

class GenerationSection(PluginConfigBase):
    auto_parameters: bool = Field(default=True, description="默认自动选择引导和参考参数，手动选项优先")
    default_style_weight: float = Field(default=1.25, description="自动继承文字画风的权重（0～2），0 禁用")
    default_ratio: str = Field(default="竖图", description="方图、竖图或横图")
    default_steps: int = Field(default=28, description="默认步数（1～50）")
    default_scale: float = Field(default=5.0, description="提示词引导强度（0～10）")
    default_sampler: str = Field(default="k_euler_ancestral", description="官方采样器名称")
    default_negative_prompt: str = Field(default="low quality, blurry", description="默认负面词，安全负面词始终追加")
    max_images: int = Field(default=2, description="最大张数（1～2）")
    max_prompt_length: int = Field(default=3000, description="提示词字符上限（1～3000）")
    collapse_groups: List[str] = Field(default_factory=list, description="名单内群号里的出图只发合并转发；明确失败时小图重试一次，仍失败则保存原图待处理；其余群与私聊逐张直发")

class LimitsSection(PluginConfigBase):
    cooldown_seconds: int = Field(default=25, description="每个用户的冷却秒数")
    hd_cooldown_seconds: int = Field(default=40, description="高清 5 Full 每张图的冷却秒数，按本次张数累计（1 张 40 秒、2 张 80 秒）")
    max_concurrent_jobs: int = Field(default=1, description="最大并发请求（1～4）")

class TranslationSection(PluginConfigBase):
    enabled: bool = Field(default=True, description="启用中文提示词翻译")
    backend: str = Field(default="nanogpt", description="nanogpt 或 ollama；云端失败不会自动切换本地")
    nanogpt_model: str = Field(default=NANOGPT_MODEL, description="NanoGPT 翻译模型 ID")
    nanogpt_base_url: str = Field(default=NANOGPT_BASE_URL, description="固定 NanoGPT 官方 API 地址")
    proxy: str = Field(default="", description="NanoGPT 可选显式代理，留空直连")
    tag_index_enabled: bool = Field(default=True, description="启用本地 Danbooru 普通标签索引")
    tag_index_path: str = Field(default="", description="编译后的 index.json 路径；留空使用 MaiBot data/plugins/local.novelai-image/tag_index/index.json")
    model: str = Field(default=DEFAULT_MODEL, description="本机 Ollama 模型，仅用于提示词翻译")
    fallback_model: str = Field(default=DEFAULT_FALLBACK_MODEL, description="主模型翻译失败时改用的备用模型，用完即卸载；留空表示不降级")
    base_url: str = Field(default=DEFAULT_BASE_URL, description="仅允许本机 Ollama 地址")
    timeout_seconds: int = Field(default=60, description="翻译请求超时（1～60 秒）")
    num_gpu: int = Field(default=DEFAULT_NUM_GPU, description="送往 GPU 的层数；-1 表示交由 Ollama 按显存自动决定，0 为纯 CPU")
    repeat_penalty: float = Field(default=DEFAULT_REPEAT_PENALTY, description="重复惩罚（1.0～2.0）；过低会让小模型陷入重复退化")
    keep_alive: int = Field(default=DEFAULT_KEEP_ALIVE, description="翻译后模型保留秒数；0 表示每次用完立即卸载，-1 表示常驻")

class PermissionSection(PluginConfigBase):
    mode: str = Field(default="all", description="all 或 whitelist；普通和高清均遵循此名单")
    user_ids: List[str] = Field(default_factory=list, description="允许的用户 ID")
    group_ids: List[str] = Field(default_factory=list, description="允许的群 ID")

class CharacterSection(PluginConfigBase):
    enabled: bool = Field(default=True, description="读取已有角色知识库")
    catalog_path: str = Field(default="", description="共享 characters.json 路径；留空自动从 MaiBot data 根寻找")
    include_image_reference: bool = Field(default=True, description="4.5 单角色使用 Precise Reference，每图额外 5 Anlas")
    default_reference_type: str = Field(default="character&style", description="默认参考类型：character 或 character&style")
    auto_reference_policy: bool = Field(default=True, description="明确画风改为仅角色参考；服装姿势仅降低保真度，身份覆盖降低参考参数")
    override_strength: float = Field(default=0.35, description="发色、瞳色或物种身份覆盖时参考强度上限，0～1")
    override_fidelity: float = Field(default=0.15, description="发色、瞳色或物种身份覆盖时参考保真度上限，0～1")
    reference_strength: float = Field(default=0.75, description="角色参考强度，0～1")
    reference_fidelity: float = Field(default=0.6, description="角色参考保真度，0～1")
    outfit_fidelity: float = Field(default=0.35, description="服装或姿势变化时保真度上限；保留角色参考强度，0～1")
    separate_style_reference: bool = Field(default=True, description="普通单角色自动把同图拆为角色与画风参考；每张生成图合计额外 10 Anlas")
    auto_reference_strength: float = Field(default=0.8, description="自动角色参考强度，0～1")
    auto_reference_fidelity: float = Field(default=0.55, description="自动角色参考保真度，0～1")

class StyleSection(PluginConfigBase):
    enabled: bool = Field(default=True, description="启用 /画图画风 图片等待命令")
    default_tags: List[str] = Field(default_factory=lambda: list(DEFAULT_STYLE), description="普通 4.5 Full 自动默认画风标签；空列表可关闭")
    hd_default_tags: List[str] = Field(default_factory=lambda: list(HD_DEFAULT_STYLE), description="高清 5 Full 自动默认画风标签；空列表可关闭")
    default_negative_tags: List[str] = Field(default_factory=lambda: list(DEFAULT_NEGATIVE_STYLE), description="未指定画风时排除的传统媒介风格标签")
    artist_tags: str = Field(default="", description="可选普通模型画师标签，支持 NovelAI 权重语法，例如 0.35::artist:ainiwaffles::")
    hd_artist_tags: str = Field(default="", description="可选高清模型画师标签，支持 NovelAI 权重语法")
    reference_strength: float = Field(default=0.75, description="上传图片的画风参考强度，0～1")
    reference_fidelity: float = Field(default=0.6, description="上传图片的画风参考保真度，0～1")
    auto_reference_strength: float = Field(default=0.85, description="自动画风参考强度，0～1")
    auto_reference_fidelity: float = Field(default=0.7, description="自动画风参考保真度，0～1；不随服装姿势降低")
    max_reference_mb: int = Field(default=8, description="上传参考图大小上限（1～8 MiB）")
    max_pending_requests: int = Field(default=100, description="同时等待参考图的请求上限（1～100）")
    preset_enabled: bool = Field(default=True, description="提示词点名特色画风时整档替换默认预设（厚涂/赛璐璐/水彩/素描/像素/写实）")
    preset_reference_enabled: bool = Field(default=True, description="特色画风附带内置参考图；参考图槽位已占满或非 4.5 Full 时自动退化为纯标签")
    hd_default_negative_tags: List[str] = Field(default_factory=lambda: list(HD_DEFAULT_NEGATIVE_STYLE), description="高清 5 Full 未指定画风时的排除标签；5 需保留颜色，故不含去色档")
    hd_default_only_negative_tags: List[str] = Field(default_factory=lambda: list(HD_DEFAULT_ONLY_NEGATIVE_STYLE), description="默认旧版配方附加排除词，历史默认值为空")
    sheet_columns: int = Field(default=6, ge=3, le=10, description="/画风预览 对照表每行几格（3～10）")
    sheet_cell_width: int = Field(default=700, ge=240, le=900, description="/画风预览 对照表单格宽度像素（240～900）；调大更清楚、载荷也更大")
    sheet_quality: int = Field(default=88, ge=60, le=95, description="/画风预览 对照表 JPEG 质量（60～95）")

class WorkflowSection(PluginConfigBase):
    retention_hours: int = Field(default=24, ge=1, le=168, description="个人任务与临时图片保留小时数（1～168）")
    persist_images: bool = Field(default=False, description="将任务图片及图片参考写入插件数据目录；默认仅内存保留，重启后图片不可用")

class NovelAIConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    api: ApiSection = Field(default_factory=ApiSection)
    generation: GenerationSection = Field(default_factory=GenerationSection)
    limits: LimitsSection = Field(default_factory=LimitsSection)
    translation: TranslationSection = Field(default_factory=TranslationSection)
    permission: PermissionSection = Field(default_factory=PermissionSection)
    characters: CharacterSection = Field(default_factory=CharacterSection)
    style: StyleSection = Field(default_factory=StyleSection)
    workflow: WorkflowSection = Field(default_factory=WorkflowSection)

class JobGate:
    """在单次同步操作中预留用户和并发名额，不积压收费任务。"""
    def __init__(self, max_jobs: int, cooldown: int):
        self.semaphore = asyncio.Semaphore(max_jobs)
        self.max_jobs, self.cooldown = max_jobs, cooldown
        self.active = set()
        self.until = {}

    def reserve(self, user_id: str, cooldown: Optional[int] = None) -> None:
        if not user_id:
            raise NovelAIError("无法识别发送者，请重新发送命令。")
        if user_id in self.active:
            raise NovelAIError("你的绘图请求正在处理中。")
        remaining = self.until.get(user_id, 0) - time.monotonic()
        if remaining > 0:
            raise NovelAIError(f"冷却中，请 {math.ceil(remaining)} 秒后再试。")
        if len(self.active) >= self.max_jobs:
            raise NovelAIError("绘图服务忙碌，请稍后再试。")
        self.active.add(user_id)
        self.until[user_id] = time.monotonic() + (self.cooldown if cooldown is None else cooldown)

    def release(self, user_id: str) -> None:
        self.active.discard(user_id)

class NovelAIImagePlugin(WorkflowMixin, MaiBotPlugin):
    config_model = NovelAIConfig
    _tag_index: DanbooruTagIndex | None = None

    async def on_unload(self) -> None:
        """关闭后台预加载任务；保留模型以兼容插件热更新。"""
        task = self._preload_task
        if task is not None and not task.done():
            task.cancel()
        self._pending_style.clear()
        for task in self._style_tasks:
            task.cancel()
        await asyncio.gather(*self._style_tasks, return_exceptions=True)
        tasks = list(self._workflow_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._workflow_cleanup_task.cancel()
        await asyncio.gather(self._workflow_cleanup_task, return_exceptions=True)
        logger.info("NovelAI 绘图插件已卸载")

    async def on_config_update(self, scope: str, config_data: Dict, version: str) -> None:
        """校验热更新配置；正在生成的请求使用其原有并发名额。"""
        self._validate_config()
        logger.info("NovelAI 绘图插件配置已更新")

    async def on_load(self) -> None:
        self._tag_indexes_cache = None
        self._gate = None
        self._gate_settings = None
        self._switch_lock = asyncio.Lock()
        self._preload_task = None
        self._pending_style = {}
        self._style_tasks = set()
        self._workflow_init()
        self._workflow_cleanup_task = asyncio.create_task(self._workflow_cleanup())
        self._validate_config()
        if self.config.plugin.enabled and self.config.translation.enabled and self.config.translation.backend == "ollama" and not self._drawing_disabled():
            self._preload_task = asyncio.create_task(self._preload_translation_model())
        logger.info("NovelAI 绘图插件已加载")

    async def _preload_translation_model(self) -> None:
        try:
            await self._translator().preload()
        except asyncio.CancelledError:
            raise
        except NovelAIError as exc:
            logger.warning("绘图翻译模型预加载失败：%s", str(exc))
        except Exception as exc:
            logger.warning("绘图翻译模型预加载失败，异常类型：%s", type(exc).__name__)

    def _disabled_marker(self) -> Path:
        data_dir = Path(self.ctx.paths.data_dir)
        return data_dir / "drawing.disabled"

    def _drawing_disabled(self) -> bool:
        return self._disabled_marker().exists()

    def _tag_indexes(self) -> list:
        """普通视觉标签词典 + 官方角色词典；每个实例缓存，文件变化时由词典自行重读。"""
        cfg = self.config.translation
        if not cfg.tag_index_enabled:
            return []
        general_path = Path(cfg.tag_index_path) if cfg.tag_index_path else Path(__file__).resolve().parents[2] / "data" / "plugins" / "local.novelai-image" / "tag_index" / "index.json"
        wanted = [(general_path, 0)]
        # 角色索引可选：缺失时只跳过它，不影响普通翻译。
        character_path = general_path.parent / "character_index.json"
        if character_path.is_file():
            wanted.append((character_path, 4))
        paths = [path for path, _ in wanted]
        if self._tag_indexes_cache is None or [index.path for index in self._tag_indexes_cache] != paths:
            self._tag_indexes_cache = [DanbooruTagIndex(path, category) for path, category in wanted]
        return self._tag_indexes_cache

    def _character_index(self):
        """官方角色词典；未启用或缺失时返回 None。"""
        return next((index for index in self._tag_indexes() if index.category == 4), None)

    def _translator(self) -> OllamaTranslator | NanoGPTTranslator:
        """云端与本地显式选一，不自动跨后端重试。"""
        cfg = self.config.translation
        if cfg.backend == "nanogpt":
            return NanoGPTTranslator(cfg.nanogpt_base_url, cfg.nanogpt_model, cfg.timeout_seconds, proxy=cfg.proxy, tag_index=self._tag_indexes())
        if cfg.backend != "ollama":
            raise NovelAIError("配置错误：translation.backend 必须为 nanogpt 或 ollama。")
        return OllamaTranslator(cfg.base_url, cfg.model, cfg.timeout_seconds, num_gpu=cfg.num_gpu, repeat_penalty=cfg.repeat_penalty, fallback_model=cfg.fallback_model, keep_alive=cfg.keep_alive)

    def _client(self) -> NovelAIClient:
        """按当前配置构造 NovelAI 客户端，统一带上显式代理。"""
        cfg = self.config.api
        return NovelAIClient(cfg.base_url, cfg.timeout_seconds, proxy=cfg.proxy, retry_attempts=cfg.retry_attempts)

    def _is_admin(self, platform: str, user_id: str) -> bool:
        return platform == "qq" and str(user_id) == ADMIN_ID

    @Command("novelai_draw_open", description="管理员开启 NovelAI 绘图", pattern=r"^/打开画图\s*$", timeout_ms=90_000)
    async def open_drawing(self, stream_id: str = "", user_id: str = "", platform: str = "", **kwargs):
        if not self._is_admin(platform, str(user_id)):
            await self.ctx.send.text("此命令仅限管理员使用。", stream_id)
            return True, "绘图开关权限不足", 1
        async with self._switch_lock:
            try:
                if self.config.translation.enabled and self.config.translation.backend == "ollama":
                    await self._translator().preload()
                marker = self._disabled_marker()
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.unlink(missing_ok=True)
            except NovelAIError as exc:
                await self.ctx.send.text("无法开启绘图：" + str(exc), stream_id)
                return True, "绘图开启失败", 1
            except Exception as exc:
                logger.error("开启绘图失败，异常类型：%s", type(exc).__name__)
                await self.ctx.send.text("开启绘图失败，请管理员检查插件数据目录和翻译配置。", stream_id)
                return True, "绘图开启失败", 1
        if not self.config.translation.enabled:
            translation_note = "提示词翻译当前已关闭。"
        elif self.config.translation.backend == "nanogpt":
            translation_note = "使用 NanoGPT 云端翻译，本地翻译模型未启动。"
        elif self.config.translation.keep_alive == 0:
            translation_note = "翻译按需加载，用完即卸载。"
        else:
            translation_note = "翻译模型已预加载。"
        await self.ctx.send.text("绘图已开启。" + translation_note, stream_id)
        return True, "绘图已开启", 1

    @Command("novelai_draw_close", description="管理员关闭 NovelAI 绘图", pattern=r"^/关闭画图\s*$", timeout_ms=90_000)
    async def close_drawing(self, stream_id: str = "", user_id: str = "", platform: str = "", **kwargs):
        if not self._is_admin(platform, str(user_id)):
            await self.ctx.send.text("此命令仅限管理员使用。", stream_id)
            return True, "绘图开关权限不足", 1
        async with self._switch_lock:
            if self._gate is not None and self._gate.active:
                await self.ctx.send.text("当前有绘图请求正在处理，请完成后再关闭。", stream_id)
                return True, "绘图正在处理中", 1
            if self._preload_task is not None and not self._preload_task.done():
                self._preload_task.cancel()
                await asyncio.gather(self._preload_task, return_exceptions=True)
            if self._gate is not None and self._gate.active:
                await self.ctx.send.text("当前有绘图请求正在处理，请完成后再关闭。", stream_id)
                return True, "绘图正在处理中", 1
            marker = self._disabled_marker()
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("disabled\n", encoding="utf-8")
                self._pending_style.clear()
            except OSError:
                await self.ctx.send.text("无法保存绘图关闭状态，请管理员检查插件数据目录。", stream_id)
                return True, "绘图关闭失败", 1
            try:
                if self.config.translation.enabled and self.config.translation.backend == "ollama":
                    await self._translator().unload()
            except NovelAIError as exc:
                logger.warning("绘图已关闭，但翻译模型卸载失败：%s", str(exc))
                await self.ctx.send.text("绘图已关闭，但 Ollama 未能确认卸载翻译模型，请管理员检查本机服务。", stream_id)
                return True, "绘图已关闭，模型卸载未确认", 1
        await self.ctx.send.text("绘图已关闭。" + ("本插件翻译模型已卸载。" if self.config.translation.enabled and self.config.translation.backend == "ollama" else "提示词翻译已暂停。"), stream_id)
        return True, "绘图已关闭", 1

    def _validate_config(self) -> None:
        cfg = self.config
        self._client()
        if cfg.permission.mode not in ("all", "whitelist"):
            raise NovelAIError("配置错误：permission.mode 必须为 all 或 whitelist。")
        if not 1 <= cfg.limits.max_concurrent_jobs <= 4 or not 0 <= cfg.limits.cooldown_seconds <= 3600:
            raise NovelAIError("配置错误：并发须为 1～4，冷却须为 0～3600 秒。")
        if not 1 <= cfg.generation.max_images <= 2 or not 1 <= cfg.generation.max_prompt_length <= 3000:
            raise NovelAIError("配置错误：图片数须为 1～2，提示词长度须为 1～3000。")
        if not 0 <= cfg.generation.default_scale <= 10 or cfg.generation.default_sampler not in SAMPLERS:
            raise NovelAIError("配置错误：引导强度或采样器不支持。")
        if not math.isfinite(cfg.generation.default_style_weight) or not 0 <= cfg.generation.default_style_weight <= 2:
            raise NovelAIError("配置错误：文字画风权重须为 0～2。")
        models = [cfg.api.model, cfg.api.hd_model, *cfg.api.model_aliases.values()]
        if any(model not in MODEL_ALIASES.values() for model in models):
            raise NovelAIError("配置错误：当前版本仅支持 4.5 Full 与 5 Full 文生图模型。")
        parse_args("landscape", default_ratio=cfg.generation.default_ratio, default_steps=cfg.generation.default_steps)
        self._translator()
        if cfg.characters.default_reference_type not in ("character", "character&style"):
            raise NovelAIError("配置错误：默认参考类型须为 character 或 character&style。")
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in (cfg.characters.reference_strength, cfg.characters.reference_fidelity, cfg.characters.auto_reference_strength, cfg.characters.auto_reference_fidelity, cfg.characters.override_strength, cfg.characters.override_fidelity, cfg.characters.outfit_fidelity, cfg.style.reference_strength, cfg.style.reference_fidelity, cfg.style.auto_reference_strength, cfg.style.auto_reference_fidelity)):
            raise NovelAIError("配置错误：角色参考强度和保真度须为 0～1。")
        if not 1 <= cfg.style.max_reference_mb <= 8 or not 1 <= cfg.style.max_pending_requests <= 100:
            raise NovelAIError("配置错误：画风图上限须为 1～8 MiB，等待请求上限须为 1～100。")

    async def _attach_preset_reference(self, cfg, req, extras, character_reference, preset: StylePreset) -> str:
        """给特色预设挂内置参考图。槽位不够或模型不支持时退化为纯标签，并说明原因。"""
        if not cfg.style.preset_reference_enabled or not preset.reference:
            return ""
        if req.model != MODEL_ALIASES["普通"]:
            return "5 Full 不支持参考图，本档仅走标签。"
        if len(extras) + (1 if character_reference else 0) >= 2:
            return "参考图已占满两张，本档仅走标签。"
        image = await asyncio.to_thread(preset_reference_image, preset, cfg.style.max_reference_mb * 1024 * 1024)
        if not image:
            return "内置参考图缺失，本档仅走标签。"
        strength, fidelity = preset.reference_strength, preset.reference_fidelity
        if preset.key != "artist_torino_aqua":
            strength = req.style_strength if req.style_strength is not None else strength
            fidelity = req.style_fidelity if req.style_fidelity is not None else fidelity
        extras.append({"image": image, "type": "style", "strength": strength, "fidelity": fidelity})
        return f"内置参考图 {preset.reference}（强度 {strength:g}），每图额外 5 Anlas。"

    async def _plan_request(self, req, user_id: str, platform: str, style_reference: str | None = None):
        """生成与免费检查共用同一计划；模型仅接收去掉角色名的本轮描述。"""
        cfg = self.config
        if req.raw_prompt:
            # 直通优先于词库、知识库、画风继承、翻译和自动扩写。
            base, raw_characters = pipe_sections(req.prompt)
            captions, negative_captions = caption_payloads(raw_characters)
            scale = req.scale if req.scale is not None else cfg.generation.default_scale
            sampler = req.sampler if req.sampler is not None else cfg.generation.default_sampler
            extras = []
            if style_reference:
                extras.append({"image": style_reference, "type": "style",
                               "strength": req.style_strength if req.style_strength is not None else cfg.style.reference_strength,
                               "fidelity": req.style_fidelity if req.style_fidelity is not None else cfg.style.reference_fidelity})
            elif any(value is not None for value in (req.style_strength, req.style_fidelity)):
                raise NovelAIError("原样模式未上传画风图，画风强度与保真度无法生效。")
            if any(value is not None for value in (req.character_strength, req.character_fidelity)):
                raise NovelAIError("原样模式不匹配角色知识库，不能设置知识库角色参考强度或保真度。")
            req = replace(req, prompt=base, scale=scale, sampler=sampler)
            payload = build_payload(req, scale, sampler, extra_references=extras,
                                    char_captions=captions, char_negative_captions=negative_captions)
            note = "原样标签直通：已跳过翻译、词库/角色匹配、预设画风与自动扩写。"
            if captions:
                note += f" 已按 | 分栏发送 {len(captions)} 个角色。"
            return req, payload, note, "文字" + (" + style" if extras else ""), 0.0, 0.0
        automatic = cfg.generation.auto_parameters if req.auto is None else req.auto
        if req.model == MODEL_ALIASES["高清"]:
            if any(value is not None for value in (req.character_strength, req.character_fidelity, req.style_strength, req.style_fidelity)) or req.reference_mode in ("character", "character&style"):
                raise NovelAIError("5 Full 当前不支持 Precise Reference 或 Vibe Transfer；图片强度、保真度和角色参考模式仅支持普通模型。高清可用 --画风权重 调整文字画风。")
        if style_reference and req.reference_mode == "character&style":
            raise NovelAIError("上传画风图时知识库只能参考角色，避免混入两种画风；请使用 --参考模式 角色或自动。")
        if style_reference and req.reference_mode == "off":
            raise NovelAIError("已关闭图片参考，请直接使用 /画图，不要上传画风图。")
        roles = await asyncio.to_thread(load_characters, Path(self.ctx.paths.data_dir), req.prompt, user_id if platform == "qq" else "", cfg.characters.catalog_path) if cfg.characters.enabled else []
        if automatic and "ratio" not in req.explicit:
            ratio = None
            if re.search(r"宽幅|全景|横向|landscape|panorama|wide shot", req.prompt, re.I):
                ratio = "横图"
            elif re.search(r"全身|竖幅|full body|fullbody|portrait", req.prompt, re.I):
                ratio = "竖图"
            if ratio:
                # /画小图 走 Small 档，改画布必须沿用原来那张表。
                sizes = size_table_for(req)
                req = replace(req, width=sizes[ratio][0], height=sizes[ratio][1])
        # 每个角色各自留一份身份标签：多角色时它们要一人一条 char_caption，
        # 摊平成一条会让模型把两个人的发色瞳色混成一个。
        identity_groups = [identity_tags(role, req.prompt) for role in roles]
        identity = [tag for group in identity_groups for tag in group]
        request_text = strip_names(req.prompt, roles) if roles else req.prompt
        # 官方作品角色：确定性换成规范 name_(series) 标签，命中的角色名随之剔除。
        preset_hint = match_preset(request_text)
        character_match_text = strip_preset_keywords(request_text, preset_hint) if preset_hint else request_text
        official_characters, official_works, request_text = resolve_official_split(
            self._character_index(), request_text, character_match_text)
        supplement_characters, supplement_scene, _hits = supplement_tags(request_text)
        for term in supplement_characters:
            if term not in official_characters:
                official_characters.append(term)
        multi_scene = needs_character_split(req.prompt, len(roles) + len(official_characters))
        official_works = [*official_works, *supplement_scene]
        reference = None
        note = "未匹配角色，仅使用本轮描述。"
        mode = cfg.characters.default_reference_type
        strength, fidelity = (cfg.characters.auto_reference_strength, cfg.characters.auto_reference_fidelity) if automatic else (cfg.characters.reference_strength, cfg.characters.reference_fidelity)
        scale = req.scale if req.scale is not None else (5.5 if automatic else cfg.generation.default_scale)
        profile = "标准"
        if automatic and req.scale is None:
            if re.search(r"复杂动作|打斗|追逐|跳跃|combat|fighting|chasing|jumping", request_text, re.I):
                scale, profile = 6.0, "多人或动作"
            elif re.search(r"柔光|柔和|渐变|细线|soft lighting|soft gradient|delicate lines", request_text + " " + " ".join(style_tags(roles)), re.I):
                scale, profile = 5.3, "柔光细线"
        sampler = req.sampler if req.sampler is not None else cfg.generation.default_sampler
        style_weight = req.style_weight if req.style_weight is not None else (cfg.generation.default_style_weight if automatic else 0.0)
        # 正负描述都参与判断：用户排除某种画风也应解除参考图的画风约束。
        policy_text = request_text + " " + req.negative
        if automatic and cfg.characters.auto_reference_policy:
            if re.search(r"水彩|油画|素描|写实|像素|漫画|画风|风格|watercolou?r|oil painting|sketch|realistic|photoreal|pixel art|manga|style|3d|cel shading|lineart", policy_text, re.I):
                mode = "character"
                note = "明确指定或排除画风，参考图只保留角色。"
            if re.search(r"发色|[黑白红金棕粉紫蓝绿灰银]发|瞳|眼色|(?:black|white|red|blond[e]?|brown|pink|purple|blue|green|grey|gray|silver) hair|(?:blue|red|green|yellow|purple|grey|gray) eyes|无猫耳|无尾|no (?:ears|tail)", policy_text, re.I):
                strength, fidelity = min(strength, cfg.characters.override_strength), min(fidelity, cfg.characters.override_fidelity)
                note += " 检测到身份特征覆盖，降低参考约束。"
            elif re.search(r"衣|裙|服|泳装|比基尼|姿势|坐|站|跑|躺|马尾|长发|短发|卷发|直发|dress|outfit|clothes|uniform|bikini|swimsuit|running|sitting|standing|lying|pose|ponytail|twintails|(?:long|short|curly|straight) hair", policy_text, re.I):
                fidelity = min(fidelity, cfg.characters.outfit_fidelity)
                note += " 服装或姿势由文字决定，保留角色参考强度并降低保真度。"
        # 上传图与知识库图承担独立职责，上传画风不得把知识库风格一并带入。
        if style_reference:
            mode = "character"
        if req.reference_mode not in (None, "auto"):
            mode = req.reference_mode
        if req.character_strength is not None:
            strength = req.character_strength
        if req.character_fidelity is not None:
            fidelity = req.character_fidelity
        if roles:
            note = "角色资料：" + "、".join(role.name for role in roles) + f"（身份标签 {len(identity)} 项）。" + (note if note != "未匹配角色，仅使用本轮描述。" else "")
            if mode == "off" or not cfg.characters.include_image_reference:
                note += "参考图已禁用。"
            elif req.model != MODEL_ALIASES["普通"]:
                note += "5 Full 仅使用文字角色资料。"
            elif multi_scene:
                note += "多角色仅使用文字资料，避免参考图混合身份。"
            else:
                reference = await asyncio.to_thread(precise_reference, roles[0])
                note += f"参考：{mode}，每图额外 5 Anlas。" if reference else "没有可用参考图，仅使用文字资料。"
        if reference is None and (req.character_strength is not None or req.character_fidelity is not None):
            raise NovelAIError("本次没有可用的角色参考图，角色强度和保真度无法生效；请匹配一个有参考图的角色并开启图片参考。")
        extras = []
        # 同图分开承担身份与画风，服装和动作调整只作用于角色参考。
        if reference and mode == "character&style" and cfg.characters.separate_style_reference and req.reference_mode in (None, "auto") and not style_reference:
            mode = "character"
            note = note.replace("参考：character&style，每图额外 5 Anlas。", "角色参考：character，每图额外 5 Anlas。")
            extras.append({"image": reference, "type": "style", "strength": req.style_strength if req.style_strength is not None else (cfg.style.auto_reference_strength if automatic else cfg.style.reference_strength), "fidelity": req.style_fidelity if req.style_fidelity is not None else (cfg.style.auto_reference_fidelity if automatic else cfg.style.reference_fidelity)})
            note += " 同图独立参考画风，服装和姿势不降低画风保真度；每张生成图再额外 5 Anlas，两项合计额外 10 Anlas。"
        if style_reference:
            if req.model != MODEL_ALIASES["普通"]:
                raise NovelAIError("/画图画风 仅支持 4.5 Full，请使用 --模型 普通。")
            clauses = [clause for clause in re.split(r"[,，。;；\n]", req.prompt) if not re.search(r"不要|不需|不参考|不采用|不照搬|不复制|不沿用|不复刻|仅.*画风|只.*画风|do not|don't|ignore|style only|only.*style", clause, re.I)]
            if any(re.search(r"(?:图中|参考图|图片).{0,8}(?:内容|场景|构图|背景)|(?:copy|recreate|reference).{0,25}(?:composition|scene|background|image content)", clause, re.I) for clause in clauses):
                raise NovelAIError("画风参考支持风格和人物；不能可靠复刻参考图场景、构图或背景，请在提示词中具体描述所需内容。")
            content = any(re.search(r"(?:参考图|图中|图片).{0,8}(?:人物|角色)|(?:use|copy|recreate|reference).{0,25}(?:character|person).{0,20}(?:reference image|uploaded image)|(?:reference image|uploaded image).{0,15}(?:character|person)", clause, re.I) for clause in clauses)
            if content and reference:
                raise NovelAIError("同时指定知识库角色和参考图人物会混合身份，请保留一种人物来源。")
            upload_mode = "character&style" if content else "style"
            extras.append({"image": style_reference, "type": upload_mode, "strength": req.style_strength if req.style_strength is not None else (cfg.style.auto_reference_strength if automatic else cfg.style.reference_strength), "fidelity": req.style_fidelity if req.style_fidelity is not None else (cfg.style.auto_reference_fidelity if automatic else cfg.style.reference_fidelity)})
            note += f" 上传图参考：{upload_mode}，每图额外 5 Anlas；" + ("参考上传图人物和画风。" if content else "仅参考画风，人物、服装、动作和背景按文字及角色资料决定。")
        # 画风优先级：图片参考 > 点名特色预设 > 角色资料词表 > 默认柔光预设。
        inherited_style: List[str] = []
        style_source = "无"
        elaina_default = False
        hd = req.model == MODEL_ALIASES["高清"]
        # 只看正面描述：负面里的 cel shading / flat colors 是配置用来挡平涂的，
        # 拿它去匹配会让每一张普通图都静默换成赛璐璐档。
        style_text = request_text
        preset = match_preset(style_text) if (cfg.style.preset_enabled and not extras and style_weight > 0) else None
        if extras:
            style_source = "图片参考"
        elif preset is not None:
            if preset.key == "artist_torino_aqua" and (req.style_strength is not None or req.style_fidelity is not None):
                raise NovelAIError("本次没有独立画风参考图，画风强度和保真度无法生效；请使用 /画图画风 上传图片，或启用普通单角色独立画风参考。文字画风请用 --画风权重。")
            # 整档替换而不是叠加：预设自带的标签和默认那套说的是两件事，叠起来会互相打架
            # （正面要平涂、基础负面在挡平涂，模型会解成高反差杂色）。
            inherited_style = [tag.strip() for tag in preset_render_tags(preset, v5=hd) if tag.strip()]
            if preset.key.startswith("artist_") and preset.key != "artist_torino_aqua":
                inherited_style = scene_priority_artist_tags(inherited_style, request_text)
            style_source = f"预设画风：{preset.label}"
            # 预设已经消费的画风关键词不再进入翻译，避免赛璐璐被拆成角色 Lulu。
            request_text = strip_preset_keywords(request_text, preset)
            base_negatives = cfg.style.hd_default_negative_tags if hd else cfg.style.default_negative_tags
            tuning = generation_tuning(preset, v5=hd)
            if tuning is not None:
                # 调参结果随配方保存，运行时不读取参考图片的元数据；样图缺失或
                # PNG Comment 损坏不能影响纯文字画风请求。
                if tuning.negative_tags is not None:
                    base_negatives = tuning.negative_tags
                if automatic:
                    if req.style_weight is None and tuning.style_weight is not None:
                        style_weight = tuning.style_weight
                    if req.scale is None and tuning.scale is not None:
                        scale = tuning.scale
                    if req.sampler is None and tuning.sampler is not None:
                        sampler = tuning.sampler
                    if "steps" not in req.explicit and tuning.steps is not None:
                        req = replace(req, steps=tuning.steps)
                    profile = "画风配方"
            # 本轮明确给出的负面优先，预设只调整自动继承的默认负面。
            if "negative" not in req.explicit:
                existing = [part.strip() for part in req.negative.split(",") if part.strip()]
                existing += [tag.strip() for tag in base_negatives if tag.strip()]
                negatives = apply_preset_negatives(existing, preset, v5=hd)
                req = replace(req, negative=", ".join(dict.fromkeys(negatives)))
            reference_note = await self._attach_preset_reference(cfg, req, extras, reference, preset)
            if reference_note:
                note += " " + reference_note
        elif STYLE_REQUEST.search(style_text):
            # 描述的是表里没收录的画风，交给翻译模型按本轮措辞处理。
            style_source = "本轮描述"
        elif style_weight > 0:
            inherited_style = style_tags(roles)
            if inherited_style:
                style_source = "角色资料固定词表"
            elif automatic:
                configured = cfg.style.hd_default_tags if hd else cfg.style.default_tags
                inherited_style = [tag.strip() for tag in configured if tag.strip()]
                neg_source = cfg.style.hd_default_negative_tags if hd else cfg.style.default_negative_tags
                excluded = [tag.strip() for tag in neg_source if tag.strip()]
                if hd:
                    excluded += [tag.strip() for tag in cfg.style.hd_default_only_negative_tags if tag.strip()]
                # 雾化组只在暗室内景成立；依赖色彩或光线的场景摘掉它，并改压过曝，否则夜变白昼、夕阳无色。
                if not scene_keeps_haze(policy_text):
                    inherited_style = drop_haze(inherited_style)
                excluded = scene_negatives(policy_text, excluded)
                artist_tags = cfg.style.hd_artist_tags if hd else cfg.style.artist_tags
                if artist_tags.strip():
                    inherited_style.append(artist_tags.strip())
                if inherited_style:
                    style_source = "高清默认细线柔光预设" if hd else "默认细线柔光预设"
                    elaina_default = hd and not identity_groups and official_characters == ["elaina_(majo_no_tabitabi)"]
                    if "negative" not in req.explicit:
                        existing = [part.strip() for part in req.negative.split(",") if part.strip()]
                        combined = existing + excluded
                        if hd:
                            combined = [tag for tag in combined if tag.casefold().replace("_", " ") not in HD_DEFAULT_DROP_NEGATIVES]
                        req = replace(req, negative=", ".join(dict.fromkeys(combined)))
        else:
            style_source = "无"
        if not extras and (req.style_strength is not None or req.style_fidelity is not None):
            raise NovelAIError("本次没有独立画风参考图，画风强度和保真度无法生效；请使用 /画图画风 上传图片，或启用普通单角色独立画风参考。文字画风请用 --画风权重。")
        # 每个角色各自一条 caption：官方角色用规范标签，知识库角色用身份标签。
        # 多角色摊平成一条会让模型把两个人的发色瞳色混成一个。
        if len(identity_groups) > 1:
            captions = [", ".join(group) for group in identity_groups]
        else:
            captions = list(official_characters)
        # 已经进了 caption 的身份标签不能留在 base 里，否则又混回一起。
        caption_terms = {tag for group in captions for tag in group.split(", ")}
        trimmed = [tag for tag in identity if tag not in caption_terms]
        # 露骨内容追加反马赛克与手部矫正的排除词，不依赖本轮是否挂了预设画风。
        extra_neg = explicit_negatives(policy_text)
        if extra_neg:
            req = replace(req, negative=", ".join(dict.fromkeys(
                [part.strip() for part in req.negative.split(",") if part.strip()] + extra_neg)))
        negative_captions = None
        if multi_scene:
            # 多人分栏不应隐式提高默认柔光的 CFG，避免仅因人数改变渲染倾向。
            if automatic and req.scale is None and style_source in ("高清默认细线柔光预设", "默认细线柔光预设"):
                scale, profile = 5.5, "默认柔光多人"
            identities = [{"id": f"kb:{index}", "name": role.name, "aliases": list(role.aliases),
                           "tags": identity_tags(role, "")} for index, role in enumerate(roles)]
            identities += [{"id": f"official:{index}", "name": tag, "aliases": [], "tags": [tag]}
                           for index, tag in enumerate(official_characters)]
            scene_text = strip_preset_keywords(req.prompt, preset) if preset else req.prompt
            if cfg.translation.enabled and cfg.translation.backend == "nanogpt":
                scene = await self._translator().translate_scene(scene_text, req.negative, identities, style_context=style_source)
            else:
                base, characters = pipe_sections(scene_text)
                if not characters or contains_chinese(scene_text + req.negative):
                    raise NovelAIError("中文多角色自动拆分需要 NanoGPT 翻译；也可使用英文 全局 | 角色1 | 角色2 分栏。")
                scene = {"base": base, "negative": req.negative, "characters": characters}
            # 知识库外貌默认值已由拆分器逐人合并并处理显式覆盖；这里只锁定官方名字。
            identity_map = {identity["id"]: identity["tags"] for identity in identities if identity["id"].startswith("official:")}
            for character in scene["characters"]:
                character["positive"] = ", ".join(merge_tags(identity_map.get(character["identity_id"], []),
                                                           [tag.strip() for tag in character["positive"].split(",") if tag.strip()]))
            # 官方要求角色栏顺序与坐标一致；居中受事之后必须连同专属负面一起排序。
            ordered_characters = sorted(scene["characters"], key=lambda character: {
                "left": 0, "center": 1, "right": 2}.get(character.get("position", "auto"), 1))
            captions, negative_captions = caption_payloads(ordered_characters, auto_layout=True)
            # 多人默认保留完整群像及互动空间；用户明确要求局部特写时尊重原构图。
            close_framing = bool(re.search(r"特写|大头|局部|半身|上半身|close.?up|portrait|upper.?body|bust", scene_text, re.I))
            framing = [] if close_framing else ["full body", "wide shot"]
            request_text = ", ".join(merge_tags(framing, scene["base"].split(",")))
            framing_negative = [] if close_framing else ["close-up", "cropped", "out of frame", "solo focus"]
            req = replace(req, negative=", ".join(merge_tags(scene["negative"].split(","), framing_negative)))
            if not close_framing:
                for positive, negative in zip(captions, negative_captions):
                    positive["char_caption"] = ", ".join(merge_tags(["full body"], positive["char_caption"].split(",")))
                    negative["char_caption"] = ", ".join(merge_tags(negative["char_caption"].split(","), framing_negative))
            # 画布维持 Normal 档。曾试过把三人及以上升到 1216×1664 / 1536×2048，
            # 但那是在反写实负面还没补的时候——缺口补上后，大画布会多长出细节，
            # 观感反而更写实。同 seed 对比 2 人基准，Normal 档最贴合柔光目标。
            # 这里曾按"横向群像提供多人动作空间"把三人及以上的画布自动切成横版。
            # 实测横画幅会把两侧区域挤成贴边的前景大特写，三人全身入镜的构图反而更差；
            # 画布方向现在只由用户指令或配置决定，不再按人数改写。
            # 已绑定到独立栏的身份绝不能经 trimmed 再流回全局栏。
            trimmed = []
            note += f" 多角色独立分栏：{len(captions)}个角色，外貌、服装、动作和专属负面分开传入。"
        to_translate = {} if multi_scene else {key: value for key, value in {"positive": request_text, "negative": req.negative}.items() if contains_chinese(value)}
        if to_translate:
            if not cfg.translation.enabled:
                raise NovelAIError("检测到中文提示词，但提示词翻译已关闭；请改用英文或启用 translation.enabled。")
            legacy_default = cfg.translation.backend == "nanogpt" and style_source in (
                "高清默认细线柔光预设", "默认细线柔光预设")
            if legacy_default:
                translation_options = {"profile": "default_20261015_1917"}
            else:
                translation_options = {"style_context": style_source} if cfg.translation.backend == "nanogpt" and (preset is not None or extras or inherited_style) else {}
                if cfg.translation.backend == "nanogpt" and captions:
                    translation_options["content_context"] = f"{len(captions)} character identity captions supplied separately; preserve their existing appearance and outfit."
            translated = await self._translator().translate_many(to_translate, **translation_options)
            request_text = translated.get("positive", request_text)
            req = replace(req, negative=translated.get("negative", req.negative))
        # 角色改由 char_captions 承载，base 提示词里不再重复，避免被画风标签稀释。
        text_tags = [part.strip() for part in request_text.split(",")] if request_text else []
        if multi_scene and style_source in ("高清默认细线柔光预设", "默认细线柔光预设"):
            # 结构化多人翻译沿用已验收 V5 默认配方：基础质量词与 0.5 赛璐璐权重。
            # 只作用于默认档，不改单人历史翻译链、特色画风或上传画风图。
            text_tags = normalize_default_multichar_style(text_tags, req.prompt)
        if elaina_default:
            # 用户选定的同 seed 配方：只降低默认译文里的赛璐璐项，不改静态柔光块。
            # 此前还追加了 0.35::photorealistic, 3d render:: 的锁定负面，两项叠加会把
            # 脸压得发灰、眼睛发闷；实测只保留这一项时脸部才和甘雨等角色同档。
            text_tags = ["0.5::cel shading::" if tag.casefold().replace("_", " ") == "cel shading" else tag
                         for tag in text_tags]
        combined = merge_tags([*official_works, *trimmed], text_tags)
        if not combined:
            # 身份是唯一内容时（提示词里只写了角色名），全剔进 caption 会把 base 清空；
            # 那就把它们留在 base 里——重复一点，好过整条提示词空掉。
            combined = merge_tags([*official_works, *identity], text_tags)
        if not combined:
            raise NovelAIError("没有任何可用的提示词内容，请补充描述或角色资料。")
        if inherited_style:
            # NovelAI 官方提示建议把画风标签放在提示词前段，减少被内容标签淹没。
            combined[0:0] = weighted_style_tags(inherited_style, style_weight)
        # 露骨内容只追加局部的私处/行为细节标签；整体画风仍由上面那组画风标签决定。
        # 多人改走全局层：整层跳过会让请求的动作标签全部丢失，画出来根本没有那件事。
        explicit_pos = explicit_global_tags(policy_text) if multi_scene else explicit_tags(policy_text)
        if explicit_pos:
            combined = merge_tags(combined, explicit_pos)
        applied_style_weight = style_weight if inherited_style else 0.0
        req = replace(req, prompt=", ".join(combined), scale=scale, sampler=sampler, style_weight=applied_style_weight)
        if multi_scene and scene.get("description"):
            # 标签表达身份和画风；独立自然语言保留动作语义，避免抓握被扁平化为牵手。
            description = scene["description"]
            if not close_framing:
                description = "All " + str(len(captions)) + " characters are visible from head to toe in one wide shot. " + description
            # 保留默认和画师原有的画风块顺序，不为多人动作重排画风配方。
            req = replace(req, prompt=req.prompt + ". " + description)
        if multi_scene and req.model == MODEL_ALIASES["高清"] and contains_chinese(scene_text):
            # V5 原生支持多语言：保留去掉画风选择词的用户原句，避免译文将抓握降格成牵手。
            # 角色槽位/画风仍各自独立；这里不扩写原句，也不依赖特定角色或动作规则。
            req = replace(req, prompt=req.prompt + " " + scene_text.strip(" ,，。") + "。")
        caption_length = sum(len(item["char_caption"] if isinstance(item, dict) else item) for item in captions)
        if len(req.prompt) + caption_length > cfg.generation.max_prompt_length:
            raise NovelAIError("合并角色和画风后的提示词过长，请缩短描述后再试。")
        if multi_scene and style_source in ("高清默认细线柔光预设", "默认细线柔光预设"):
            # 画风只放在 base 会被角色栏摊薄，默认柔光又没有强制平面媒介的词，
            # 结果就是柔光叠在角色的官方 3D 先验上。但整段 44 标签塞进角色栏会把
            # V5 共用的注意力预算吃光，动作标签全部失效——所以只放精简核心。
            # 只作用于默认柔光多人；单人、画师预设和上传画风图都不经过这里。
            core = multichar_style_core()
            for caption in captions:
                if isinstance(caption, dict) and caption.get("char_caption"):
                    caption["char_caption"] = core + ", " + caption["char_caption"]
            # 高清档默认负面只有两个词，普通档那组反写实词在这条路径上一直缺失。
            # 负面不占正向注意力预算，所以不会像画风标签那样挤掉动作。
            req = replace(req, negative=req.negative.rstrip(", ") + ", " + multichar_realism_negative())
        if preset is not None and hd:
            note += " 5 Full 当前仅文字画风引导，不发送参考图片；无法保证复刻参考画风。"
        manual = "、".join(req.explicit) or "无"
        note += f" 参数：{'自动' if automatic else '配置'}（{profile}）；手动覆盖：{manual}。引导 {scale:g}；采样器 {sampler}；追加文字画风权重 {applied_style_weight:g}（画风来源：{style_source}；不是图片参考精度）。"
        if extras:
            note += f" 图片画风强度 {extras[0]['strength']:g}；保真度 {extras[0]['fidelity']:g}。"
        payload = build_payload(req, scale, sampler, reference, strength, fidelity, mode, extras, captions or None, negative_captions)
        recorded_style_tags = weighted_style_tags(inherited_style, style_weight)
        if style_source in ("高清默认细线柔光预设", "默认细线柔光预设"):
            # 默认档的历史翻译尾词同样决定媒介；系列/收藏不能只保存静态块而丢掉它们。
            for tag in text_tags:
                normalized = re.sub(r"^[\d.]+::|::$", "", tag).casefold().replace("_", " ").strip()
                if normalized in ("anime style", "cel shading", "soft lighting", "detailed") and tag not in recorded_style_tags:
                    recorded_style_tags.append(tag)
        payload = WorkflowPayload(payload, {"label": style_source,
            "tags": recorded_style_tags,
            "negative": list(preset.extra_negatives) if preset else [],
            "drop_negative": list(preset.drop_negatives) if preset else []} if inherited_style else None,
            identity=[", ".join(group) for group in identity_groups] or list(official_characters))
        return req, payload, note, (mode if reference else "文字") + (" + " + extras[0]["type"] if extras else ""), strength, fidelity

    @Command("novelai_preview", description="免费检查绘图计划", pattern=r"^/(?P<kind>高清)?画图(?:检查|预览)(?:\s+(?P<args>[\s\S]*))?$", timeout_ms=90_000)
    async def preview(self, stream_id: str = "", user_id: str = "", group_id: str = "", platform: str = "", matched_groups=None, **kwargs):
        reserved = False
        gate_key = platform + ":" + str(user_id) if user_id and platform else ""
        try:
            self._validate_config()
            cfg = self.config
            if not cfg.plugin.enabled or self._drawing_disabled():
                raise NovelAIError("绘图当前已关闭。")
            if cfg.permission.mode == "whitelist" and str(user_id) not in cfg.permission.user_ids and str(group_id) not in cfg.permission.group_ids:
                raise NovelAIError("你或当前群不在绘图允许名单中。")
            # /高清画图检查 与 /高清画图 同档：默认 5 Full；/画图检查 仍是 4.5。
            hd = (matched_groups or {}).get("kind") == "高清"
            req = parse_args((matched_groups or {}).get("args") or "", default_model=cfg.api.hd_model if hd else cfg.api.model, default_ratio=cfg.generation.default_ratio, default_steps=cfg.generation.default_steps, default_negative=cfg.generation.default_negative_prompt, max_images=cfg.generation.max_images, max_prompt_length=cfg.generation.max_prompt_length, model_aliases=cfg.api.model_aliases)
            settings = (cfg.limits.max_concurrent_jobs, cfg.limits.cooldown_seconds)
            if self._gate is None or (self._gate_settings != settings and not self._gate.active):
                self._gate, self._gate_settings = JobGate(*settings), settings
            self._gate.reserve(gate_key)
            gate = self._gate
            reserved = True
            async with gate.semaphore:
                async with asyncio.timeout(cfg.translation.timeout_seconds + 10):
                    req, payload, note, mode, strength, fidelity = await self._plan_request(req, str(user_id), platform)
            reference_detail = "未发送图片参考" if mode == "文字" else f"强度：{strength:g}；保真度：{fidelity:g}"
            positive_chars = payload["parameters"]["v4_prompt"]["caption"]["char_captions"]
            negative_chars = payload["parameters"]["v4_negative_prompt"]["caption"]["char_captions"]
            character_detail = "".join(f"\n角色{i + 1}：{item['char_caption']}\n角色{i + 1}负面：{negative_chars[i]['char_caption'] if i < len(negative_chars) else ''}"
                                       for i, item in enumerate(positive_chars))
            await self.ctx.send.text(f"绘图预览（未调用 NovelAI 生成；NanoGPT 翻译可能计费）\n模型：{req.model}\n尺寸：{req.width}×{req.height}；步数：{req.steps}；数量：{req.count}\n{note}\n参考模式：{mode}；{reference_detail}\n正面：{req.prompt}\n负面：{req.negative}{character_detail}", stream_id)
        except NovelAIError as exc:
            await self.ctx.send.text(str(exc), stream_id)
        except TimeoutError:
            await self.ctx.send.text("绘图预览超时，请稍后再试。", stream_id)
        except Exception as exc:
            logger.error("绘图预览失败，异常类型：%s", type(exc).__name__)
            await self.ctx.send.text("绘图预览失败，请管理员检查插件状态。", stream_id)
        finally:
            if reserved:
                gate.release(gate_key)
        return True, "绘图预览已处理", 1

    @Command("novelai_help", description="NovelAI 绘图帮助（多页图片）", pattern=r"^/画图帮助\s*$")
    async def help(self, stream_id: str = "", **kwargs):
        await self._send_help_images(stream_id, workflow_only=False)
        return True, "显示绘图帮助", 1

    async def _send_help_images(self, stream_id: str, *, workflow_only: bool = False) -> None:
        """发送帮助图片；合并转发整条失败时对半拆，再不行就逐张发，最后才退回文字。

        NapCat 的转发资源上传会整条失败（实测 15 张 1080px 的页面报
        `上传转发消息资源失败：15/15`）；逐张直发走的是普通图片消息，不受这个限制。
        """
        try:
            from .workflow_help import help_images, workflow_help_images
            render = workflow_help_images if workflow_only else help_images
            pages = await asyncio.to_thread(render)
            if not pages:
                logger.warning("帮助图片渲染结果为空")
            elif len(pages) == 1:
                if await self._send_help_page(pages[0], stream_id):
                    return
                logger.warning("单页帮助图片未送达")
            else:
                delivered = await self._send_help_forward(pages, stream_id)
                if delivered < len(pages):
                    mid = len(pages) // 2
                    delivered = await self._send_help_forward(pages[:mid], stream_id)
                    if delivered == mid:
                        delivered += await self._send_help_forward(pages[mid:], stream_id)
                if delivered == len(pages):
                    return
                logger.warning("帮助图片合并转发未送达，改逐张发送")
                for page in pages[delivered:]:
                    if not await self._send_help_page(page, stream_id):
                        break
                else:
                    return
        except Exception as exc:
            logger.warning("帮助图片发送失败，退回文字：%s", _describe_error(exc))
        text = WORKFLOW_HELP if workflow_only else HELP + "\n" + WORKFLOW_HELP
        await self.ctx.send.text("帮助图片发送失败，以下为文字帮助：\n" + text, stream_id)

    @staticmethod
    def _sent(result: Any) -> bool:
        return result is True or (isinstance(result, dict) and result.get("success") is not False
                                  and result.get("sent", result.get("success")) is True)

    async def _send_help_page(self, page: str, stream_id: str) -> bool:
        return self._sent(await self.ctx.send.image(page, stream_id))

    async def _send_help_forward(self, pages: List[str], stream_id: str) -> int:
        """发一条合并转发，返回实际送达的页数；失败返回 0（整条不会部分送达）。"""
        if not pages:
            return 0
        account, nickname = await self._bot_identity()
        if not account:
            logger.warning("缺少机器人账号，无法合并发送帮助图片")
            return 0
        messages = [{"user_id": account, "nickname": nickname,
                     "segments": [{"type": "image", "content": data}]} for data in pages]
        result = await self.ctx.send.forward(messages, stream_id, timeout_ms=FORWARD_TIMEOUT_MS)
        return len(pages) if self._sent(result) else 0

    @staticmethod
    def _message_owner(message: Any) -> tuple[str, str, str, str]:
        """只使用 SDK 消息元数据绑定归属，拒绝从图片或文字推断身份。"""
        if not isinstance(message, dict):
            return "", "", "", ""
        info = message.get("message_info")
        info = info if isinstance(info, dict) else {}
        user, group = info.get("user_info"), info.get("group_info")
        user, group = user if isinstance(user, dict) else {}, group if isinstance(group, dict) else {}
        return (str(message.get("platform") or ""), str(message.get("session_id") or ""), str(user.get("user_id") or ""), str(group.get("group_id") or ""))

    def _check_style_access(self, user_id: str, group_id: str) -> None:
        cfg = self.config
        if not cfg.plugin.enabled or self._drawing_disabled():
            raise NovelAIError("绘图当前已关闭。")
        if not cfg.style.enabled:
            raise NovelAIError("画风参考命令当前已关闭。")
        if cfg.permission.mode == "whitelist" and user_id not in cfg.permission.user_ids and group_id not in cfg.permission.group_ids:
            raise NovelAIError("你或当前群不在绘图允许名单中。")

    def _prune_pending_styles(self) -> None:
        now = time.monotonic()
        self._pending_style = {key: value for key, value in self._pending_style.items() if value[1] > now}

    @Command("novelai_style", description="接收画风参考图后绘图", pattern=r"^/画图画风(?:\s+(?P<args>[\s\S]*))?$")
    async def set_style_reference(self, stream_id: str = "", user_id: str = "", group_id: str = "", platform: str = "", matched_groups=None, message: Any = None, **kwargs):
        try:
            self._validate_config()
            if message is not None:
                owner = self._message_owner(message)
                if any(given and str(given) != actual for given, actual in zip((platform, stream_id, user_id, group_id), owner)):
                    raise NovelAIError("消息归属不一致，无法绑定画风参考图。")
                platform, stream_id, user_id, group_id = owner
            user_id, group_id = str(user_id), str(group_id)
            if not platform or not stream_id or not user_id:
                raise NovelAIError("无法识别平台、聊天或发送者，不能绑定画风参考图。")
            self._check_style_access(user_id, group_id)
            args = (matched_groups or {}).get("args") or ""
            cfg = self.config
            req = parse_args(args, default_model=cfg.api.model, default_ratio=cfg.generation.default_ratio, default_steps=cfg.generation.default_steps, default_negative=cfg.generation.default_negative_prompt, max_images=cfg.generation.max_images, max_prompt_length=cfg.generation.max_prompt_length, model_aliases=cfg.api.model_aliases)
            if req.model != MODEL_ALIASES["普通"]:
                raise NovelAIError("/画图画风 仅支持 4.5 Full，请使用 --模型 普通。")
            self._prune_pending_styles()
            key = (platform, stream_id, user_id)
            if key not in self._pending_style and len(self._pending_style) >= cfg.style.max_pending_requests:
                raise NovelAIError("等待画风图的请求已满，请稍后再试。")
            self._pending_style[key] = (args, time.monotonic() + 300, group_id, time.time())
            await self.ctx.send.text("请在 5 分钟内，由你在当前聊天单独发送一张画风参考图（PNG/JPEG/WebP，最多 " + str(cfg.style.max_reference_mb) + " MiB）。默认仅参考画风；需要参考图人物时，请在提示词中明确写出。参考图会上传到 NovelAI，每张参考图每张生成图片额外 5 Anlas。再次发送命令会替换等待请求。", stream_id)
        except NovelAIError as exc:
            if stream_id:
                await self.ctx.send.text(str(exc), stream_id)
        except Exception as exc:
            logger.error("画风参考等待设置失败，异常类型：%s", type(exc).__name__)
            if stream_id:
                await self.ctx.send.text("无法设置画风参考，请管理员检查插件状态。", stream_id)
        return True, "画风参考命令已处理", 1

    @HookHandler("chat.receive.before_process", name="capture_novelai_style", description="接收同一用户在原聊天上传的画风图", mode=HookMode.BLOCKING, order=HookOrder.EARLY, timeout_ms=30_000, error_policy=ErrorPolicy.SKIP)
    async def capture_style_reference(self, message: Any, **kwargs) -> Dict[str, Any] | None:
        platform, stream_id, user_id, group_id = self._message_owner(message)
        key = (platform, stream_id, user_id)
        self._prune_pending_styles()
        pending = self._pending_style.get(key)
        if not pending or not all(key):
            return None
        segments = message.get("raw_message")
        if not isinstance(segments, list):
            return None
        images = [part for part in segments if isinstance(part, dict) and part.get("type") == "image"]
        if not images:
            return None
        consumed = {"action": "abort", "custom_result": {"reason": "NovelAI 画风参考图已处理"}}
        try:
            self._validate_config()
            self._check_style_access(user_id, group_id)
            if group_id != pending[2]:
                raise NovelAIError("图片群归属与原请求不一致，请重新发送画风命令。")
            try:
                timestamp = float(message.get("timestamp", ""))
            except (TypeError, ValueError):
                raise NovelAIError("图片消息时间元数据无效，请重新上传。") from None
            if not math.isfinite(timestamp) or timestamp < pending[3] - 1 or timestamp > time.time() + 30:
                raise NovelAIError("请发送本次命令之后的新图片，不能使用旧消息。")
            if len(images) != 1 or any(not isinstance(part, dict) or part.get("type") not in ("image", "text") or (part.get("type") == "text" and str(part.get("text") or "").strip()) for part in segments):
                raise NovelAIError("请单独发送一张参考图，不要附带文字或其他图片。")
            encoded = images[0].get("binary_data_base64")
            if not isinstance(encoded, str) or not encoded:
                raise NovelAIError("没有收到可读取的图片数据，请重新上传图片；不接受图片链接或文件路径。")
            cfg = self.config
            if len(self._style_tasks) >= cfg.limits.max_concurrent_jobs:
                raise NovelAIError("绘图服务忙碌，请稍后重新上传画风图。")
            prepared = await asyncio.to_thread(prepare_uploaded_reference, encoded, cfg.style.max_reference_mb * 1024 * 1024)
            # 图片读取期间关闭服务、替换请求或超过五分钟都不能提交旧请求。
            self._check_style_access(user_id, group_id)
            if self._pending_style.get(key) != pending or time.monotonic() >= pending[1]:
                raise NovelAIError("画风等待请求已过期或被替换，请重新发送 /画图画风。")
            if len(self._style_tasks) >= cfg.limits.max_concurrent_jobs:
                raise NovelAIError("绘图服务忙碌，请稍后重新上传画风图。")
            self._pending_style.pop(key, None)
            task = asyncio.create_task(self.draw(stream_id=stream_id, user_id=user_id, group_id=group_id, platform=platform, matched_groups={"args": pending[0]}, style_reference=prepared))
            self._style_tasks.add(task)
            task.add_done_callback(self._style_task_done)
        except NovelAIError as exc:
            await self.ctx.send.text(str(exc), stream_id)
        except Exception as exc:
            logger.error("画风参考图处理失败，异常类型：%s", type(exc).__name__)
            await self.ctx.send.text("画风图片处理失败，请重新上传或联系管理员。", stream_id)
        return consumed

    def _style_task_done(self, task: asyncio.Task) -> None:
        self._style_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("画风绘图后台任务失败，异常类型：%s", type(task.exception()).__name__)

    @Command("novelai_draw", description="NovelAI 文生图；画小图走 5 Full 的 Small 画布，不计 Anlas", pattern=r"^/(?P<kind>高清画图|画小图|画图)(?:\s+(?P<args>[\s\S]*))?$", timeout_ms=300_000)
    async def draw(self, stream_id: str = "", user_id: str = "", group_id: str = "", platform: str = "", matched_groups=None, **kwargs):
        reserved = False
        workflow_owner = None
        user_id, group_id = str(user_id), str(group_id)
        gate_key = platform + ":" + user_id if user_id and platform else ""
        try:
            self._validate_config()
            cfg = self.config
            if not cfg.plugin.enabled:
                raise NovelAIError("绘图插件当前未启用。")
            if self._drawing_disabled():
                raise NovelAIError("绘图当前已关闭，请管理员发送 /打开画图。")
            if kwargs.get("style_reference"):
                self._check_style_access(user_id, group_id)
            groups = matched_groups or {}
            # /画小图 也是 5 Full，但锁在 Small 画布上——Opus 的无限生成只覆盖这一档，不走 Anlas。
            small = groups.get("kind") == "画小图"
            hd = small or groups.get("kind") == "高清画图"
            if cfg.permission.mode == "whitelist" and user_id not in cfg.permission.user_ids and group_id not in cfg.permission.group_ids:
                raise NovelAIError("你或当前群不在绘图允许名单中。")
            req = parse_args(groups.get("args") or "", default_model=cfg.api.hd_model if hd else cfg.api.model, default_ratio=cfg.generation.default_ratio, default_steps=cfg.generation.default_steps, default_negative=cfg.generation.default_negative_prompt, max_images=cfg.generation.max_images, max_prompt_length=cfg.generation.max_prompt_length, model_aliases=cfg.api.model_aliases, size_presets=SMALL_SIZE_PRESETS if small else None)
            if not resolve_api_token():
                raise NovelAIError("未配置 NOVELAI_API_TOKEN，请管理员设置环境变量并重启 MaiBot。")
            workflow_owner = self._workflow_owner(platform, stream_id, user_id)
            if workflow_owner in self._workflow_tasks:
                raise NovelAIError("当前绘图任务正在处理中，请等待或 /画图取消。")
            settings = (cfg.limits.max_concurrent_jobs, cfg.limits.cooldown_seconds)
            if self._gate is None or (self._gate_settings != settings and not self._gate.active):
                self._gate, self._gate_settings = JobGate(*settings), settings
            # 5 Full 一张就要跑很久，按张数累计冷却，免得连着发把并发位和限流都撞上。
            # /画小图 豁免：那一档画布不计 Anlas，卡冷却只是让人白等，只走每用户基础冷却。
            heavy = not small and req.model in (cfg.api.hd_model, MODEL_ALIASES["高清"])
            self._gate.reserve(gate_key, cfg.limits.hd_cooldown_seconds * req.count if heavy else None)
            gate = self._gate
            reserved = True
            self._workflow_tasks[workflow_owner] = asyncio.current_task()
            await self.ctx.send.text("🎨 已收到请求，正在准备绘图……", stream_id)
            async with gate.semaphore:
                async with asyncio.timeout(cfg.api.timeout_seconds + (cfg.translation.timeout_seconds if cfg.translation.enabled else 0)):
                    req, payload, role_note, _, _, _ = await self._plan_request(req, user_id, platform, kwargs.get("style_reference"))
                    await self.ctx.send.text(role_note, stream_id)
                    images = await self._client().generate(payload)
            workflow_owner = self._workflow_owner(platform, stream_id, user_id)
            job_ids = await self._workflow_record(workflow_owner, payload, images, "普通绘图", payload.style if isinstance(payload, WorkflowPayload) else None)
            # 按群号开折叠：名单内的群不管画什么都折成一条合并转发，其余群和私聊逐张直发。
            collapsed = False
            listed_group_delivery_failed = False
            if group_id and group_id in cfg.generation.collapse_groups:
                collapsed = await self._send_collapsed(images, req, stream_id)
                if collapsed:
                    await self.ctx.send.text(f"已折叠为合并转发（{len(images)} 张）。", stream_id)
                else:
                    listed_group_delivery_failed = True
                    try:
                        pending_path = await asyncio.to_thread(
                            self._save_pending_delivery, images, stream_id)
                    except Exception as exc:
                        logger.exception("合并转发失败且原图保存失败，异常类型：%s", type(exc).__name__)
                        await self.ctx.send.text(
                            "图片已生成，但合并转发发送失败，原图自动保存也失败了；请管理员立即查看插件日志。",
                            stream_id)
                    else:
                        logger.error("合并转发失败，原图已保存待处理：%s", pending_path)
                        await self.ctx.send.text(
                            f"图片已生成，但合并转发发送失败，原图已保留（任务号：{pending_path.name}）。请联系管理员补发。",
                            stream_id)
            else:
                for index, image in enumerate(images):
                    try:
                        sent = await self.ctx.send.image(image["image"], stream_id, return_details=True)
                        if sent is not True and not (isinstance(sent, dict) and sent.get("sent") is True):
                            raise NovelAIError("图片生成成功，但聊天图片发送失败。")
                        if isinstance(sent, dict):
                            message_id = str(sent.get("message_id") or sent.get("id") or "")
                            if message_id:
                                self._workflow_replies[(workflow_owner, message_id)] = job_ids[index]
                    except NovelAIError:
                        raise
                    except Exception:
                        raise NovelAIError("图片生成成功，但聊天图片发送失败。") from None
            if not listed_group_delivery_failed:
                await self.ctx.send.text(f"生成完成\n模型：{req.model}\n尺寸：{req.width}×{req.height}\n种子：" + "、".join(str(image["seed"]) for image in images), stream_id)
            return True, "图片生成完成", 1
        except TimeoutError:
            await self.ctx.send.text("绘图请求总超时，请稍后再试。", stream_id)
        except NovelAIError as exc:
            await self.ctx.send.text(str(exc), stream_id)
        except Exception as exc:
            # 仅记录类型；SDK/网络异常可能含凭证、消息内容或图片数据。
            logger.error("绘图处理失败，异常类型：%s", type(exc).__name__)
            await self.ctx.send.text("绘图处理失败，请管理员检查插件状态。", stream_id)
        finally:
            if workflow_owner is not None and self._workflow_tasks.get(workflow_owner) is asyncio.current_task():
                self._workflow_tasks.pop(workflow_owner, None)
            if reserved:
                gate.release(gate_key)
        return True, "绘图请求已处理", 1



    async def _bot_identity(self) -> tuple[str, str]:
        """合并转发节点要挂机器人自己的账号与昵称。取不到账号就放弃折叠并保存原图待处理。"""
        account, nickname = "", ""
        try:
            account = str(await self.ctx.config.get("bot.qq_account", "") or "").strip()
            nickname = str(await self.ctx.config.get("bot.nickname", "") or "").strip()
        except Exception:
            return "", ""
        return account, nickname or "麦麦"

    async def _send_collapsed(self, images, req, stream_id: str) -> bool:
        """折叠群只尝试合并转发；明确拒绝时用小图再试一次，其他失败不重发。"""
        account, nickname = await self._bot_identity()
        if not account:
            return False
        for attempt, compact in ((1, False), (2, True)):
            payloads = await asyncio.to_thread(
                lambda compact=compact: [shrink_for_forward(image["image"], compact=compact)
                                         for image in images])
            messages = [{"user_id": account, "nickname": nickname,
                         "segments": [{"type": "image", "content": data}]} for data in payloads]
            try:
                result = await self.ctx.send.forward(messages, stream_id, timeout_ms=FORWARD_TIMEOUT_MS)
            except Exception as exc:
                detail = _describe_error(exc)
                logger.warning("合并转发第 %d 次失败：图 %s，%s", attempt,
                               "、".join(f"{len(d)/1048576:.2f} MiB" for d in payloads), detail)
                # UploadForwardMsgV2 failures are definitive rejections. A timeout or
                # transport disconnect may have delivered, so never retry those.
                definitive = any(marker in detail.lower() for marker in (
                    "uploadforwardmsgv2", "上传转发消息资源失败", "retcode=1200"))
                if attempt == 1 and definitive:
                    continue
                return False
            sent = result is True or (isinstance(result, dict) and result.get("success") is not False
                                      and result.get("sent", result.get("success")) is True)
            if sent:
                return True
            logger.warning("合并转发第 %d 次返回未送达：图 %s", attempt,
                           "、".join(f"{len(d)/1048576:.2f} MiB" for d in payloads))
            if attempt == 1 and result is not None and (result is False or
                    (isinstance(result, dict) and (result.get("success") is False or result.get("sent") is False))):
                continue
            return False
        return False

    def _save_pending_delivery(self, images, stream_id: str) -> Path:
        """保存转发失败的原始生成图和可供管理员恢复的索引。"""
        job_id = uuid.uuid4().hex
        directory = Path(self.ctx.paths.data_dir) / "pending_deliveries" / job_id
        directory.mkdir(parents=True, exist_ok=False)
        records = []
        for index, image in enumerate(images, start=1):
            encoded = image["image"]
            raw = base64.b64decode(encoded, validate=True)
            try:
                with Image.open(BytesIO(raw)) as source:
                    extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(source.format, ".bin")
            except Exception:
                extension = ".bin"
            seed = image.get("seed")
            safe_seed = str(seed) if isinstance(seed, int) and not isinstance(seed, bool) else "unknown"
            filename = f"image-{index:02d}-seed-{safe_seed}{extension}"
            (directory / filename).write_bytes(raw)
            records.append({"seed": seed, "filename": filename})
        metadata = {"job_id": job_id, "stream_id": stream_id, "images": records}
        temporary = directory / "metadata.json.tmp"
        temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(directory / "metadata.json")
        return directory

    @Command(
        "style_list",
        description="查看所有画风：合并转发内先列表格，再逐档发画风名与两个模型的参考图；画师档收进嵌套转发，每个画师再各嵌一层",
        pattern=r"^/(?:查看画风|画风列表|style.?list)\s*$",
    )
    async def style_list(self, stream_id: str = "", **kwargs: Any):
        if not stream_id:
            return False, "无法获取聊天流信息", 0
        try:
            media, artists = preview_groups()
            cfg = self.config
            # 每档并排给两个模型的观感：4.5 那套是生成时真正用的参考图，5 那套只用于比对。
            by_model = []
            for model, v5 in ((cfg.api.model, False), (cfg.api.hd_model, True)):
                entries = preview_groups(v5=v5)
                previews = await asyncio.to_thread(style_preview, [*entries[0], *entries[1]])
                by_model.append((model, dict(previews)))
            if not any(by_label for _, by_label in by_model):
                raise NovelAIError("没有可用的画风参考图。")
            # 节点里放哪些档，以 4.5 那套（生成时真正用的）为准。
            first_by = by_model[0][1]

            def models_of(label: str) -> List[Tuple[str, str]]:
                return [(model, by_label[label]) for model, by_label in by_model if label in by_label]

            def flat_node(label: str) -> dict:
                """外层节点：宿主会归一化，照 SDK 那套 content / nickname 写即可。"""
                segments: List[Dict[str, Any]] = [{"type": "text", "content": label}]
                for model, data in models_of(label):
                    segments += [{"type": "text", "content": model_tag(model)},
                                 {"type": "image", "content": data}]
                return {"user_id": account, "nickname": label, "segments": segments}

            def artist_node(label: str) -> dict:
                """画师档的单个画师。

                宿主只归一化最外层节点，这层起原样透传，所以段必须直接写成适配器读的
                形状：文字用 data、图片用 binary_data_base64、节点名用 user_nickname。
                照外层那套写会变成一串空文本加 [image]，名字也全掉成「QQ用户」。
                """
                segments: List[Dict[str, Any]] = [{"type": "text", "data": label}]
                for model, data in models_of(label):
                    segments += [{"type": "text", "data": model_tag(model)},
                                 {"type": "image", "binary_data_base64": data, "data": ""}]
                return {"user_id": account, "user_nickname": label, "content": segments}

            def cost(label: str) -> int:
                """这一档两个模型预览图的 base64 合计，用来按体积分批。"""
                return sum(len(data) for _, data in models_of(label))

            def chunk_by_cost(labels: List[str], budget: int) -> List[List[str]]:
                """按预算把档分组：一条转发里塞太多图，NapCat 上传就会整条失败。"""
                groups: List[List[str]] = []
                current: List[str] = []
                used = 0
                for label in labels:
                    size = cost(label)
                    if current and used + size > budget:
                        groups.append(current)
                        current, used = [], 0
                    current.append(label)
                    used += size
                if current:
                    groups.append(current)
                return groups

            account, _ = await self._bot_identity()
            if account:
                sent = 0
                head = [{"user_id": account, "nickname": "画风总览",
                         "segments": [{"type": "text", "content": style_table(media, artists)}]}]
                head += [flat_node(label) for label, _ in media if label in first_by]
                artist_labels = [label for label, _ in artists if label in first_by]
                plan = [head]
                for batch in chunk_by_cost(artist_labels, SHEET_MAX_BASE64):
                    inner = [{"user_id": account, "user_nickname": label,
                              "content": [{"type": "forward", "data": [artist_node(label)]}]}
                             for label in batch]
                    plan.append([{"user_id": account, "nickname": f"画师档（{len(inner)} 款）",
                                  "segments": [{"type": "text", "content": f"画师档 · {len(inner)} 款"},
                                               {"type": "forward", "data": inner}]}])
                for messages in plan:
                    try:
                        result = await self.ctx.send.forward(messages, stream_id, timeout_ms=120_000)
                        if result is True or (isinstance(result, dict) and result.get("success") is not False
                                              and result.get("sent", result.get("success")) is True):
                            sent += 1
                    except Exception as exc:
                        logger.warning("画风列表第 %d 条合并转发失败：%s", sent + 1, _describe_error(exc))
                if sent == len(plan):
                    return True, "画风列表已发送", 1
                logger.warning("画风列表合并转发只成功 %d/%d 条，其余改为逐条发送", sent, len(plan))
            await self.ctx.send.text(f"画风列表（{len(first_by)} 档，合并转发不可用，逐条发送）", stream_id)
            # 逐条发也要把两个模型都发全：只发第一套会让人以为 v5 那套不存在。
            for label in first_by:
                await self.ctx.send.text(label, stream_id)
                for model, by_label in by_model:
                    if label not in by_label:
                        continue
                    await self.ctx.send.text(model_tag(model), stream_id)
                    await self.ctx.send.image(by_label[label], stream_id)
            return True, "画风列表已发送", 1
        except NovelAIError as exc:
            await self.ctx.send.text(str(exc), stream_id)
            return True, "异常", 0
        except Exception as exc:
            logger.error("画风列表异常：%s", _describe_error(exc))
            await self.ctx.send.text("画风列表发送失败，请管理员检查插件日志。", stream_id)
            return True, "异常", 0

    @Command(
        "style_artist",
        description="查看某位画师的两套参考图：/查看画师 <画师名>，画师名支持模糊写法",
        pattern=r"^/(?:查看画师|画师参考)\s+(?P<name>[\s\S]+?)\s*$",
    )
    async def style_artist(self, stream_id: str = "", **kwargs: Any):
        if not stream_id:
            return False, "无法获取聊天流信息", 0
        name = str((kwargs.get("matched_groups") or {}).get("name") or "").strip()
        try:
            preset = match_artist(name)
            if preset is None:
                await self.ctx.send.text(f"没找到画师「{name}」。发送 /查看画风 可以看到全部画师档。", stream_id)
                return True, "未找到画师", 0
            # 这里发原图：对照表是缩过的，要看细节得看这一条。
            images: List[Tuple[str, str]] = []
            for model, path in ((self.config.api.model, preset.reference_path),
                                (self.config.api.hd_model, preset.reference_v5_path)):
                if not path.is_file():
                    continue
                data = await asyncio.to_thread(
                    lambda p=path: base64.b64encode(p.read_bytes()).decode("ascii"))
                images.append((model, data))
            if not images:
                raise NovelAIError(f"画师「{preset.label}」的参考图缺失，请管理员检查素材目录。")
            account, _ = await self._bot_identity()
            head = f"{preset.label}（{preset.key}）\n点名方式：在提示词里写「{preset.keywords[0]}」。"
            if account and len(images) > 1:
                messages = [{"user_id": account, "nickname": "画师参考", "segments": [
                    {"type": "text", "content": head}]}]
                messages += [{"user_id": account, "nickname": f"{preset.label} · {model_tag(model)}",
                              "segments": [{"type": "text", "content": f"{model_tag(model)} · {model}"},
                                           {"type": "image", "content": data}]}
                             for model, data in images]
                try:
                    result = await self.ctx.send.forward(messages, stream_id, timeout_ms=120_000)
                    if result is True or (isinstance(result, dict) and result.get("success") is not False
                                          and result.get("sent", result.get("success")) is True):
                        return True, "画师参考已发送", 1
                except Exception as exc:
                    logger.warning("画师参考合并转发失败，改为逐张发送：%s", _describe_error(exc))
            await self.ctx.send.text(head, stream_id)
            for model, data in images:
                await self.ctx.send.text(f"{model_tag(model)} · {model}", stream_id)
                await self.ctx.send.image(data, stream_id)
            return True, "画师参考已发送", 1
        except NovelAIError as exc:
            await self.ctx.send.text(str(exc), stream_id)
            return True, "异常", 0
        except Exception as exc:
            logger.error("画师参考异常：%s", _describe_error(exc))
            await self.ctx.send.text("画师参考发送失败，请管理员检查插件日志。", stream_id)
            return True, "异常", 0

    @Command(
        "style_preview",
        description="发送全部画风对照表：两张完整拼接图逐条直发，图顶标明 4.5 与 5 模型",
        pattern=r"^/(?:画风预览|画风对照表|style.?sheet)\s*$",
    )
    async def style_sheet(self, stream_id: str = "", **kwargs: Any):
        if not stream_id:
            return False, "无法获取聊天流信息", 0
        try:
            cfg = self.config
            sheets: List[Tuple[str, str]] = []
            style = cfg.style
            for model, v5 in ((cfg.api.model, False), (cfg.api.hd_model, True)):
                # 整张不拆：拆分会在张与张之间断掉对照关系。两张各自是一条消息。
                sheet = await asyncio.to_thread(
                    style_sheet_image, preview_entries(v5=v5), model,
                    style.sheet_columns, style.sheet_cell_width, style.sheet_quality)
                if sheet:
                    sheets.append((model, sheet))
                    logger.info("对照表 %s 生成完成：base64 %.2f MiB", model, len(sheet) / 1048576)
            if not sheets:
                raise NovelAIError("没有可用的画风参考图。")
            # 逐条直发，不走合并转发：模型标注已经画在图顶，不需要再包一层。
            for model, data in sheets:
                sent = await self.ctx.send.image(data, stream_id)
                if sent is not True and not (isinstance(sent, dict) and sent.get("sent") is True):
                    raise NovelAIError("画风对照表发送失败。")
            return True, "画风对照表已发送", 1
        except NovelAIError as exc:
            await self.ctx.send.text(str(exc), stream_id)
            return True, "异常", 0
        except Exception as exc:
            logger.error("画风对照表异常：%s", _describe_error(exc))
            await self.ctx.send.text("画风对照表生成失败，请管理员检查插件日志。", stream_id)
            return True, "异常", 0


def create_plugin():
    return NovelAIImagePlugin()
