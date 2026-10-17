"""默认柔光档的画风边界；历史翻译协议、预算与源码快照独立保留。"""

from typing import Any, Dict, List
import re

from .legacy_default_nanogpt import NanoGPTTranslator

# 原始甘雨和回滚甘雨的真实云译文共有项，并非重新设计静态 44 标签。
FIXED_STYLE_TAIL = ("anime style", "cel shading", "soft_lighting", "detailed")
_QUALITY = {"masterpiece", "best quality", "absurdres", "very aesthetic", "newest"}
V5_DEFAULT_MULTI_QUALITY = ("masterpiece", "best_quality", "absurdres", "very aesthetic", "newest")
# 赛璐璐是默认柔光配方里唯一强制平面媒介的标签；其余 44 个词（soft shading、
# gradient shading、soft focus、depth of field、blurry background）对 3D 渲染同样成立，
# 所以它们压不住官方 3D 观感强的角色，只会叠在 3D 底子上。
# 单人 0.5 是按伊蕾娜同 seed 验收定的；多人时角色栏各带强先验，0.5 压不住。
# 实测 1.0 会硬成平涂、丢掉柔光，故压到 0.7。
MULTI_CEL_WEIGHT = 0.7
# 多人默认柔光放进角色栏的画风核心：从 44 标签里挑出真正决定媒介的那几个。
# 整段塞进角色栏会占满 V5 共用的注意力预算，把后面的动作标签挤没（插入、自慰
# 实测完全丢失）；核心超过约 16 个标签时，构图还会把边缘角色挤成前景大特写。
# 这一段是实测里唯一同时保住动作、构图和柔光观感的长度。
MULTI_STYLE_CORE = (
    "soft shading", "airbrushed shading", "gentle shading", "blended shading",
    "soft edges", "blurred lineart", "soft lineart", "soft focus", "anime style", "2d",
)


def multichar_style_core() -> str:
    return f"1::{', '.join(MULTI_STYLE_CORE)}::, {MULTI_CEL_WEIGHT:g}::cel shading::"


# 普通档 default_negative_tags 里已有的反写实词。高清档的 hd_default_negative_tags
# 只有 dramatic lighting / harsh shadows，这组词在 HD 路径上一直缺失——单人靠
# HD_ELAINA_LOCKED_NEGATIVES 补了两个词，多人则完全没有。负面不占正向注意力预算，
# 补进来不会像画风标签那样挤掉动作。
MULTI_REALISM_NEGATIVES = (
    "realistic skin", "photorealistic", "hyperrealistic", "3d", "3d render",
    "skin pores", "oily skin", "wax figure", "doll",
)
MULTI_REALISM_WEIGHT = 0.9


def multichar_realism_negative() -> str:
    return f"{MULTI_REALISM_WEIGHT:g}::{', '.join(MULTI_REALISM_NEGATIVES)}::"
_STYLE = re.compile(
    r"\b(?:style|shading|lineart|rendering|palette|brushwork|brushstrokes|dithering|"
    r"watercolou?r|painterly|photorealistic|photorealism|realism|realistic|impasto|render|cgi|"
    r"monochrome|greyscale|grayscale|halftone|screentone|screencap|gacha|"
    r"saturation|saturated|desaturated|lighting|illumination|shadows?|gradients?|"
    r"glossy|gloss|shiny|porcelain|silky|specular|highlights?|bokeh)\b|"
    r"\b(?:detailed|intricate|fine) (?:skin|face|eyes?|iris|irises|hair|anatomy)\b|"
    r"\b(?:skin|iris|hair) (?:texture|pores?)\b|\beyelashes\b|"
    r"\b(?:smooth|detailed|intricate|fine|beautiful|textured|polished|luminous|glowing)\b.*"
    r"\b(?:skin|face|eyes?|iris|irises|hair)\b|"
    r"\b(?:sharp|soft|clean|crisp|thick|thin|delicate) (?:lines|edges|outlines|focus)\b|"
    r"\b(?:colors?|tones?|medium|media)\b$|\b(?:depth of field|subsurface scattering)\b|"
    r"^(?:artist:|by |anime$|2d$|3d$|cartoon$|chibi$|official art$|key visual$|"
    r"pixel art$|concept art$|digital (?:art|painting|illustration)$|traditional art$|oil painting$|"
    r"cinematic composition$|film grain$|.* (?:paper|canvas) texture$)", re.I,
)

