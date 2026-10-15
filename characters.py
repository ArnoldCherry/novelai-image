"""只读取共享角色库和当前请求者自己的私人角色库。"""
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple
from io import BytesIO
import base64
import hashlib
import json
import logging
import re
import warnings

from PIL import Image, ImageOps
from .exceptions import NovelAIError
from .tag_index import variant_key

logger = logging.getLogger(__name__)
CATALOG_NAME = "github.QlzqQlzq.character-knowledge-plugin"

# 只收录稳定身份特征：发色、发型发长、瞳色、耳、尾、物种特征。
# 服装、姿势、道具、画风一律不收——那些属于本轮要求，不能从知识库继承。
IDENTITY_TERMS: Dict[str, str] = {
    # 发色
    "银白": "silver hair",
    "银灰": "silver grey hair",
    "银发": "silver hair",
    "白发": "white hair",
    "灰发": "grey hair",
    "黑发": "black hair",
    "金发": "blonde hair",
    "红发": "red hair",
    "棕发": "brown hair",
    "浅棕": "light brown hair",
    "深棕": "dark brown hair",
    "粉发": "pink hair",
    "紫发": "purple hair",
    "淡紫": "light purple hair",
    "蓝发": "blue hair",
    "绿发": "green hair",
    # 发型与发长
    "中长发": "medium hair",
    "长发": "long hair",
    "短发": "short hair",
    "卷发": "curly hair",
    "直发": "straight hair",
    "双马尾": "twintails",
    "高马尾": "high ponytail",
    "马尾": "ponytail",
    "刘海": "bangs",
    # 瞳色
    "浅蓝灰": "grey blue eyes",
    "蓝瞳": "blue eyes",
    "红瞳": "red eyes",
    "绿瞳": "green eyes",
    "金瞳": "yellow eyes",
    "紫瞳": "purple eyes",
    "异色瞳": "heterochromia",
    # 兽耳
    "猫耳": "cat ears",
    "狐耳": "fox ears",
    "犬耳": "dog ears",
    "狼耳": "wolf ears",
    "兔耳": "rabbit ears",
    "兽耳": "animal ears",
    "精灵耳": "pointy ears",
    # 尾
    "猫尾": "cat tail",
    "狐尾": "fox tail",
    "兽尾": "tail",
    # 其他物种特征
    "龙角": "dragon horns",
    "天使翅膀": "angel wings",
    "恶魔翅膀": "demon wings",
    "翅膀": "wings",
}

_IDENTITY_BY_LENGTH = sorted(IDENTITY_TERMS.items(), key=lambda item: -len(item[0]))
_IDENTITY_PATTERN = re.compile("|".join(re.escape(term) for term, _ in _IDENTITY_BY_LENGTH))
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Character:
    name: str
    aliases: tuple[str, ...]
    descriptions: tuple[str, ...]
    references: tuple[str, ...]
    catalog: Path


def _strings(value, limit: int, length: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item.strip()[:length] for item in value[:limit] if isinstance(item, str) and item.strip())


