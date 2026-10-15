"""本地 Danbooru 普通标签词典；不是机器学习模型。"""

from pathlib import Path
from typing import Dict, List, Tuple
import json
import re

from .exceptions import NovelAIError

# 全量 Danbooru：角色约 40 万、版权约 7 万、普通约 62 万，JSON 约 60-100 MB。
# 上限按全量留余量；candidates() 有首字倒排，不靠扫描量换准确度。
MAX_INDEX_BYTES = 192 * 1024 * 1024
MAX_ROWS = 800000
# 候选上限：够翻译模型判断，又不至于把系统提示撑爆。
MAX_CANDIDATES = 30
# 同一个中文名最多带出几个标签，供调用方按作品名消歧。
MAX_SAME_NAME = 3
_SAFE_TAG = re.compile(r"[a-z0-9][a-z0-9_()': /+\-]{0,127}\Z")

# 中文音译常见变体：同音或近音的字归为一类，用于补充匹配。
# 只收高置信度、且不易与常用词碰撞的字；匹配要求整段每个字都同类，因此精度较高。
VARIANT_GROUPS = (
    "希西夕", "德达", "莉丽利", "娅亚雅", "妮尼", "娜纳那", "蒂迪帝", "斯丝司",
    "特托", "洛罗萝", "琳林霖", "露路鲁卢", "米蜜咪弥", "莎沙纱", "薇维微威",
    "蕾雷莱", "卡咔", "弗佛芙", "伊依", "尔耳", "穆木", "贝倍佩", "菲非",
)
_VARIANT: Dict[str, str] = {char: group[0] for group in VARIANT_GROUPS for char in group}


def variant_key(text: str) -> str:
    """把音译变体字替换为同类代表字；逐字替换，因此下标与原文一一对应。"""
    return "".join(_VARIANT.get(char, char) for char in text)


def _work_affinity(tag: str, works: set) -> int:
    """标签名括号里的系列名是否也在本轮出现。

    「七大罪的路西法」里 lucifer_(sin:_nanatsu_no_taizai) 与 nanatsu_no_taizai 对得上，
    lucifer_(helltaker) 对不上——同名角色靠这个区分。
    """
    if "(" not in tag:
        return 0
    suffix = tag.rsplit("(", 1)[-1].rstrip(")")
    if not suffix:
        return 0
    # 只认「作品名出现在角色标签的系列位里」这一个方向。反向（系列位是作品名的子串）
    # 太松：hatsune_miku_(sp) 的 sp 会命中 springtime_(pierre_auguste_cot)。
    return int(any(work == suffix or work in suffix for work in works))


# 解析好的索引按（路径, 分类, mtime, 大小）在模块级共享。
# 全量索引是几十万条词条，解析一次要好几秒；每个实例各解析一遍的话，
# 测试里几十个实例就是一两分钟，生产上多处持有时也会白白多占内存。
_INDEX_CACHE: Dict[tuple, tuple] = {}
_INDEX_CACHE_LIMIT = 4


def _index_structures(aliases, terms, variant_terms):
    by_first: Dict[str, list] = {}
    for item in terms:
        by_first.setdefault(item[0][0], []).append(item)
    variants_by_first: Dict[str, list] = {}
    for item in variant_terms:
        variants_by_first.setdefault(item[0][0], []).append(item)
    return aliases, terms, variant_terms, by_first, variants_by_first