# 每个映射只授权该词对应的显式概念，眼睛高光不能解禁皮肤高光。
_EXPLICIT = {
    "anime style": r"二次元|动漫(?:画风|风格)", "cel shading": r"赛璐璐|平涂",
    "soft lighting": r"柔光|柔和.{0,4}(?:光|照明)", "detailed": r"精细|细致|详细",
    "watercolor": r"水彩", "watercolour": r"水彩", "oil painting": r"油画",
    "digital painting": r"数字绘画|数码绘画", "digital illustration": r"数字插画",
    "photorealistic": r"照片级写实|照片写实", "realistic": r"写实",
    "rim lighting": r"轮廓光", "backlighting": r"逆光", "cinematic lighting": r"电影(?:感)?光照|电影布光",
    "depth of field": r"景深", "bokeh": r"散景|焦外光斑",
    "soft shadows": r"柔和.{0,3}阴影", "detailed eyes": r"(?:精细|细致|详细).{0,4}眼睛",
    "detailed hair": r"(?:精细|细致|详细).{0,4}(?:头发|发丝)",
    "detailed skin": r"(?:精细|细致|详细).{0,4}(?:皮肤|肌肤)",
    "detailed face": r"(?:精细|细致|详细).{0,4}(?:脸|面部|五官)",
    "skin texture": r"(?:皮肤|肌肤).{0,3}(?:纹理|肌理)", "eyelashes": r"睫毛",
    "glossy skin": r"(?:光泽|油亮|亮泽).{0,3}(?:皮肤|肌肤)|(?:皮肤|肌肤).{0,3}(?:光泽|油亮|亮泽)",
    "glossy hair": r"(?:光泽|油亮|亮泽).{0,3}(?:头发|发丝)|(?:头发|发丝).{0,3}(?:光泽|油亮|亮泽)",
    "eye highlights": r"眼睛.{0,3}高光|高光.{0,3}眼睛",
    "hair highlights": r"(?:头发|发丝).{0,3}高光|高光.{0,3}(?:头发|发丝)",
    "skin highlights": r"(?:皮肤|肌肤).{0,3}高光|高光.{0,3}(?:皮肤|肌肤)",
}
_NEGATION = re.compile(r"不要|不许|禁止|避免|没有|不用|无|\b(?:no|not|without|avoid)\b", re.I)
_DETAIL_CONCEPTS = {
    "detailed": r"精细|细致|详细|精致", "beautiful": r"美丽|漂亮|精美",
    "smooth": r"光滑|平滑", "glossy": r"光泽|油亮|亮泽", "silky": r"丝绸|丝滑",
    "highlights": r"高光", "specular": r"镜面|镜面反射", "texture": r"纹理|肌理",
    "skin": r"皮肤|肌肤", "face": r"脸|面部|五官", "eyes": r"眼睛|眼部",
    "eye": r"眼睛|眼部", "iris": r"虹膜", "hair": r"头发|发丝",
    "dress": r"连衣裙|裙子", "clothes": r"衣服|衣物", "fabric": r"布料|面料",
}
_POLICY = (
    "\nDEFAULT SOFT-LIGHT PROFILE OVERRIDE: The application fixes art style separately. "
    "Override the earlier default-style expansion instruction: expand only characters, clothing, actions, "
    "pose, gaze, expression, camera angle, framing and concrete scene content. "
    "Do not invent artists, art styles, media, lighting styles, palette or skin/face/eye/hair rendering details. "
    "Translate and preserve such details when explicitly requested, including explicit exclusions. "
    "Keep the original tag budget, quality tags and response format."
)


def _canonical(tag: str) -> str:
    return re.sub(r"^[\d.]+::|::$", "", tag.strip()).replace("_", " ").casefold()


def _requested(tag: str, source: str) -> bool:
    normalized = _canonical(tag)
    source = source.replace("_", " ").casefold()
    if re.search(r"(?<![\w:])" + re.escape(normalized) + r"(?!\w)", source):
        return True
    parts = re.split(r"[,，。;；!！\n]|以及|并且|和|与|及", source)
    if normalized in _EXPLICIT and any(re.search(_EXPLICIT[normalized], part, re.I) for part in parts):
        return True
    words = [word for word in normalized.split() if word not in ("on", "of", "and", "with")]
    return bool(words) and any(all(word in _DETAIL_CONCEPTS and re.search(_DETAIL_CONCEPTS[word], part)
                                  for word in words) for part in parts)


def normalize_default_style(tags: List[str], source: str) -> List[str]:
    """只限制默认档云端画风；不改场景内容、静态配方或负面。"""
    clauses = re.split(r"[,，。;；!！\n]|\bbut\b|但是|但", source)
    affirmative = ", ".join(clause for clause in clauses if not _NEGATION.search(clause))
    negative = ", ".join(clause for clause in clauses if _NEGATION.search(clause))
    fixed = [tag for tag in FIXED_STYLE_TAIL if not _requested(tag, negative)]
    fixed_names = {_canonical(tag) for tag in FIXED_STYLE_TAIL}
    quality, content = [], []
    seen = set()
    for tag in tags:
        normalized = _canonical(tag)
        if normalized in fixed_names or normalized in seen:
            continue
        if _STYLE.search(normalized) and not _requested(tag, affirmative):
            continue
        seen.add(normalized)
        (quality if normalized in _QUALITY else content).append(tag)
    # 历史链已经截断云标签；固定画风属于额外开销，不能再次挤掉已保留的用户细节。
    return quality + fixed + content


def normalize_default_multichar_style(tags: List[str], source: str) -> List[str]:
    """让结构化多人翻译沿用已验收 V5 默认配方的质量词与赛璐璐权重。"""
    normalized = normalize_default_style(tags, source)
    result = []
    seen = set()
    supplied_quality = []
    for tag in normalized:
        key = _canonical(tag)
        if key in _QUALITY:
            if key not in {_canonical(item) for item in supplied_quality}:
                supplied_quality.append(tag)
                seen.add(key)
            continue
        if key == "cel shading":
            tag = f"{MULTI_CEL_WEIGHT:g}::cel shading::"
        if key not in seen:
            result.append(tag)
            seen.add(key)
    standard_quality = list(V5_DEFAULT_MULTI_QUALITY)
    standard_keys = {_canonical(tag) for tag in standard_quality}
    extra_quality = [tag for tag in supplied_quality if _canonical(tag) not in standard_keys]
    return standard_quality + extra_quality + result


class DefaultStyleTranslator(NanoGPTTranslator):
    """继承历史请求实现，仅追加默认画风边界并确定性整理正向结果。"""

    async def _post(self, payload: Dict[str, Any], token: str) -> Dict[str, Any]:
        payload = {**payload, "messages": [dict(message) for message in payload["messages"]]}
        payload["messages"][0]["content"] += _POLICY
        return await super()._post(payload, token)

    async def translate_many(self, prompts: Dict[str, str]) -> Dict[str, str]:
        result = await super().translate_many(prompts)
        for key in ("positive", "prompt"):
            if key in result:
                result[key] = ", ".join(normalize_default_style(result[key].split(", "), prompts[key]))
        return result
