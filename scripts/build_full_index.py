"""把抓下来的全量 Danbooru 标签合并成本地索引。

读 crawl_danbooru_tags.py 落在 full_raw/ 下的分页 JSONL，合并既有中文名词表，
写出：
    character_index.json  角色(category 4) + 版权(category 3)
    index.json            普通标签(category 0)

用法：
    python build_full_index.py [tag_index 目录]
"""

from pathlib import Path
import json
import re
import sqlite3
import sys

CATEGORIES = (3, 4)
MAX_ALIASES_PER_ROW = 32
ZH_PATTERN = r"[㐀-鿿]"
# 中文对照表里的名字常带作品后缀（「五条悟（咒术回战）」），
# 这个整串不作为匹配用的名字——用户只会写「五条悟」。
SERIES_SUFFIX = re.compile(r"[\s]*[（(][^)）]{1,40}[)）][\s]*$")
# 对照表里不少作品名带拉丁前缀（「Re:从零开始的异世界生活」），
# 用户只会写后半截，把它也作为别名，否则整条匹配不上。
LEADING_LATIN = re.compile(r"^[\s]*[A-Za-z][A-Za-z0-9 .&'!\-]*[：:][\s]*")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def load_translated_jsonl(path: Path) -> dict:
    """读 translate_tag_names.py 的产物；词表没覆盖到的冷门标签由翻译模型补。"""
    if not path.is_file():
        return {}
    out = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            name, zh = item.get("name"), item.get("zh")
            if isinstance(name, str) and isinstance(zh, str) and zh.strip():
                out[name] = zh.strip()
    return out


def load_chinese_table(path: Path) -> dict:
    """读社区维护的中英对照表（ffdkj 的 tag.sqlite，覆盖 post_count>=10 的全部标签）。"""
    if not path.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = conn.execute("select name, cn_name from tags where cn_name is not null and cn_name != ''").fetchall()
        conn.close()
    except sqlite3.Error:
        return {}
    return {str(name): str(cn).strip() for name, cn in rows if str(cn).strip()}


def read_raw(raw_dir: Path) -> dict:
    """分页 JSONL 合并成 {name: (category, post_count, is_deprecated)}。"""
    rows: dict = {}
    for path in sorted(raw_dir.glob("cat*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                name = item.get("name")
                if isinstance(name, str) and name:
                    rows[name] = (int(item.get("category", 0)), int(item.get("post_count", 0)), bool(item.get("is_deprecated")))
    return rows


def build(rows: dict, categories, zh_names: dict, other_names: dict, chinese: dict) -> dict:
    output = []
    for name, (category, post_count, deprecated) in rows.items():
        if category not in categories or deprecated:
            continue
        # 中文名：本地人工词表优先，其次社区对照表。对照表的值常带作品后缀，
        # 拆成「五条悟」+「五条悟（咒术回战）」两条，用户写哪种都能命中。
        translated = chinese.get(name, "")
        bare = SERIES_SUFFIX.sub("", translated).strip()
        primary = bare if bare else translated
        zh = str(zh_names.get(name, "")) or primary
        aliases, seen = [], set()
        stripped_latin = LEADING_LATIN.sub("", primary).strip()
        extra = [translated, stripped_latin] if stripped_latin and stripped_latin != zh else [translated]
        for candidate in (extra if any(item and item != zh for item in extra) else []) + list(other_names.get(name, [])):
            if not isinstance(candidate, str):
                continue
            key = candidate.strip()
            if not key or key in seen or len(key) > 128:
                continue
            seen.add(key)
            aliases.append(key)
            if len(aliases) >= MAX_ALIASES_PER_ROW:
                break
        # cat 逐行保留：角色(4)与版权(3)混在一个文件里，靠名字里有没有括号分不开
        # （gojo_satoru 就没有括号），只能把 Danbooru 的原始分类带过来。
        output.append({"name": name, "zh": zh, "post_count": post_count,
                       "cat": category, "aliases": aliases})
    output.sort(key=lambda row: -row["post_count"])
    return {"version": 1, "category": 4 if 4 in categories else 0, "rows": output}


def main() -> None:
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[3] / "data" / "plugins" / "local.novelai-image" / "tag_index"
    raw_dir = base / "full_raw"
    if not raw_dir.is_dir():
        raise SystemExit(f"没有找到抓取结果目录：{raw_dir}")
    rows = read_raw(raw_dir)
    print(f"原始标签 {len(rows)} 条", flush=True)

    zh_names = {}
    for name in ("general_zh.json", "zh_supplement.json", "zh_manual.json", "general_manual.json"):
        zh_names.update(load_json(base / name))
    other_names = load_json(base / "wiki_other_names.json")
    # 社区对照表打底，模型翻译补冷门缺口；两份合并后按标签名取用。
    chinese = load_chinese_table(base / "danbooru_zh.sqlite")
    translated = load_translated_jsonl(base / "zh_translated.jsonl")
    print(f"中文对照表 {len(chinese)} 条，模型补译 {len(translated)} 条", flush=True)
    chinese.update(translated)

    def dump(payload, path):
        with_chinese = sum(1 for row in payload["rows"] if row["zh"] or any(re.search(ZH_PATTERN, a) for a in row["aliases"]))
        path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"{path.name}: {len(payload['rows'])} 行，含中文 {with_chinese} 行，{path.stat().st_size / 1e6:.1f} MB", flush=True)

    dump(build(rows, CATEGORIES, zh_names, other_names, chinese), base / "character_index.json")
    dump(build(rows, (0,), zh_names, other_names, chinese), base / "index.json")


if __name__ == "__main__":
    main()
