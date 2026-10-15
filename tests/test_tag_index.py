import json

from novelai_image.tag_index import DanbooruTagIndex, variant_key


def build_index(tmp_path, rows, category=4):
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"version": 1, "category": category, "rows": rows}, ensure_ascii=False), encoding="utf-8")
    return DanbooruTagIndex(path, category=category)


def test_variant_key_groups_common_transliterations():
    assert variant_key("希格莉卡") == variant_key("西格莉卡")
    assert variant_key("德妮娅") == variant_key("达妮娅")
    assert variant_key("普通描述") == "普通描述"


def test_candidates_find_transliteration_variant(tmp_path):
    index = build_index(tmp_path, [{"name": "sigrika_(wuthering_waves)", "zh": "西格莉卡", "post_count": 1252}])
    assert [hit["tag"] for hit in index.candidates("希格莉卡站在雨中")] == ["sigrika_(wuthering_waves)"]


def test_candidates_find_variant_when_alias_holds_the_variant(tmp_path):
    # 变体出在别名一侧：别名「达妮娅」，文本写「德妮娅」。
    index = build_index(tmp_path, [{"name": "denia_(wuthering_waves)", "zh": "达妮娅", "post_count": 2568}])
    assert [hit["tag"] for hit in index.candidates("德妮娅在跳舞")] == ["denia_(wuthering_waves)"]


def test_candidates_ignore_unrelated_text(tmp_path):
    index = build_index(tmp_path, [{"name": "ganyu_(genshin_impact)", "zh": "甘雨", "post_count": 20273}])
    assert index.candidates("一只猫在睡觉") == []


def test_exact_match_still_wins_by_length(tmp_path):
    index = build_index(tmp_path, [
        {"name": "sigrika_(wuthering_waves)", "zh": "西格莉卡", "post_count": 1252},
        {"name": "lucas_(mother_3)", "zh": "利卡", "post_count": 1011},
    ])
    tags = [hit["tag"] for hit in index.candidates("西格莉卡在跳舞")]
    assert tags[0] == "sigrika_(wuthering_waves)"
    assert "lucas_(mother_3)" in tags  # 候选不过滤，交由 resolve_official 处理子串


def test_normalize_repairs_over_suffixed_names(tmp_path):
    # 翻译模型会把无歧义的角色名补成 name_(series)：gojo_satoru 被写成
    # gojo_satoru_(jujutsu_kaisen)，后者在 Danbooru 不存在，模型认不出来。
    index_file = tmp_path / "character_index.json"
    index_file.write_text(json.dumps({"version": 1, "category": 4, "rows": [
        {"name": "gojo_satoru", "zh": "五条悟", "post_count": 6163, "cat": 4},
        {"name": "ryoumen_sukuna_(jujutsu_kaisen)", "zh": "宿傩", "post_count": 2491, "cat": 4},
    ]}, ensure_ascii=False), encoding="utf-8")
    index = DanbooruTagIndex(index_file, category=4)
    assert index.normalize(["gojo_satoru_(jujutsu_kaisen)", "ryoumen_sukuna_(jujutsu_kaisen)"]) == [
        "gojo_satoru", "ryoumen_sukuna_(jujutsu_kaisen)"]
    # 本来就对的不能被改坏
    assert index.normalize(["gojo_satoru", "blindfold"]) == ["gojo_satoru", "blindfold"]


def test_parsed_index_is_shared_between_instances(tmp_path):
    # 全量索引解析要几秒、几十万条词条。每个实例各解析一遍的话，
    # 测试里几十个实例就是一两分钟。同一份文件应当共享解析结果。
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"version": 1, "category": 4, "rows": [
        {"name": "rem_(re:zero)", "zh": "雷姆", "post_count": 10361, "cat": 4},
    ]}, ensure_ascii=False), encoding="utf-8")
    first, second = DanbooruTagIndex(path, 4), DanbooruTagIndex(path, 4)
    first._load()
    second._load()
    assert first._terms is second._terms
    assert first._aliases is second._aliases


def test_cache_is_invalidated_when_the_file_changes(tmp_path):
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"version": 1, "category": 4, "rows": [
        {"name": "rem_(re:zero)", "zh": "雷姆", "post_count": 10361, "cat": 4},
    ]}, ensure_ascii=False), encoding="utf-8")
    first = DanbooruTagIndex(path, 4)
    first._load()
    path.write_text(json.dumps({"version": 1, "category": 4, "rows": [
        {"name": "rem_(re:zero)", "zh": "蕾姆", "post_count": 10361, "cat": 4},
        {"name": "ram_(re:zero)", "zh": "拉姆", "post_count": 3602, "cat": 4},
    ]}, ensure_ascii=False), encoding="utf-8")
    fresh = DanbooruTagIndex(path, 4)
    assert [h["tag"] for h in fresh.candidates("拉姆")] == ["ram_(re:zero)"]