def load_characters(data_dir: Path, prompt: str, user_id: str, catalog_path: str = "") -> list[Character]:
    current = data_dir.resolve()
    root = next((p for p in (current, *current.parents) if p.name.casefold() == "data"), None)
    path = Path(catalog_path).expanduser().resolve() if catalog_path else (root / "plugins" / CATALOG_NAME / "characters.json" if root else None)
    if path is None:
        return []
    catalogs = [path]
    if user_id:
        digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:24]
        catalogs.append(path.parent / "personal_characters" / digest / "characters.json")
    visible: dict[str, Character] = {}
    for catalog in catalogs:
        try:
            if catalog.is_symlink() or (catalog != path and any(parent.is_symlink() for parent in (catalog.parent, catalog.parent.parent))):
                raise NovelAIError("角色知识库路径包含符号链接，请管理员检查数据目录。")
            if not catalog.exists():
                continue
            if catalog.stat().st_size > 4 * 1024 * 1024:
                raise NovelAIError("角色知识库超过 4 MiB 读取限制，请管理员检查。")
            raw = json.loads(catalog.read_text(encoding="utf-8-sig"))
            entries = raw.get("characters") if isinstance(raw, dict) else None
            if not isinstance(entries, list):
                raise NovelAIError("角色知识库缺少有效 characters 列表，请管理员检查。")
            for entry in entries[:2000]:
                if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                    continue
                name = entry["name"].strip()[:128]
                if not name:
                    continue
                raw_cards = entry.get("appearance_cards")
                if isinstance(raw_cards, list) and (len(raw_cards) > 30 or any(isinstance(card, str) and len(card) > 4096 for card in raw_cards)):
                    raise NovelAIError("角色外观卡超过读取预算，请管理员精简角色资料。")
                cards = _strings(raw_cards, 30, 4096)
                note = entry.get("visual_reference_note")
                if isinstance(note, str) and note.strip():
                    if len(note) > 4096:
                        raise NovelAIError("角色视觉备注超过读取预算，请管理员精简角色资料。")
                    cards += (note.strip(),)
                visible[name.casefold()] = Character(name, _strings(entry.get("aliases"), 32, 128), cards, _strings(entry.get("reference_images"), 8, 512), catalog)
        except (OSError, ValueError, UnicodeError):
            raise NovelAIError("角色知识库读取失败，请管理员检查文件格式和访问权限。") from None
    def position(label: str) -> int:
        if len(label) < 2:
            return -1
        if label.isascii():
            found = re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", prompt, re.I)
            return found.start() if found else -1
        return prompt.casefold().find(label.casefold())

    def matches(label: str) -> bool:
        return position(label) >= 0

    # 先覆盖同名角色再匹配别名，避免共享旧别名绕过私人覆盖。
    found = [role for role in visible.values() if any(matches(label) for label in (role.name, *role.aliases))]
    # 顺序跟着用户写的顺序走：第一位决定拿谁的参考图，按目录顺序排会把别人的图发出去。
    found.sort(key=lambda role: min((spot for spot in (position(label) for label in (role.name, *role.aliases)) if spot >= 0), default=len(prompt)))
    if len(found) > 4:
        raise NovelAIError("本次角色超过 4 个，请减少角色后再绘图。")
    return found


