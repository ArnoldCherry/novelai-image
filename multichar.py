"""全局场景与逐角色提示词的边界，以及 NovelAI 角色位置。"""

from typing import Any, Dict, List
import re

from .exceptions import NovelAIError
from .glossary import apply_glossary, merge_tags

MAX_CHARACTERS = 6
POSITIONS = {"left": (0.2, 0.5), "right": (0.8, 0.5), "top": (0.5, 0.2),
             "bottom": (0.5, 0.8), "center": (0.5, 0.5)}


def expected_character_count(text: str, known_count: int = 0) -> int:
    counts: Dict[str, int] = {}
    for match in re.finditer(r"([1-9])\s*(girls?|boys?|people|characters?)\b", text, re.I):
        kind = "girl" if match[2].lower().startswith("girl") else "boy" if match[2].lower().startswith("boy") else "people"
        counts[kind] = max(counts.get(kind, 0), int(match[1]))
    numerals = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    for match in re.finditer(r"([一二两三四五六七八九1-9])(?:个|位|名)?(女孩|女生|少女|男孩|男生|少年|人物|角色|人|男|女)", text):
        kind = "girl" if match[2] in ("女孩", "女生", "少女", "女") else "boy" if match[2] in ("男孩", "男生", "少年", "男") else "people"
        counts[kind] = max(counts.get(kind, 0), numerals.get(match[1], int(match[1]) if match[1].isdigit() else 0))
    return max(known_count, counts.get("people", 0), counts.get("girl", 0) + counts.get("boy", 0), len(text.split("|")) - 1)


def needs_character_split(text: str, known_count: int = 0) -> bool:
    return expected_character_count(text, known_count) > 1 or "|" in text or len(re.findall(r"女孩|女生|少女|男孩|男生|少年|女人|男人", text)) >= 2 or bool(re.search(
        r"[2-6]\s*(?:girls?|boys?|people|characters?)\b|(?:两|二|三|四|五|六|[2-6])(?:个|位|名)?(?:女孩|女生|少女|男孩|男生|少年|人物|角色)|一男一女|一女一男|角色\s*[AB12]", text, re.I))


def enforce_character_count(base: str, count: int) -> str:
    """全局人数必须与独立角色栏一致，移除压回单人的 solo/错误计数。"""
    tags = [tag.strip() for tag in base.split(",") if tag.strip() and tag.strip().lower() not in ("solo", "solo focus")]
    numbers = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
    for index, tag in enumerate(tags):
        match = re.fullmatch(r"(one|two|three|four|five|six)[ _](women|girls|men|boys)", tag, re.I)
        if match:
            gender = "girls" if match[2].lower() in ("women", "girls") else "boys"
            tags[index] = str(numbers[match[1].lower()]) + gender
    observed = expected_character_count(", ".join(tags))
    if observed != count:
        tags = [tag for tag in tags if not re.fullmatch(r"\d+\s*(?:girls?|boys?|people|characters?)", tag, re.I)]
        tags.insert(0, f"{count}people")
    return ", ".join(tags)


def pipe_sections(text: str) -> tuple[str, List[Dict[str, str]]]:
    """与官方一致：全局 | 角色一 | 角色二；直通模式不改写分栏内容。"""
    parts = [part.strip() for part in text.split("|")]
    if len(parts) == 1:
        return text, []
    if not 1 <= len(parts) - 1 <= MAX_CHARACTERS or any(not part for part in parts):
        raise NovelAIError("分栏格式须为 全局场景 | 角色1 | 角色2，最多6个角色，各栏不能为空。")
    return parts[0], [{"positive": part, "negative": "", "position": "auto", "identity_id": ""} for part in parts[1:]]


