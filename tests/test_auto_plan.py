from pathlib import Path
from unittest.mock import AsyncMock
import asyncio
import json
import pytest
from novelai_image.characters import Character, identity_tags, strip_names
from novelai_image.parser import parse_args
from novelai_image.plugin import default_multichar_style_head
from novelai_image.tag_index import DanbooruTagIndex
from test_plugin import plugin


def test_rainy_scene_does_not_add_tenma_identity(tmp_path, monkeypatch):
    index_file = tmp_path / "character_index.json"
    index_file.write_text(json.dumps({"version": 1, "category": 4, "rows": [
        {"name": "matsukaze_tenma", "zh": "松风天马", "aliases": ["雨天"], "post_count": 694, "cat": 4},
        {"name": "elaina_(majo_no_tabitabi)", "zh": "伊蕾娜", "post_count": 1000, "cat": 4},
        {"name": "topaz_(honkai:_star_rail)", "zh": "托帕", "post_count": 1000, "cat": 4},
    ]}, ensure_ascii=False), encoding="utf-8")
    obj = plugin()
    monkeypatch.setattr(obj, "_tag_indexes", lambda: [DanbooruTagIndex(index_file, category=4)])
    translator = AsyncMock()
    translator.translate_many.return_value = {"positive": "rain, holding umbrella"}
    monkeypatch.setattr(obj, "_translator", lambda: translator)
    for prompt, expected in [
        ("雨天打伞的伊蕾娜", "elaina_(majo_no_tabitabi)"),
        ("雨天大伞的托帕", "topaz_(honkai:_star_rail)"),
        ("雨天打伞的松风天马", "matsukaze_tenma"),
    ]:
        _, payload, *_ = asyncio.run(obj._plan_request(parse_args(prompt), "a", "qq"))
        captions = payload["parameters"]["v4_prompt"]["caption"]["char_captions"]
        assert [item["char_caption"] for item in captions] == [expected]

@pytest.mark.parametrize("prompt,negative,mode,strength,fidelity", [
    ("Alice", "", "character", .8, .55),
    ("Alice watercolor", "", "character", .8, .55),
    ("Alice", "watercolor", "character", .8, .55),
    ("Alice running in a uniform", "", "character", .8, .35),
    ("Alice blue hair, red eyes", "", "character", .35, .15),
])
def test_auto_plan(prompt, negative, mode, strength, fidelity, monkeypatch):
    role = Character("Alice", (), ("银发，长发，蓝瞳",), (), Path("catalog.json"))
    monkeypatch.setattr("novelai_image.plugin.load_characters", lambda *a: [role])
    monkeypatch.setattr("novelai_image.plugin.precise_reference", lambda *a: "mock_reference")
    obj = plugin()
    translator = AsyncMock()
    monkeypatch.setattr(obj, "_translator", lambda: translator)
    req = parse_args(prompt + (" --negative " + negative if negative else ""))
    final, payload, note, actual, value, secondary = asyncio.run(obj._plan_request(req, "a", "qq"))
    assert (value, secondary) == (strength, fidelity)
    assert payload["parameters"]["director_reference_descriptions"][0]["caption"]["base_caption"] == mode
    assert "Alice" not in final.prompt
    translator.translate_many.assert_not_called()
    if "blue hair" in prompt:
        assert "silver hair" not in final.prompt
        assert "blue eyes" not in final.prompt


def test_style_keyword_does_not_match_an_embedded_character_alias(tmp_path, monkeypatch):
    index_file = tmp_path / "character_index.json"
    index_file.write_text(
        '{"version":1,"category":4,"rows":['
        '{"name":"ganyu_(genshin_impact)","zh":"甘雨","post_count":20278,"cat":4},'
        '{"name":"lulu_(league)","zh":"璐璐","post_count":1085,"cat":4}]}' ,
        encoding="utf-8",
    )
    obj = plugin()
    monkeypatch.setattr(obj, "_tag_indexes", lambda: [DanbooruTagIndex(index_file, category=4)])
    translator = AsyncMock()
    translator.translate_many.return_value = {"positive": "rain, wet clothes, cel shading"}
    monkeypatch.setattr(obj, "_translator", lambda: translator)

    _, cel_payload, cel_note, *_ = asyncio.run(
        obj._plan_request(parse_args("雨中淋湿的甘雨，赛璐璐"), "a", "qq"))
    cel_captions = cel_payload["parameters"]["v4_prompt"]["caption"]["char_captions"]
    assert [item["char_caption"] for item in cel_captions] == ["ganyu_(genshin_impact)"]
    assert "预设画风：赛璐璐" in cel_note

    translator.translate_scene.return_value = {
        "base": "2girls, standing", "negative": "low quality",
        "characters": [{"identity_id": "official:0", "positive": "ganyu_(genshin_impact)", "negative": "", "position": "auto"},
                       {"identity_id": "official:1", "positive": "lulu_(league)", "negative": "", "position": "auto"}]}
    _, character_payload, *_ = asyncio.run(
        obj._plan_request(parse_args("甘雨和璐璐并肩站着"), "a", "qq"))
    character_captions = character_payload["parameters"]["v4_prompt"]["caption"]["char_captions"]
    character_base = character_payload["parameters"]["v4_prompt"]["caption"]["base_caption"]
    head = default_multichar_style_head(character_base)
    assert head
    assert [item["char_caption"].removeprefix(head + ", ") for item in character_captions] == [
        "full body, ganyu_(genshin_impact)", "full body, lulu_(league)"]