class DanbooruTagIndex:
    """读取单一 category 的词典，只提供词义候选与精确规范化。

    ``category`` 默认 0（普通视觉标签）；官方角色与版权标签用 4。
    """

    def __init__(self, path: Path, category: int = 0):
        self.path = path
        self.category = category
        self._signature: Tuple[int, int] | None = None
        self._aliases: Dict[str, str] = {}
        self._terms: List[Tuple[str, str, int, int]] = []
        self._variant_terms: List[Tuple[str, str, str, int, int]] = []
        # 首字倒排：全量索引下词条五十万量级，逐条扫全表会让画图卡住。
        self._terms_by_first: Dict[str, List[Tuple[str, str, int, int]]] = {}
        self._variants_by_first: Dict[str, List[Tuple[str, str, str, int, int]]] = {}

    def _load(self) -> None:
        try:
            stat = self.path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature == self._signature:
                return
            cache_key = (str(self.path), self.category) + signature
            cached = _INDEX_CACHE.get(cache_key)
            if cached is not None:
                (self._aliases, self._terms, self._variant_terms,
                 self._terms_by_first, self._variants_by_first) = cached
                self._signature = signature
                return
            if stat.st_size > MAX_INDEX_BYTES:
                raise ValueError
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_INDEX_BYTES + 1)
            if len(raw) > MAX_INDEX_BYTES:
                raise ValueError
            data = json.loads(raw)
            rows = data.get("rows") if isinstance(data, dict) else None
            if not isinstance(rows, list) or len(rows) > MAX_ROWS or data.get("version") != 1 or data.get("category") != self.category:
                raise ValueError
            aliases: Dict[str, str] = {}
            extra_aliases: Dict[str, str] = {}
            ambiguous = set()
            terms: List[Tuple[str, str, int, int]] = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError
                name, zh, count = row.get("name"), row.get("zh"), row.get("post_count", 0)
                if not isinstance(name, str) or not isinstance(zh, str) or not isinstance(count, int) or count < 0 or len(zh) > 128:
                    raise ValueError
                # 不发送可当成模型指令的异常标签；标点标签保留原始输出。
                if not _SAFE_TAG.fullmatch(name):
                    continue
                # 逐行分类：角色(4)与版权(3)混在一个文件里，靠名字有没有括号分不开
                # （gojo_satoru 就没有括号）。缺字段的老索引退回文件级 category。
                cat = row.get("cat", self.category)
                if not isinstance(cat, int):
                    cat = self.category
                aliases[self._key(name)] = name
                if 2 <= len(zh) <= 32 and re.search(r"[\u3400-\u9fff]", zh):
                    terms.append((zh, name, count, cat))
                row_aliases = row.get("aliases", [])
                if not isinstance(row_aliases, list) or len(row_aliases) > 32:
                    raise ValueError
                for alias in row_aliases:
                    if not isinstance(alias, str) or len(alias) > 128:
                        raise ValueError
                    if 2 <= len(alias) <= 32 and re.search(r"[\u3400-\u9fff]", alias):
                        terms.append((alias, name, count, cat))
                    elif _SAFE_TAG.fullmatch(alias.lower()):
                        key = self._key(alias)
                        if key in extra_aliases and extra_aliases[key] != name:
                            ambiguous.add(key)
                        extra_aliases[key] = name
            # 上游别名指向多个标签时不做转换，正式标签本身永远优先。
            for key, name in extra_aliases.items():
                if key not in ambiguous and key not in aliases:
                    aliases[key] = name
            variant_terms = [(variant_key(zh), zh, name, count, cat) for zh, name, count, cat in terms]
            structures = _index_structures(aliases, terms, variant_terms)
            if len(_INDEX_CACHE) >= _INDEX_CACHE_LIMIT:
                _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
            _INDEX_CACHE[cache_key] = structures
            (self._aliases, self._terms, self._variant_terms,
             self._terms_by_first, self._variants_by_first) = structures
            self._signature = signature
        except (OSError, ValueError, TypeError, UnicodeError):
            raise NovelAIError("本地 Danbooru 标签索引不可读或格式错误，请管理员检查 translation.tag_index_path。") from None

    @staticmethod
    def _key(tag: str) -> str:
        return " ".join(tag.lower().replace("_", " ").split())

    def candidates(self, text: str) -> List[Dict[str, object]]:
        self._load()
        # 按首字取候选池再逐条判断包含关系，而不是全表扫一遍。
        # 索引到全量 Danbooru 后词条有五十万量级，全表扫会让每次画图都卡住；
        # 首字筛选把扫描量压到「文本里出现过的字所开头的那些词条」。
        pool: List[Tuple[str, str, int, int]] = []
        for char in set(text):
            pool.extend(self._terms_by_first.get(char, ()))
        # 先做字面匹配；候选并不等于确认标签，模型须判断词义。
        hits = [(zh, name, count, cat) for zh, name, count, cat in pool if zh in text]
        # 补充音译变体匹配：整段每个字都同类才算命中，避免撞上不同角色。
        # 变体可能出在别名一侧（如别名「达妮娅」对文本「德妮娅」），因此不能只看文本。
        normalized = variant_key(text)
        if self._variants_by_first:
            variant_pool: List[Tuple[str, str, str, int, int]] = []
            for char in set(normalized):
                variant_pool.extend(self._variants_by_first.get(char, ()))
            hits += [(zh, name, count, cat) for key, zh, name, count, cat in variant_pool if key in normalized]
        # 字面匹配与音译变体匹配会把同一个标签带进来两三次，先按 (名字, 标签) 去重，
        # 否则重复项会把同名组的名额占满，真正的同名标签反而进不来。
        hits = list(dict.fromkeys(hits))
        hits.sort(key=lambda item: (-len(item[0]), -item[2], item[1]))
        # 同名多标签要一起给出去。调用方靠"本轮写出的作品名"在这些同名标签里挑，
        # 「七大罪的路西法」只给 Helltaker 那个就无从挑起——两边中文名都是路西法。
        # 所以先按名字分组，组内把"系列名也在本轮出现"的排前面，再按名字数截断。
        works = {name for _, name, _, cat in hits if cat != 4}
        grouped: Dict[str, List[Tuple[str, str, int, int]]] = {}
        for item in hits:
            grouped.setdefault(item[0], []).append(item)
        for group in grouped.values():
            group.sort(key=lambda item: (-_work_affinity(item[1], works), -item[2], item[1]))
            del group[MAX_SAME_NAME:]
        result: List[Dict[str, object]] = []
        for zh, group in grouped.items():
            for _, name, count, cat in group:
                # count 一并带出去：同一段别名常挂在多个标签上，调用方要按热度收敛。
                result.append({"zh": zh, "tag": name, "count": count, "cat": cat})
            if len(result) >= MAX_CANDIDATES:
                break
        return result

    def normalize(self, tags: List[str]) -> List[str]:
        self._load()
        result: List[str] = []
        seen = set()
        for tag in tags:
            canonical = self._aliases.get(self._key(tag)) or self._repair(tag)
            key = self._key(canonical)
            if key and key not in seen:
                seen.add(key)
                result.append(canonical)
        return result

    def _repair(self, tag: str) -> str:
        """修掉"多加了系列后缀"的标签。

        翻译模型会把无歧义的官方角色名补成 ``name_(series)``：``gojo_satoru`` 被写成
        ``gojo_satoru_(jujutsu_kaisen)``，而后者在 Danbooru 根本不存在，模型认不出来。
        索引里查不到整串时，去掉括号再查一次，查得到就用去掉后的规范写法。
        """
        stripped = tag.strip()
        stem = stripped.split("(", 1)[0].strip()
        if stem and stem != stripped:
            canonical = self._aliases.get(self._key(stem))
            if canonical:
                return canonical
        return stripped