def precise_reference(role: Character) -> str | None:
    if (role.catalog.parent / "references").is_symlink():
        return None
    root = (role.catalog.parent / "references").resolve()
    for relative_text in role.references:
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        try:
            raw_path = role.catalog.parent / relative
            if raw_path.is_symlink() or any(parent.is_symlink() for parent in raw_path.parents if parent != role.catalog.parent and parent.is_relative_to(role.catalog.parent)):
                continue
            path = raw_path.resolve()
            if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(path.read_bytes())) as source:
                    if source.width * source.height > 16_777_216 or source.format not in ("PNG", "JPEG", "WEBP"):
                        continue
                    source = ImageOps.exif_transpose(source).convert("RGBA")
                    size = (1536, 1024) if source.width > source.height * 1.2 else ((1024, 1536) if source.height > source.width * 1.2 else (1472, 1472))
                    source = ImageOps.contain(source, size, Image.Resampling.LANCZOS)
                    padded = Image.new("RGB", size, "black")
                    padded.paste(source, ((size[0] - source.width) // 2, (size[1] - source.height) // 2), source.getchannel("A"))
                    output = BytesIO()
                    padded.save(output, format="PNG")
                    return base64.b64encode(output.getvalue()).decode("ascii")
        except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            continue
    return None


def identity_tags(role: Character, request: str) -> List[str]:
    """按固定词表从角色外观卡抽取稳定身份标签，不经过任何模型。

    只取发色、发型发长、瞳色、耳、尾、物种这类身份特征；服装、姿势、道具、
    画风不在词表内，因此不会被继承。本轮描述已提到的特征不重复追加，保证
    用户当前要求优先。

    Args:
        role: 命中的角色条目。
        request: 本轮原始描述，用于判断哪些特征由用户自己指定。

    Returns:
        List[str]: 按外观卡出现顺序去重的英文标签。
    """
    text = " ".join(role.descriptions)
    # 用户指定同一维度的任一特征时，整个维度交由本轮要求决定，避免混色。
    groups = {
        "color": r"(?:(?:black|white|red|blond[e]?|brown|pink|purple|blue|green|grey|gray|silver) hair|发色|[黑白红金棕粉紫蓝绿灰银]发)",
        "eyes": r"(?:eyes?|瞳|眼睛|眼色)",
        "shape": r"(?:ponytail|twintails|(?:long|short|medium|curly|straight) hair|马尾|长发|短发|卷发|直发|刘海|bangs)",
        "ears": r"(?:ears?|耳)", "tail": r"(?:tail|尾)", "species": r"(?:horns?|wings?|角|翅膀)",
    }
    overridden = {group for group, pattern in groups.items() if re.search(pattern, request, re.I)}
    def dimension(tag: str) -> str:
        if "eyes" in tag or tag == "heterochromia": return "eyes"
        if "ears" in tag: return "ears"
        if "tail" in tag: return "tail"
        if "horns" in tag or "wings" in tag: return "species"
        if tag in ("medium hair", "long hair", "short hair", "curly hair", "straight hair", "twintails", "high ponytail", "ponytail", "bangs"): return "shape"
        return "color"
    tags: List[str] = []
    # 复合发色描述的是同一种颜色，不能拆成两个互相竞争的发色标签。
    compound_color = re.compile(r"银白(?:色)?(?:偏|带|略带)?(?:的)?(?:淡紫|浅紫)|银白淡紫")
    if compound_color.search(text):
        if "color" not in overridden:
            tags.extend(["silver hair", "subtle lavender hair tint"])
        text = compound_color.sub("", text)
    for match in _IDENTITY_PATTERN.finditer(text):
        term = match.group(0)
        tag = IDENTITY_TERMS[term]
        if dimension(tag) in overridden:
            continue
        if tag not in tags:
            tags.append(tag)
    # 保留兽耳与尾巴的局部细节，不把它们误当成头发颜色。
    details = (
        ("ears", r"白(?:色)?猫耳", "cat ears", "white cat ears"),
        ("tail", r"蓬松(?:的)?猫尾", "cat tail", "fluffy cat tail"),
        ("tail", r"白(?:色)?猫尾", "cat tail", "white cat tail"),
    )
    for group, pattern, base, detail in details:
        if group not in overridden and re.search(pattern, text):
            if base in tags:
                tags.remove(base)
            if detail not in tags:
                tags.append(detail)
    return tags


def strip_names(prompt: str, roles: Sequence[Character]) -> str:
    """剔除本轮描述里命中的角色名与别名。

    小模型见到中文人名要么误译、要么原样保留中文导致校验失败，因此在翻译前
    先摘掉；身份信息由 identity_tags 另行确定性补齐。

    Args:
        prompt: 本轮原始描述。
        roles: 命中的角色条目。

    Returns:
        str: 剔除角色名后的描述，空白已折叠并去掉首尾标点。
    """
    result = prompt
    labels = sorted(
        {label for role in roles for label in (role.name, *role.aliases) if len(label) >= 2},
        key=len,
        reverse=True,
    )
    for label in labels:
        pattern = (r"(?<!\w)" + re.escape(label) + r"(?!\w)") if label.isascii() else re.escape(label)
        result = re.sub(pattern, " ", result, flags=re.I)
    return _WHITESPACE.sub(" ", result).strip(" ,、，。；;")


MAX_OFFICIAL_TAGS = 3
# 两字中文词的命中门槛：短词歧义大，「草莓」「变身」这类通用词会撞上冷门标签。
MIN_SHORT_TERM_POSTS = 300
# 词典会收录角色配对简称（如松风天马的「雨天」）；这类常用场景词
# 不能仅凭角色热度就当成身份。正名与其他明确别名仍按原规则匹配。
AMBIGUOUS_SCENE_TERMS = frozenset({"雨天"})


def _resolve(index, text: str, candidate_text: str | None = None) -> Tuple[List[str], List[str], str]:
    """把本轮描述里的官方作品角色解析成规范 ``name_(series)`` 标签。

    角色名命中后从文本里剔除，避免模型再翻一遍或翻错；只命中作品名的部分保留在
    文本里，让模型仍能看到系列，从而纠正音译差异（如「德妮娅」对官方「达妮娅」）。

    Args:
        index: 已加载官方角色词典的 DanbooruTagIndex（category=4）；可为 None。
        text: 本轮描述。

    Returns:
        Tuple[List[str], List[str], str]: 角色标签、作品标签，以及剔除角色名后的描述。
    """
    if index is None or not text:
        return [], [], text
    hits = index.candidates(candidate_text if candidate_text is not None else text)
    hits = [hit for hit in hits if int(hit.get("cat", 4)) != 4 or str(hit["zh"]) not in AMBIGUOUS_SCENE_TERMS]
    # 同一段别名常挂在多个标签上：「初音未来」同时是 hatsune_miku、
    # magical_mirai_miku、hatsune_miku_(append) 等六个标签的中文别名。不收敛的话
    # 一个名字就带出一串幽灵角色，把真正的第二个角色挤出 MAX_OFFICIAL_TAGS。
    # 同一别名只留一个：本轮写出的作品名优先，其次比热度。
    # 「七大罪的路西法」里「路西法」同时是七大罪和 Helltaker 的角色，
    # 只看热度会选中 Helltaker 那个——用户已经把作品名写出来了。
    matched_works = {str(hit["tag"]) for hit in hits if int(hit.get("cat", 4)) != 4}

    def affinity(hit: Dict[str, object]) -> Tuple[int, int]:
        tag = str(hit["tag"])
        suffix = tag.rsplit("(", 1)[-1].rstrip(")") if "(" in tag else ""
        # 只认一个方向，反向会误命：sp 是 springtime 的子串。
        related = int(any(work == suffix or work in suffix for work in matched_works)) if suffix else 0
        return related, int(hit.get("count", 0))

    best: Dict[str, Dict[str, object]] = {}
    for hit in hits:
        current = best.get(str(hit["zh"]))
        if current is None or affinity(hit) > affinity(current):
            best[str(hit["zh"])] = hit
    hits = list(best.values())
    # 两字中文词歧义太大：「天野草莓在变身」会撞上「草莓」「变身」这类通用词，
    # 命中的又是 12～111 帖的冷门标签。两字词只认热度够高的，长词不受限
    # （五条悟、伊蕾娜这种本身就很具体，不会误撞）。
    hits = [hit for hit in hits if len(str(hit["zh"])) > 2 or int(hit.get("count", 0)) >= MIN_SHORT_TERM_POSTS]
    # 「妮娅」「利卡」这类短名会作为子串命中更长的角色名（音译归一化后更明显），
    # 按归一化形式剔除被包含的那些，避免混入别的角色。
    keys = {id(hit): variant_key(str(hit["zh"])) for hit in hits}

    def dominated(hit: Dict[str, object]) -> bool:
        mine = keys[id(hit)]
        for other in hits:
            if other is hit:
                continue
            theirs = keys[id(other)]
            if mine == theirs:
                # 归一化后同形（「雷姆」对「蕾姆」）：只留热度高的那个。
                # 老写法在这里会把两个都判成被包含，结果一个都不剩。
                if (int(other.get("count", 0)), str(other["zh"])) > (int(hit.get("count", 0)), str(hit["zh"])):
                    return True
            elif mine in theirs:
                return True
        return False

    hits = [hit for hit in hits if not dominated(hit)]
    # 顺序跟着用户写的顺序走：第一位决定主参考图与身份锚点，按索引顺序排会让画出来的是别人。
    normalized = variant_key(text)
    def position(hit: Dict[str, object]) -> Tuple[int, int]:
        term = str(hit["zh"])
        found = text.find(term)
        if found < 0:
            found = normalized.find(variant_key(term))
        return (found if found >= 0 else len(text), -int(hit.get("count", 0)))
    hits.sort(key=position)
    characters: List[str] = []
    works: List[str] = []
    cleaned = text
    for hit in hits:
        tag, term = str(hit["tag"]), str(hit["zh"])
        # 分类取自索引的逐行 category：角色是 4，版权是 3。
        # 靠名字里有没有括号分不开——gojo_satoru 就没有括号，会被误判成作品。
        bucket = characters if int(hit.get("cat", 4)) == 4 else works
        if tag not in bucket and len(characters) + len(works) < MAX_OFFICIAL_TAGS:
            bucket.append(tag)
        # 角色标签形如 name_(series)，已确定性解决，从文本里摘掉；作品名保留。
        if "(" in tag:
            cleaned = _strip_term(cleaned, term)
    return characters, works, _WHITESPACE.sub(" ", cleaned).strip(" ,、，。；;")


def resolve_official_split(index, text: str, candidate_text: str | None = None) -> Tuple[List[str], List[str], str]:
    """同 resolve_official，但把角色与作品分开返回。

    角色要进 char_captions 逐个锚定身份，作品只是留在 base 提示词里的上下文。
    混在一起会让多角色场景只锚定到其中一个：`五宿两人图` 里 gojo_satoru 没有括号，
    按老判据会被当成作品，结果 char_caption 里只剩宿傩一个人。
    """
    if index is None or not text:
        return [], [], text
    return _resolve(index, text, candidate_text)


def resolve_official(index, text: str) -> Tuple[List[str], str]:
    """兼容入口：角色在前、作品在后，其余同 resolve_official_split。"""
    if index is None or not text:
        return [], text
    characters, works, cleaned = _resolve(index, text)
    return [*characters, *works], cleaned


def _strip_term(text: str, term: str) -> str:
    """剔除 text 中所有与 term 同类的写法，含音译变体（如「希格莉卡」对「西格莉卡」）。"""
    if not term:
        return text
    if term.isascii():
        return re.sub(r"(?<!\w)" + re.escape(term) + r"(?!\w)", " ", text, flags=re.I)
    normalized, target = variant_key(text), variant_key(term)
    parts, index = [], 0
    while True:
        found = normalized.find(target, index)
        if found < 0:
            parts.append(text[index:])
            break
        parts.append(text[index:found])
        parts.append(" ")
        index = found + len(target)
    return "".join(parts)