def caption_payloads(characters: List[Dict[str, Any]], *, auto_layout: bool = False) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    positives, negatives = [], []
    manual = auto_layout or any(character.get("position", "auto") != "auto" for character in characters)
    for index, character in enumerate(characters):
        position = character.get("position", "auto")
        if position not in (*POSITIONS, "auto"):
            raise NovelAIError("角色位置无效。")
        x, y = POSITIONS.get(position, ((index + 0.5) / len(characters) if manual else 0.5, 0.5))
        common = {"centers": [{"x": x, "y": y}], "use_coords": manual}
        positives.append({"char_caption": character["positive"], **common})
        negatives.append({"char_caption": character.get("negative", ""), **common})
    return positives, negatives


def owned_descriptions(text: str, identities: List[Dict[str, Any]], characters: List[Dict[str, Any]], candidates: List[Dict[str, Any]]) -> List[str]:
    """按原文人名/左右称呼归属片段，供显式细节校验；不会把全局词表摊给每个人。"""
    if "|" in text:
        _, parts = pipe_sections(text)
        return [parts[index]["positive"] if index < len(parts) else "" for index in range(len(characters))]
    by_id = {identity["id"]: identity for identity in identities}
    labels = []
    for index, character in enumerate(characters):
        identity = by_id.get(character["identity_id"], {})
        names = [identity.get("name", ""), *identity.get("aliases", [])]
        names += [str(candidate.get("zh", "")) for candidate in candidates if candidate.get("tag") in identity.get("tags", [])]
        names += [f"角色{index + 1}", f"角色{chr(65 + index)}"]
        if character["position"] == "left":
            names += ["左边", "左侧", "左方"]
        elif character["position"] == "right":
            names += ["右边", "右侧", "右方"]
        labels.append(sorted({name for name in names if name}, key=len, reverse=True))
    owned: List[List[str]] = [[] for _ in characters]
    active = None
    for clause in re.split(r"([。；;\n])|[,，]", text):
        if not clause:
            continue
        if clause in "。；;\n":
            active = None
            continue
        matches = []
        for index, names in enumerate(labels):
            if names:
                match = re.search("|".join(re.escape(name) for name in names), clause, re.I)
                if match:
                    matches.append((match.start(), index))
        if len(matches) == 1:
            active = matches[0][1]
        elif len(matches) > 1:
            # 多人共同描述不归给最后一位；各自带谓语时才可按名字切段。
            matches.sort()
            for i, (start, owner) in enumerate(matches):
                end = matches[i + 1][0] if i + 1 < len(matches) else len(clause)
                piece = clause[start:end]
                if re.search(r"穿|戴|拿|捧|握|撑|打|坐|站|hair|dress|holding", piece, re.I):
                    owned[owner].append(piece)
            active = None
            continue
        if active is not None and not re.search(r"两人|所有人|大家|共同|一起|\bboth\b|\btogether\b", clause, re.I):
            owned[active].append(clause)
    return ["，".join(parts) for parts in owned]


def explicit_character_tags(source: str) -> tuple[List[str], List[str]]:
    """只保留本角色明确要求的常用道具动作与排除项，修复翻译器省略。"""
    _, positive = apply_glossary(source)
    negative: List[str] = []
    for noun, tag in (("帽子|帽", "hat"), ("眼镜", "glasses"), ("雨伞|伞", "umbrella"), ("书本|书", "book")):
        denied = rf"(?:不要|不戴|不带|不拿|不撑|没有|不需要|无)(?:任何|一顶|一把|一本|戴|带|拿|着|的)*\s*(?:{noun})"
        if re.search(denied, source):
            negative.append(tag)
    for pattern, tags in (
        (r"(?:拿|捧|抱|握|持).{0,8}(?:书本|书)", ["holding book", "book"]),
        (r"(?:撑|打|拿|持).{0,8}(?:雨伞|伞)", ["holding umbrella", "umbrella"]),
        (r"红(?:色)?(?:的)?(?:雨)?伞", ["red umbrella"]),
    ):
        noun = "book" if "book" in tags else "umbrella"
        if re.search(pattern, source) and noun not in negative:
            positive = merge_tags(positive, tags)
    positive = [tag for tag in positive if not any(noun in tag for noun in negative)]
    return positive, negative
