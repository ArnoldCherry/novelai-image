"""全量抓取 Danbooru 标签，按分类落成断点续传的 JSONL。

单页上限 1000，page 硬上限 1000，因此单次查询最多 100 万条；实测各分类都在线内：
角色 406 页 / 版权 70 页 / 普通 617 页 / meta 2 页。

用法：
    python crawl_danbooru_tags.py <输出目录> [分类...]

已存在的分页文件会跳过，中断后重跑即可续传。
"""

from pathlib import Path
import json
import sys
import time
import urllib.error
import urllib.request

API = "https://danbooru.donmai.us/tags.json"
UA = {"User-Agent": "MaiBot-novelai-image/1.0 (tag index builder)"}
PAGE_SIZE = 1000
SLEEP_SECONDS = 0.7
CATEGORIES = (4, 3, 0, 5)


def fetch(category: int, page: int) -> list:
    url = f"{API}?limit={PAGE_SIZE}&search%5Border%5D=count&search%5Bcategory%5D={category}&page={page}"
    request = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))
    if isinstance(data, dict):
        # 超过 page 上限时返回 {"success": false, ...}
        return []
    return data


def crawl_category(out_dir: Path, category: int) -> int:
    pages = 0
    for page in range(1, 1001):
        target = out_dir / f"cat{category}_page{page:04d}.jsonl"
        if target.is_file() and target.stat().st_size > 0:
            pages += 1
            continue
        for attempt in range(5):
            try:
                rows = fetch(category, page)
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503):
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
            except Exception:
                time.sleep(3 * (attempt + 1))
        else:
            raise RuntimeError(f"分类 {category} 第 {page} 页连续失败")
        if not rows:
            print(f"  分类 {category} 到第 {page} 页结束，共 {pages} 页", flush=True)
            return pages
        with target.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps({
                    "name": row["name"],
                    "category": row["category"],
                    "post_count": row["post_count"],
                    "is_deprecated": bool(row.get("is_deprecated")),
                }, ensure_ascii=False) + "\n")
        pages += 1
        if page % 25 == 0:
            print(f"  分类 {category} 已抓 {page} 页", flush=True)
        time.sleep(SLEEP_SECONDS)
    return pages


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("用法：crawl_danbooru_tags.py <输出目录> [分类...]")
    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    categories = [int(value) for value in sys.argv[2:]] or list(CATEGORIES)
    for category in categories:
        print(f"开始抓取分类 {category}", flush=True)
        pages = crawl_category(out_dir, category)
        print(f"分类 {category} 完成：{pages} 页", flush=True)
    print("全部完成", flush=True)


if __name__ == "__main__":
    main()
