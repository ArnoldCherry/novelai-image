"""中文绘图术语到英文 Danbooru 风格标签的固定映射。

小模型（1.7B 及以下）对成人向术语和一词多义的中文词容易误译或凭空编造，
实测例如把「露点」当作气象学的 dew point、把「乳头」翻成无关词、
把「无修正」拆成 no correction。这些词命中即用固定标签，不经过模型。

只收录身体、性行为、姿势、服装、视角构图、体型六类绘图术语，不收录任何
指向未成年长相或年龄的词。

视角构图这一类（`正对镜头` → looking at viewer 等）是专门加进来的：小模型
会把「朝正面朝向镜头」翻成 `facing the camera frontally` 这类散文句子，
NovelAI 对句子的构图控制力远弱于 Danbooru 标签，实测会导致完全不朝镜头。
"""

from typing import Dict, List, Tuple
import re

GLOSSARY: Dict[str, str] = {
    # 容易被小模型省略的日常动作与精确服装名；动作不能退化为完成后的发型。
    "正在扎高马尾": "tying hair, high ponytail, arms up",
    "扎高马尾": "tying hair, high ponytail, arms up",
    "正在扎马尾": "tying hair, ponytail, arms up",
    "扎马尾": "tying hair, ponytail, arms up",
    "死库水": "school swimsuit, one-piece swimsuit",
    "连体泳衣": "one-piece swimsuit",
    # 裸露与身体
    "无修正": "uncensored",
    "露点": "exposed nipples",
    "乳头": "nipples",
    "全裸": "completely nude",
    "裸体": "nude",
    "半裸": "half-nude",
    "裸露": "exposed",
    "乳房": "breasts",
    "巨乳": "large breasts",
    "贫乳": "flat chest",
    "臀部": "ass",
    "大腿": "thighs",
    "阴茎": "penis",
    "阴道": "vagina",
    # 性行为
    "性交": "sex",
    "性爱": "sex",
    "做爱": "sex",
    "口交": "fellatio",
    "插入": "insertion",
    "内射": "creampie",
    "精液": "cum",
    "高潮": "orgasm",
    "自慰": "masturbation",
    "潮吹": "squirting",
    "淫乱": "lewd",
    "色情": "lewd",
    # 姿势
    "骑乘位": "girl on top",
    "后入": "doggystyle",
    "张开双腿": "spread legs",
    "拘束": "bondage",
    "捆绑": "bondage",
    # 服装
    "黑丝": "black thighhighs",
    "白丝": "white thighhighs",
    "丝袜": "thighhighs",
    "女仆装": "maid",
    "水手服": "serafuku",
    "校服": "school uniform",
    "制服": "uniform",
    "内衣": "underwear",
    "内裤": "panties",
    # 视角与构图（决定「有没有正对镜头」这类构图，必须走固定标签）
    "正对镜头": "looking at viewer, facing viewer",
    "正面朝向": "looking at viewer, facing viewer",
    "正视前方": "looking at viewer, facing viewer",
    "朝向镜头": "looking at viewer",
    "面对镜头": "looking at viewer",
    "面向镜头": "looking at viewer",
    "看着镜头": "looking at viewer",
    "看向镜头": "looking at viewer",
    "看镜头": "looking at viewer",
    "背对镜头": "facing away",
    "侧身": "from side",
    # 姿势
    "蹲在地上": "squatting",
    "蹲下来": "squatting",
    "蹲着": "squatting",
    "蹲下": "squatting",
    "跪在地上": "kneeling",
    "跪着": "kneeling",
    "打开双腿": "spread legs",
    "分开双腿": "spread legs",
    "双腿张开": "spread legs",
    "双腿打开": "spread legs",
    "打开腿": "spread legs",
    "张开腿": "spread legs",
    # 体型
    "大屁股": "large ass",
    "翘臀": "large ass",
    "丰臀": "large ass",
    "屁股": "ass",
    "大胸": "large breasts",
    "巨胸": "huge breasts",
    "胸部": "breasts",
    # 隐私部位
    "阴部": "pussy",
    "私处": "pussy",
    "下体": "pussy",
    "小穴": "pussy",
    # 裸露变体
    "脱光衣服": "completely nude",
    "一丝不挂": "completely nude",
    "脱光": "completely nude",
    "赤裸": "nude",
    "光着身子": "nude",
    # 表情（torogao 是 Danbooru 上「快感/失神表情」的规范标签）
    "表情享受": "torogao",
    "享受的表情": "torogao",
    "享受": "torogao",
    # 人物计数（Danbooru 标准标签）
    "少女": "1girl",
    "少年": "1boy",
}

_BY_LENGTH_DESC: List[Tuple[str, str]] = sorted(GLOSSARY.items(), key=lambda item: -len(item[0]))
_PATTERN = re.compile("|".join(re.escape(chinese) for chinese, _ in _BY_LENGTH_DESC))
_WHITESPACE = re.compile(r"\s+")
_EDGE_PUNCTUATION = " ,、，。；;"


def apply_glossary(text: str) -> Tuple[str, List[str]]:
    """把命中的中文术语替换为固定英文标签。

    正则分支由长到短排列，保证「全裸」先于「裸体」匹配；``re.sub`` 从左到右
    扫描，因此命中标签保持原文出现顺序。

    Args:
        text: 中文绘图描述。

    Returns:
        Tuple[str, List[str]]: 替换后的文本，以及按原文顺序去重的命中标签。
    """
    hits: List[str] = []

    def replace(match: "re.Match[str]") -> str:
        english = GLOSSARY[match.group(0)]
        for tag in (part.strip() for part in english.split(",")):
            if tag and tag not in hits:
                hits.append(tag)
        return f" {english} "

    result = _PATTERN.sub(replace, text)
    return _WHITESPACE.sub(" ", result).strip(_EDGE_PUNCTUATION), hits


def merge_tags(first: List[str], second: List[str]) -> List[str]:
    """按顺序合并两组标签，忽略大小写去重并丢弃空项。

    Args:
        first: 先出现的标签组，通常是词表命中结果。
        second: 后出现的标签组，通常是模型输出的标签。

    Returns:
        List[str]: 去重后的标签列表。
    """
    seen = set()
    merged: List[str] = []
    for tag in [*first, *second]:
        normalized = tag.strip()
        if not normalized or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        merged.append(normalized)
    return merged