def test_preview_no_paid_call_no_token(monkeypatch):
    monkeypatch.delenv("NOVELAI_API_TOKEN", raising=False)
    generate = AsyncMock()
    monkeypatch.setattr("novelai_image.plugin.NovelAIClient.generate", generate)
    obj = plugin()
    asyncio.run(obj.preview(stream_id="s", user_id="a", platform="qq", matched_groups={"args":"castle"}))
    generate.assert_not_called()
    assert "未调用 NovelAI 生成" in obj._ctx.send.text.call_args.args[0]


def test_name_only_and_remaining_translation(monkeypatch):
    role = Character("小雪", ("Alice",), ("银发，蓝瞳",), (), Path("catalog.json"))
    monkeypatch.setattr("novelai_image.plugin.load_characters", lambda *a: [role])
    monkeypatch.setattr("novelai_image.plugin.precise_reference", lambda *a: None)
    obj = plugin()
    translator = AsyncMock()
    translator.translate_many.return_value = {"positive": "sitting beside a window"}
    monkeypatch.setattr(obj, "_translator", lambda: translator)
    asyncio.run(obj._plan_request(parse_args("小雪"), "a", "qq"))
    translator.translate_many.assert_not_called()
    asyncio.run(obj._plan_request(parse_args("小雪 坐在窗边"), "a", "qq"))
    assert translator.translate_many.call_args.args[0] == {"positive":"坐在窗边"}
    assert strip_names("ALICE", [role]) == ""


@pytest.mark.parametrize("platform,user,args,expected", [
    ("", "a", "castle", "识别发送者"),
    ("qq", "", "castle", "识别发送者"),
])
def test_preview_permissions(platform, user, args, expected, monkeypatch):
    generate = AsyncMock()
    monkeypatch.setattr("novelai_image.plugin.NovelAIClient.generate", generate)
    obj = plugin()
    asyncio.run(obj.preview(stream_id="s", user_id=user, platform=platform, matched_groups={"args":args}))
    assert expected in obj._ctx.send.text.call_args.args[0]
    generate.assert_not_called()


def test_preview_hd_allowed_on_non_qq(monkeypatch):
    generate = AsyncMock()
    monkeypatch.setattr("novelai_image.plugin.NovelAIClient.generate", generate)
    obj = plugin()
    asyncio.run(obj.preview(stream_id="s", user_id="other", platform="discord", matched_groups={"args":"castle --模型 高清"}))
    assert "nai-diffusion-5-full" in obj._ctx.send.text.call_args.args[0]
    generate.assert_not_called()


@pytest.mark.parametrize("model,count", [("nai-diffusion-5-full", 1), ("nai-diffusion-4-5-full", 2)])
def test_unsupported_or_multi_reference_text_only(model, count, monkeypatch):
    role = Character("Alice", (), ("银发",), (), Path("catalog.json"))
    monkeypatch.setattr("novelai_image.plugin.load_characters", lambda *a: [role] * count)
    reference = AsyncMock()
    monkeypatch.setattr("novelai_image.plugin.precise_reference", reference)
    obj = plugin()
    translator = AsyncMock()
    translator.translate_scene.return_value = {"base": "2girls", "negative": "low quality", "characters": [
        {"identity_id": f"kb:{i}", "positive": "silver hair", "negative": "", "position": "auto"} for i in range(count)]}
    monkeypatch.setattr(obj, "_translator", lambda: translator)
    req = parse_args("Alice", default_model=model)
    _, payload, note, mode, _, _ = asyncio.run(obj._plan_request(req, "a", "qq"))
    assert mode == "文字"
    assert "director_reference_images" not in payload["parameters"]
    reference.assert_not_called()
