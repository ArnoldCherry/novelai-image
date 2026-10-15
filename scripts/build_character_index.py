"""从本地 Danbooru 维度表生成官方角色与版权索引（category 3 与 4）。

与 tag_index/index.json 同源、全部本地读取、不联网。输出 character_index.json，
schema 与 index.json 一致，因此可直接被 tag_index.DanbooruTagIndex 读取。

用法：
    python build_character_index.py [tag_index 目录]

默认目录为 <仓库>/data/plugins/local.novelai-image/tag_index。
"""

from pathlib import Path
import json
import re
import sys

import pandas as pd

CATEGORIES = (3, 4)
ZH_PATTERN = re.compile(r"[㐀-鿿]")
MAX_ALIASES_PER_ROW = 32


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def build(source: Path) -> dict:
    tags = pd.read_parquet(source / "dim_tag.parquet")
    zh_names = {**load_json(source / "general_zh.json"), **load_json(source / "zh_supplement.json")}
    other_names = load_json(source / "wiki_other_names.json")
    rows = []
    for record in tags[tags.category.isin(CATEGORIES)].itertuples():
        name = str(record.name)
        aliases, seen = [], set()
        # wiki_other_names 是中文名的主要来源：芙宁娜、西格莉卡、鸣潮西格莉卡等。
        for alias in other_names.get(name, []):
            if not isinstance(alias, str):
                continue
            key = alias.strip()
            if not key or key in seen or len(key) > 128:
                continue
            seen.add(key)
            aliases.append(key)
            if len(aliases) >= MAX_ALIASES_PER_ROW:
                break
        rows.append({
            "name": name,
            "zh": str(zh_names.get(name, "")),
            "post_count": int(record.post_count),
            "aliases": aliases,
        })
    rows.sort(key=lambda row: -row["post_count"])
    return {"version": 1, "category": 4, "source": str(source), "rows": rows}


def main() -> None:
    if len(sys.argv) > 1:
        source = Path(sys.argv[1])
    else:
        source = Path(__file__).resolve().parents[3] / "data" / "plugins" / "local.novelai-image" / "tag_index"
    output = source / "character_index.json"
    data = build(source)
    with_chinese = sum(1 for row in data["rows"] if row["zh"] or any(ZH_PATTERN.search(a) for a in row["aliases"]))
    output.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"写出 {output}")
    print(f"行数 {len(data['rows'])}，其中含中文名或中文别名的 {with_chinese} 行，大小 {output.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
