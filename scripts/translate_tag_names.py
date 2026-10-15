"""用翻译模型批量生成标签的中文名，断点续传。

Danbooru 的 API 不返回中文名，本地那份中文词表只覆盖两万条量级。
这里把抓下来的全量角色/版权标签分批丢给翻译模型，让它给出通用中文译名，
结果落到 zh_translated.jsonl 供 build_full_index.py 合并。

用法：
    python translate_tag_names.py [tag_index 目录] [--min-posts 10] [--limit N] [--batch 60]

已翻译过的标签会跳过，中断后重跑即可续传。
"""

from pathlib import Path
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx

from novelai_image.nanogpt import NANOGPT_BASE_URL, NANOGPT_MODEL, resolve_nanogpt_key

SYSTEM = """你在为 Danbooru 标签生成中文对照表。输入是一批英文标签名，可能是角色名、作品名或画师名。
输出一个 JSON 对象：键是输入里原样的标签名，值是它最通用的中文名。

规则：
- 角色名给最通用的中文译名（gojo_satoru → 五条悟，hatsune_miku → 初音未来）。
- 作品名同理（jujutsu_kaisen → 咒术回战）。
- 画师名通常没有中文译名，给空字符串。
- 不确定、或想不出通用译名时给空字符串，**不要音译、不要编造**。
- 值必须是中文，不要英文。
- 键必须与输入完全一致，一个不多一个不少。"""


def batched(items, size):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def load_targets(raw_dir: Path, min_posts: int, known: set) -> list:
    rows = {}
    for path in sorted(raw_dir.glob("cat*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if item.get("is_deprecated") or item.get("category") not in (3, 4):
                    continue
                if int(item.get("post_count", 0)) >= min_posts:
                    rows[item["name"]] = int(item["post_count"])
    return [name for name in sorted(rows, key=lambda n: -rows[n]) if name not in known]


def main() -> None:
    argv = sys.argv[1:]
    base = Path(argv[0]) if argv and not argv[0].startswith("--") else Path(__file__).resolve().parents[3] / "data" / "plugins" / "local.novelai-image" / "tag_index"
    def option(flag, default):
        return int(argv[argv.index(flag) + 1]) if flag in argv else default

    min_posts, limit, batch_size = option("--min-posts", 10), option("--limit", 0), option("--batch", 60)
    out_path = base / "zh_translated.jsonl"
    done = set()
    if out_path.is_file():
        with out_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    done.add(json.loads(line)["name"])
                except (ValueError, KeyError):
                    continue
    known = set()
    for name in ("general_zh.json", "zh_supplement.json", "zh_manual.json", "general_manual.json"):
        path = base / name
        if path.is_file():
            known.update(json.loads(path.read_text(encoding="utf-8")))
    known |= done
    # 社区对照表已经覆盖 post_count>=10 的绝大部分，不算进来的话会白翻十几万条。
    table = base / "danbooru_zh.sqlite"
    if table.is_file():
        conn = sqlite3.connect(f"file:{table}?mode=ro", uri=True)
        known |= {r[0] for r in conn.execute("select name from tags where cn_name is not null and cn_name != ''")}
        conn.close()

    targets = load_targets(base / "full_raw", min_posts, known)
    if limit:
        targets = targets[:limit]
    print(f"待翻译 {len(targets)} 条（post_count>={min_posts}，已跳过 {len(known)} 条）", flush=True)

    token = resolve_nanogpt_key()
    if not token:
        raise SystemExit("未配置 NANOGPT_API_KEY")
        # 同步 Client 必须配同步 transport：AsyncHTTPTransport 会直接抛 __enter__ 缺失，
    # 之前每条请求都这样失败、又被 except 吞掉，结果整批写空值。
    transport = httpx.HTTPTransport(proxy=os.environ.get("NANOGPT_PROXY") or None, trust_env=False)
    written = 0
    with out_path.open("a", encoding="utf-8") as handle:
        for index, group in enumerate(batched(targets, batch_size), 1):
            payload = {
                "model": NANOGPT_MODEL,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": json.dumps(group, ensure_ascii=False)}],
                "temperature": 0.1, "max_tokens": 4000, "stream": False,
                "response_format": {"type": "json_object"},
            }
            mapping = {}
            for attempt in range(4):
                try:
                    with httpx.Client(timeout=httpx.Timeout(120), transport=transport, trust_env=False) as client:
                        response = client.post(f"{NANOGPT_BASE_URL}/chat/completions", json=payload,
                                               headers={"Authorization": "Bearer " + token})
                    if response.status_code == 429:
                        time.sleep(8 * (attempt + 1)); continue
                    response.raise_for_status()
                    content = response.json()["choices"][0]["message"]["content"]
                    mapping = json.loads(content)
                    break
                except Exception:
                    time.sleep(4 * (attempt + 1))
            for name in group:
                value = mapping.get(name)
                if not isinstance(value, str):
                    value = ""
                value = value.strip()
                # 只收真正的中文，模型偶尔会给拼音或英文
                if value and not re.search(r"[一-鿿]", value):
                    value = ""
                handle.write(json.dumps({"name": name, "zh": value}, ensure_ascii=False) + "\n")
                written += 1
            handle.flush()
            if index % 10 == 0:
                print(f"  已处理 {index * batch_size} 条，写入 {written}", flush=True)
    print(f"完成，共写入 {written} 条到 {out_path}", flush=True)


if __name__ == "__main__":
    main()
