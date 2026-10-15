from pathlib import Path
import hashlib
import json

from novelai_image.characters import Character, identity_tags, load_characters, resolve_official, strip_names
from novelai_image.glossary import merge_tags
from novelai_image.tag_index import DanbooruTagIndex


def role(name="小虚", aliases=(), cards=()):
    return Character(name, tuple(aliases), tuple(cards), (), Path("/tmp/characters.json"))


def write_catalog(data_root: Path, characters):
    catalog = data_root / "plugins" / "github.QlzqQlzq.character-knowledge-plugin"
    catalog.mkdir(parents=True, exist_ok=True)
    (catalog / "characters.json").write_text(
        json.dumps({"characters": characters}, ensure_ascii=False), encoding="utf-8"
    )
    return catalog


def test_identity_tags_extract_identity_only():
    r = role(cards=[
        "银白偏淡紫的长发、浅蓝灰眼睛、白色猫耳与蓬松猫尾。",
        "服装以白色和深海军蓝为主，带有褶边、蝴蝶结与宽檐尖顶帽。",
    ])
    tags = identity_tags(r, "小虚站在星空下")
    assert tags == ["silver hair", "subtle lavender hair tint", "long hair", "grey blue eyes", "white cat ears", "fluffy cat tail"]
    # 服装、道具不在词表内，不能被继承
    assert not any(t in tags for t in ("bow", "frills", "hat", "dress", "broom"))


def test_identity_tags_skip_features_the_request_already_states():
    tags = identity_tags(role(cards=["银白长发", "猫耳"]), "画小虚，要长发的")
    assert "long hair" not in tags
    assert tags == ["silver hair", "cat ears"]


def test_identity_tags_deduplicate_and_prefer_longest():
    assert identity_tags(role(cards=["银白长发", "银色长发其实是银白"]), "") == ["silver hair", "long hair"]
    assert identity_tags(role(cards=["浅蓝灰眼睛"]), "") == ["grey blue eyes"]


def test_strip_names_removes_name_alias_and_edge_punctuation():
    r = role(name="尤筠炫", aliases=["有晕眩"])
    assert strip_names("小虚，坐在窗边", [role()]) == "坐在窗边"
    assert strip_names("帮我画尤筠炫在看书", [r]) == "帮我画 在看书"
    assert strip_names("有晕眩跑了起来", [r]) == "跑了起来"


def test_strip_names_ignores_single_character_labels():
    assert strip_names("小虚子", [role(name="虚")]) == "小虚子"


def test_load_characters_matches_name_and_alias(tmp_path):
    data = tmp_path / "data"
    write_catalog(data, [
        {"name": "小虚", "appearance_cards": ["银白长发"]},
        {"name": "尤筠炫", "aliases": ["有晕眩"], "appearance_cards": ["浅棕短发"]},
    ])
    assert [r.name for r in load_characters(data, "画尤筠炫", "")] == ["尤筠炫"]
    assert [r.name for r in load_characters(data, "有晕眩在跑", "")] == ["尤筠炫"]
    assert load_characters(data, "今天天气真好", "") == []


def test_load_characters_private_library_overrides_shared(tmp_path):
    data = tmp_path / "data"
    catalog = write_catalog(data, [{"name": "小虚", "appearance_cards": ["共享版本"]}])
    digest = hashlib.sha256("12345".encode()).hexdigest()[:24]
    personal = catalog / "personal_characters" / digest
    personal.mkdir(parents=True)
    (personal / "characters.json").write_text(
        json.dumps({"characters": [{"name": "小虚", "appearance_cards": ["私人版本"]}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert [r.descriptions for r in load_characters(data, "画小虚", "12345")] == [("私人版本",)]
    # 别人的私人库不可见，只看得到共享条目
    assert [r.descriptions for r in load_characters(data, "画小虚", "999")] == [("共享版本",)]


def test_resolve_official_maps_chinese_names(tmp_path):
    index_file = tmp_path / "character_index.json"
    index_file.write_text(json.dumps({
        "version": 1, "category": 4,
        "rows": [
            {"name": "furina_(genshin_impact)", "zh": "芙宁娜", "post_count": 14095, "aliases": ["Furina"]},
            {"name": "genshin_impact", "zh": "原神", "post_count": 319705, "aliases": []},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    tags, cleaned = resolve_official(DanbooruTagIndex(index_file, category=4), "原神的芙宁娜站在雨中")
    assert set(tags) == {"furina_(genshin_impact)", "genshin_impact"}
    # 顺序跟着用户写的顺序走：先说作品就是作品在前。
    assert tags == ["genshin_impact", "furina_(genshin_impact)"]
    assert "芙宁娜" not in cleaned   # 命中的角色名剔除，避免模型翻错或重复
    assert "原神" in cleaned         # 作品名保留，让模型能纠正音译差异


def test_resolve_official_without_index_is_noop():
    assert resolve_official(None, "任意描述") == ([], "任意描述")


def test_resolve_official_matches_transliteration_variant(tmp_path):
    index_file = tmp_path / "character_index.json"
    index_file.write_text(json.dumps({"version": 1, "category": 4, "rows": [
        {"name": "sigrika_(wuthering_waves)", "zh": "西格莉卡", "post_count": 1252},
    ]}, ensure_ascii=False), encoding="utf-8")
    tags, cleaned = resolve_official(DanbooruTagIndex(index_file, category=4), "希格莉卡站在雪地里")
    assert tags == ["sigrika_(wuthering_waves)"]
    assert cleaned == "站在雪地里"


def test_resolve_official_drops_shorter_name_contained_in_longer(tmp_path):
    index_file = tmp_path / "character_index.json"
    index_file.write_text(json.dumps({"version": 1, "category": 4, "rows": [
        {"name": "sigrika_(wuthering_waves)", "zh": "西格莉卡", "post_count": 1252},
        {"name": "lucas_(mother_3)", "zh": "利卡", "post_count": 1011},
    ]}, ensure_ascii=False), encoding="utf-8")
    tags, _ = resolve_official(DanbooruTagIndex(index_file, category=4), "西格莉卡在跳舞")
    assert tags == ["sigrika_(wuthering_waves)"]


def _temp_index(tmp_path, rows):
    index_file = tmp_path / "character_index.json"
    index_file.write_text(json.dumps({"version": 1, "category": 4, "rows": rows}, ensure_ascii=False), encoding="utf-8")
    return DanbooruTagIndex(index_file, category=4)


def test_official_order_follows_the_prompt(tmp_path):
    # 顺序错会让第一位拿到别人的主参考图，画出来就是另一个角色。
    index = _temp_index(tmp_path, [
        {"name": "gojo_satoru", "zh": "五条悟", "post_count": 6163, "aliases": []},
        {"name": "ryoumen_sukuna_(jujutsu_kaisen)", "zh": "宿傩", "post_count": 2491, "aliases": []},
    ])
    assert resolve_official(index, "五条悟和宿傩")[0] == ["gojo_satoru", "ryoumen_sukuna_(jujutsu_kaisen)"]
    assert resolve_official(index, "宿傩和五条悟")[0] == ["ryoumen_sukuna_(jujutsu_kaisen)", "gojo_satoru"]


def test_one_alias_collapses_to_the_hottest_tag(tmp_path):
    # 「初音未来」同时挂在六个标签上；不收敛就会带出幽灵角色，挤掉真正的第二个。
    index = _temp_index(tmp_path, [
        {"name": "hatsune_miku", "zh": "初音未来", "post_count": 100000, "aliases": []},
        {"name": "magical_mirai_miku", "zh": "初音未来", "post_count": 3000, "aliases": []},
        {"name": "hatsune_miku_(append)", "zh": "初音未来", "post_count": 900, "aliases": []},
    ])
    assert resolve_official(index, "初音未来在唱歌")[0] == ["hatsune_miku"]


def test_short_alias_inside_a_longer_name_is_dropped(tmp_path):
    index = _temp_index(tmp_path, [
        {"name": "hatsune_miku", "zh": "初音未来", "post_count": 100000, "aliases": []},
        {"name": "mirai_(senran_kagura)", "zh": "未来", "post_count": 800, "aliases": []},
    ])
    assert resolve_official(index, "初音未来在唱歌")[0] == ["hatsune_miku"]


def test_full_character_composition_is_deterministic():
    r = role(cards=["银白偏淡紫的长发、浅蓝灰眼睛、白色猫耳与蓬松猫尾。", "服装以白色和深海军蓝为主。"])
    request = "小虚坐在咖啡馆看书"
    identity = identity_tags(r, request)
    cleaned = strip_names(request, [r])
    assert cleaned == "坐在咖啡馆看书"
    translated = "sitting in a cafe, reading a book"
    combined = merge_tags(identity, [part.strip() for part in translated.split(",")])
    assert combined == [
        "silver hair", "subtle lavender hair tint", "long hair", "grey blue eyes", "white cat ears", "fluffy cat tail",
        "sitting in a cafe", "reading a book",
    ]


def test_supplement_expands_abbreviations_and_events():
    from novelai_image.supplement import supplement_tags
    characters, scene, hits = supplement_tags("画五宿两人图")
    assert hits == ["五宿"]
    # 两个角色都要出来，场景栏为空——缩写就是「两个人」。
    assert characters == ["gojo_satoru", "ryoumen_sukuna_(jujutsu_kaisen)"]
    assert scene == []
    characters, scene, hits = supplement_tags("新宿大战")
    assert hits == ["新宿大战"]
    assert characters == ["gojo_satoru", "ryoumen_sukuna_(jujutsu_kaisen)"]
    for tag in ("jujutsu_kaisen", "ruins", "rubble", "battle", "night"):
        assert tag in scene
    # 场景标签不能进角色栏，否则会污染 char_caption。
    assert "ruins" not in characters
    assert supplement_tags("1girl, sitting on bed") == ([], [], [])


def _index_with(tmp_path, rows):
    index_file = tmp_path / "character_index.json"
    index_file.write_text(json.dumps({"version": 1, "category": 4, "rows": rows}, ensure_ascii=False), encoding="utf-8")
    return DanbooruTagIndex(index_file, category=4)


def test_tags_with_colon_are_loaded(tmp_path):
    # Danbooru 里 sin:_nanatsu_no_taizai、re:zero_... 这类名字很多。
    # 白名单漏了冒号会让整行被丢掉，索引里根本不存在。
    index = _index_with(tmp_path, [
        {"name": "lucifer_(sin:_nanatsu_no_taizai)", "zh": "路西法", "post_count": 32, "cat": 4},
    ])
    assert index.normalize(["lucifer_(sin:_nanatsu_no_taizai)"]) == ["lucifer_(sin:_nanatsu_no_taizai)"]


def test_variant_equal_names_keep_the_hottest(tmp_path):
    # 「雷姆」与「蕾姆」音译归一后同形。老写法会把两个都判成"被包含"，一个都不剩。
    index = _index_with(tmp_path, [
        {"name": "rem_(re:zero)", "zh": "雷姆", "post_count": 10361, "cat": 4},
        {"name": "rem_(death_note)", "zh": "蕾姆", "post_count": 162, "cat": 4},
    ])
    assert resolve_official(index, "画雷姆")[0] == ["rem_(re:zero)"]


def test_same_name_picks_the_work_named_in_the_prompt(tmp_path):
    # 两边的中文名都是「路西法」，靠本轮写出的作品名区分。
    index = _index_with(tmp_path, [
        {"name": "lucifer_(helltaker)", "zh": "路西法", "post_count": 2127, "cat": 4},
        {"name": "lucifer_(sin:_nanatsu_no_taizai)", "zh": "路西法", "post_count": 32, "cat": 4},
        {"name": "nanatsu_no_taizai", "zh": "七大罪", "post_count": 1356, "cat": 3},
    ])
    # resolve_official 是合并入口：角色在前、作品在后
    assert resolve_official(index, "七大罪的路西法")[0] == ["lucifer_(sin:_nanatsu_no_taizai)", "nanatsu_no_taizai"]
    # 没写作品名时按热度
    assert resolve_official(index, "路西法")[0] == ["lucifer_(helltaker)"]


def test_short_suffix_does_not_match_a_longer_work_name(tmp_path):
    # sp 是 springtime 的子串，反向包含会让 hatsune_miku_(sp) 顶掉 hatsune_miku。
    index = _index_with(tmp_path, [
        {"name": "hatsune_miku", "zh": "初音未来", "post_count": 147425, "cat": 4},
        {"name": "hatsune_miku_(sp)", "zh": "初音未来", "post_count": 32, "cat": 4},
        {"name": "springtime_(pierre_auguste_cot)", "zh": "春日", "post_count": 12, "cat": 3},
    ])
    assert resolve_official(index, "初音未来和春日")[0] == ["hatsune_miku"]
