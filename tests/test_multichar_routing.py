from types import SimpleNamespace
from unittest.mock import Mock
import asyncio
import json

import httpx
import pytest

from novelai_image.exceptions import NovelAIError
from novelai_image.characters import Character, resolve_official_split
from novelai_image.multichar import caption_payloads, enforce_character_count, expected_character_count, needs_character_split
from novelai_image.nanogpt import NanoGPTTranslator
from novelai_image.parser import parse_args
from novelai_image.plugin import NovelAIConfig, NovelAIImagePlugin


def make_plugin(tmp_path):
    obj = NovelAIImagePlugin()
    obj._plugin_config_instance = NovelAIConfig()
    obj._plugin_config_instance.translation.backend = "nanogpt"
    obj._plugin_config_instance.characters.enabled = False
    obj._ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path))
    obj._character_index = lambda: None
    return obj


@pytest.mark.parametrize("marker", ["不要看这个提示词", "原样把我的标签传入", "不要看这个提示词，直接原样把我的标签传入"])
def test_raw_bypasses_every_matcher_and_preserves_weights_quotes(tmp_path, marker):
    obj = make_plugin(tmp_path)
    obj._translator = Mock(side_effect=AssertionError("raw must not translate"))
    obj._character_index = Mock(side_effect=AssertionError("raw must not match index"))
    tags = '1girl,  1.35::artist:rella::, "silver hair", solo'
    req = parse_args(marker + "：" + tags + " --种子 123 --负面 bad hands")
    planned, payload, note, *_ = asyncio.run(obj._plan_request(req, "test", "qq"))
    assert planned.prompt == tags
    assert payload["input"] == tags
    assert payload["parameters"]["qualityToggle"] is False
    assert payload["parameters"]["negative_prompt"] == "bad hands"
    assert payload["parameters"]["seed"] == 123
    assert "直通" in note
    obj._translator.assert_not_called()
    obj._character_index.assert_not_called()


def test_raw_pipe_uses_official_character_fields_without_translation(tmp_path):
    obj = make_plugin(tmp_path)
    req = parse_args("原样把我的标签传入：2girls, rain | red hair, holding umbrella | blue hair, holding book")
    _, payload, *_ = asyncio.run(obj._plan_request(req, "test", "qq"))
    prompt = payload["parameters"]["v4_prompt"]
    assert prompt["caption"]["base_caption"] == "2girls, rain"
    assert [c["char_caption"] for c in prompt["caption"]["char_captions"]] == ["red hair, holding umbrella", "blue hair, holding book"]
    assert prompt["use_coords"] is False


def test_scene_chain_separates_clothes_props_negatives_and_coordinates(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOGPT_API_KEY", "MOCK_ONLY_NOT_A_REAL_CREDENTIAL")
    scene = {"interactions": [], "base": "2girls, rain, outdoors", "negative": "low quality",
             "characters": [
                 {"identity_id": "", "positive": "red hair, white dress, holding umbrella", "negative": "hat", "position": "left"},
                 {"identity_id": "", "positive": "blue hair, black dress, holding book", "negative": "glasses", "position": "right"}]}
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(scene)}}]})

    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler))
    obj = make_plugin(tmp_path)
    obj._translator = lambda: translator
    req = parse_args("两名女生，左边红发女孩穿白裙拿伞不要帽子，右边蓝发女孩穿黑裙拿书不要眼镜 --种子 123")
    _, payload, _, *_ = asyncio.run(obj._plan_request(req, "test", "qq"))
    positive = payload["parameters"]["v4_prompt"]
    negative = payload["parameters"]["v4_negative_prompt"]
    assert len(captured) == 1  # 拆分、翻译一次完成，不再走第二轮全局翻译。
    assert "red hair" not in positive["caption"]["base_caption"]
    chars = positive["caption"]["char_captions"]
    assert "holding umbrella" in chars[0]["char_caption"] and "blue hair" not in chars[0]["char_caption"]
    assert "holding book" in chars[1]["char_caption"] and "white dress" not in chars[1]["char_caption"]
    assert chars[0]["centers"][0]["x"] == 0.2
    assert chars[1]["centers"][0]["x"] == 0.8
    assert positive["use_coords"] is True
    for item, excluded in zip(negative["caption"]["char_captions"], ("hat", "glasses")):
        assert excluded in item["char_caption"].split(", ")
        assert "cropped" in item["char_caption"].split(", ")


def test_scene_rejects_omitted_known_character(monkeypatch):
    monkeypatch.setenv("NANOGPT_API_KEY", "MOCK_ONLY_NOT_A_REAL_CREDENTIAL")
    scene = {"interactions": [], "base": "2girls", "negative": "", "characters": [
        {"identity_id": "a", "positive": "red hair", "negative": "", "position": "auto"}]}
    translator = NanoGPTTranslator(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(scene)}}]})))
    with pytest.raises(NovelAIError, match="遗漏"):
        asyncio.run(translator.translate_scene("两位角色", "", [{"id": "a"}, {"id": "b"}]))


def test_position_defaults_and_generic_detection():
    assert needs_character_split("红发女孩和蓝发男孩一起散步")
    positive, negative = caption_payloads([{"positive": "red hair"}, {"positive": "blue hair"}])
    assert all(item["use_coords"] is False for item in positive + negative)
    with pytest.raises(NovelAIError, match="标记后"):
        parse_args("不要看这个提示词：")


def test_knowledge_and_official_character_share_separate_captions(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    obj = make_plugin(tmp_path)
    obj._plugin_config_instance.characters.enabled = True
    role = Character("小虚", (), ("银发，蓝瞳",), (), tmp_path / "catalog.json")
    monkeypatch.setattr("novelai_image.plugin.load_characters", lambda *args: [role])
    monkeypatch.setattr("novelai_image.plugin.resolve_official_split", lambda *args: (["ganyu_(genshin_impact)"], [], args[1]))
    reference = Mock(side_effect=AssertionError("multiple characters must not share a single identity reference"))
    monkeypatch.setattr("novelai_image.plugin.precise_reference", reference)
    translator = AsyncMock()
    translator.translate_scene.return_value = {"base": "2girls, outdoors", "negative": "low quality", "characters": [
        {"identity_id": "kb:0", "positive": "silver hair, blue eyes, white dress", "negative": "", "position": "left"},
        {"identity_id": "official:0", "positive": "ganyu_(genshin_impact), black dress", "negative": "", "position": "right"}]}
    obj._translator = lambda: translator
    _, payload, *_ = asyncio.run(obj._plan_request(parse_args("小虚穿白裙，甘雨穿黑裙"), "test", "qq"))
    supplied = translator.translate_scene.call_args.args[2]
    assert "silver hair" in supplied[0]["tags"]
    assert supplied[1]["tags"] == ["ganyu_(genshin_impact)"]
    captions = payload["parameters"]["v4_prompt"]["caption"]["char_captions"]
    assert len(captions) == 2 and "white dress" in captions[0]["char_caption"] and "black dress" in captions[1]["char_caption"]
    assert "director_reference_images" not in payload["parameters"]
    translator.translate_many.assert_not_awaited()


def test_explicit_per_character_props_survive_model_omission(monkeypatch):
    monkeypatch.setenv("NANOGPT_API_KEY", "MOCK_ONLY_NOT_A_REAL_CREDENTIAL")
    scene = {"interactions": [], "base": "2girls, park, ganyu_(genshin_impact)#left", "negative": "", "characters": [
        {"identity_id": "ganyu", "positive": "white dress", "negative": "", "position": "left"},
        {"identity_id": "elaina", "positive": "black dress", "negative": "", "position": "right"}]}
    translator = NanoGPTTranslator(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(scene)}}]})))
    identities = [{"id": "ganyu", "name": "ganyu_(genshin_impact)", "aliases": ["甘雨"], "tags": ["ganyu_(genshin_impact)"]},
                  {"id": "elaina", "name": "elaina_(majo_no_tabitabi)", "aliases": ["伊蕾娜"], "tags": ["elaina_(majo_no_tabitabi)"]}]
    result = asyncio.run(translator.translate_scene(
        "甘雨和伊蕾娜在公园。甘雨站左边，穿白色连衣裙，双手拿一本书；伊蕾娜站右边，穿黑色连衣裙，撑红色雨伞，不戴帽子。", "", identities))
    first, second = result["characters"]
    assert "holding book" in first["positive"] and "umbrella" not in first["positive"]
    assert "holding umbrella" in second["positive"] and "red umbrella" in second["positive"]
    assert "book" not in second["positive"] and second["negative"] == "hat"
    assert "ganyu" not in result["base"]


@pytest.mark.parametrize("text,count", [("1girl, 1boy", 2), ("三个女生和一个男生", 4), ("一男一女", 2), ("3girls", 3)])
def test_multiple_subject_counts_in_one_sentence(text, count):
    assert needs_character_split(text)
    assert expected_character_count(text) == count


def test_single_person_markers_cannot_override_independent_characters():
    tags = enforce_character_count("1girl, solo, rain", 2).split(", ")
    assert "1girl" not in tags and "solo" not in tags and "2people" in tags
    assert enforce_character_count("2girls, rain", 2) == "2girls, rain"
    assert enforce_character_count("two_women, rain", 2) == "2girls, rain"


def test_copyright_does_not_displace_roles_in_one_sentence():
    names = ["甲甲", "乙乙", "丙丙", "丁丁", "戊戊", "己己"]
    hits = [{"tag": "test_series", "zh": "作品", "cat": 3, "count": 1000}]
    hits += [{"tag": f"character_{i}_(test_series)", "zh": name, "cat": 4, "count": 1000} for i, name in enumerate(names)]
    index = SimpleNamespace(candidates=lambda text: hits)
    characters, works, _ = resolve_official_split(index, "作品中的" + "和".join(names) + "一起散步")
    assert len(characters) == 6 and works == ["test_series"]


@pytest.mark.parametrize("text,expected", [
    ("哆啦A梦一脚踹飞范马勇次郎", ["doraemon_(character)", "hanma_yujiro"]),
    ("哆啦A梦画风的范马勇次郎", ["hanma_yujiro"]),
    ("《哆啦A梦》中的范马勇次郎", ["hanma_yujiro"]),
    ("哆啦A梦里的范马勇次郎", ["hanma_yujiro"]),
])
def test_copyright_character_collision_uses_sentence_context(text, expected):
    hits = [{"zh": "哆啦A梦", "tag": "doraemon", "count": 2257, "cat": 3},
            {"zh": "哆啦A梦", "tag": "doraemon_(character)", "count": 1137, "cat": 4},
            {"zh": "范马勇次郎", "tag": "hanma_yujiro", "count": 92, "cat": 4}]
    characters, works, _ = resolve_official_split(SimpleNamespace(candidates=lambda text: hits), text)
    assert characters == expected and works == ["doraemon"]


def test_known_identity_deduplicates_without_phrase_specific_rules(monkeypatch):
    monkeypatch.setenv("NANOGPT_API_KEY", "MOCK_ONLY_NOT_A_REAL_CREDENTIAL")
    scene = {"interactions": [], "base": ["two_women"], "negative": [], "characters": [
        {"identity_id": "a", "positive": ["ganyu_(genshin_impact)"], "negative": [], "position": "auto"},
        {"identity_id": "", "positive": ["ganyu_(genshin_impact)"], "negative": [], "position": "auto"},
        {"identity_id": "b", "positive": ["hu_tao_(genshin_impact)", "ganyu"], "negative": [], "position": "auto"}]}
    def handler(request):
        data = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert data["negative"] == ""  # 已有英文负面由插件保留，无需翻译模型重复抄写。
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(scene)}}]})
    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler))
    identities = [{"id": "a", "name": "甘雨", "tags": ["ganyu_(genshin_impact)"]},
                  {"id": "b", "name": "胡桃", "tags": ["hu_tao_(genshin_impact)"]}]
    result = asyncio.run(translator.translate_scene("甘雨把手放在胡桃头上", "low quality", identities))
    assert len(result["characters"]) == 2 and result["negative"] == "low quality"
    assert "source#headpat" not in result["characters"][0]["positive"]
    assert "ganyu" not in result["characters"][1]["positive"]


def test_multi_default_inherits_same_locked_style_tail_as_single(tmp_path):
    from unittest.mock import AsyncMock
    from novelai_image.default_style_policy import MULTI_CEL_WEIGHT, V5_DEFAULT_MULTI_QUALITY
    from novelai_image.parser import MODEL_ALIASES
    obj = make_plugin(tmp_path)
    translator = AsyncMock()
    translator.translate_scene.return_value = {"base": "2girls, headpat", "negative": "low quality", "characters": [
        {"identity_id": "", "positive": "blue hair", "negative": "", "position": "auto"},
        {"identity_id": "", "positive": "brown hair", "negative": "", "position": "auto"}]}
    obj._translator = lambda: translator
    req = parse_args("两个女生摸头", default_model=MODEL_ALIASES["高清"])
    planned, *_ = asyncio.run(obj._plan_request(req, "test", "qq"))
    prompt_tags = planned.prompt.split(", ")
    for tag in V5_DEFAULT_MULTI_QUALITY:
        assert tag in prompt_tags
    assert f"{MULTI_CEL_WEIGHT:g}::cel shading::" in prompt_tags
    assert "cel shading" not in prompt_tags


def test_multi_default_puts_a_compact_style_core_in_each_character_caption(tmp_path):
    """角色栏各自决定区域渲染；画风只放 base 会被人数摊薄，默认柔光又压不住 3D 先验。

    但整段 44 标签会把 V5 共用的注意力预算吃光，动作标签失效——只放精简核心。
    """
    from unittest.mock import AsyncMock
    from novelai_image.default_style_policy import multichar_style_core
    from novelai_image.parser import MODEL_ALIASES
    obj = make_plugin(tmp_path)
    translator = AsyncMock()
    translator.translate_scene.return_value = {"base": "3girls, holding_hands", "negative": "low quality", "characters": [
        {"identity_id": "", "positive": "blue hair", "negative": "", "position": "left"},
        {"identity_id": "", "positive": "blonde hair", "negative": "", "position": "center"},
        {"identity_id": "", "positive": "brown hair", "negative": "", "position": "right"}]}
    obj._translator = lambda: translator
    req = parse_args("三个女生牵手", default_model=MODEL_ALIASES["高清"])
    _, payload, *_ = asyncio.run(obj._plan_request(req, "test", "qq"))
    caption = payload["parameters"]["v4_prompt"]["caption"]
    core = multichar_style_core()
    assert len(core) < 220, "核心必须够短，整段 44 标签会挤掉动作标签"
    assert len(caption["char_captions"]) == 3
    for item in caption["char_captions"]:
        assert item["char_caption"].startswith(core + ", ")
        # 角色内容（含动作）必须留在核心之后且完整。
        assert item["char_caption"].removeprefix(core + ", ").startswith("full body, ")


def test_multi_default_restores_the_realism_negatives_missing_from_the_hd_preset(tmp_path):
    """高清档默认负面只有 dramatic lighting / harsh shadows；普通档那组词一直没接上。

    负面不占正向注意力预算，所以补它不会像画风标签那样挤掉动作。
    """
    from unittest.mock import AsyncMock
    from novelai_image.default_style_policy import multichar_realism_negative
    from novelai_image.parser import MODEL_ALIASES
    obj = make_plugin(tmp_path)
    translator = AsyncMock()
    translator.translate_scene.return_value = {"base": "3girls, holding_hands", "negative": "low quality", "characters": [
        {"identity_id": "", "positive": "blue hair", "negative": "", "position": "left"},
        {"identity_id": "", "positive": "blonde hair", "negative": "", "position": "center"},
        {"identity_id": "", "positive": "brown hair", "negative": "", "position": "right"}]}
    obj._translator = lambda: translator
    planned, *_ = asyncio.run(obj._plan_request(
        parse_args("三个女生牵手", default_model=MODEL_ALIASES["高清"]), "lab", "qq"))
    assert multichar_realism_negative() in planned.negative

    # 单人不经过这里，负面保持原样。
    translator.translate_many = AsyncMock(return_value={"positive": "1girl, rain"})
    translator.translate_scene.return_value = {"base": "1girl, rain", "negative": "low quality", "characters": []}
    single_req, *_ = asyncio.run(obj._plan_request(
        parse_args("雨中打伞的伊蕾娜", default_model=MODEL_ALIASES["高清"]), "lab", "qq"))
    assert multichar_realism_negative() not in single_req.negative


def test_generic_slots_and_interaction_graph_without_phrase_rules(monkeypatch):
    monkeypatch.setenv("NANOGPT_API_KEY", "MOCK_ONLY_NOT_A_REAL_CREDENTIAL")
    scene = {"base": ["2girls"], "negative": [], "characters": {
        "a": {"positive": ["red_hair"], "negative": [], "position": "auto"},
        "b": {"positive": ["blue_hair"], "negative": [], "position": "auto"}},
        "interactions": [{"actor": "b", "target": "a", "action": "pushing"}]}
    def handler(request):
        schema = json.loads(request.content)["response_format"]["json_schema"]["schema"]["properties"]["characters"]
        assert schema["type"] == "object" and set(schema["properties"]) == {"a", "b"}
        assert schema["additionalProperties"] is False
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(scene)}}]})
    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler))
    result = asyncio.run(translator.translate_scene("乙推开甲", "", [{"id": "a", "name": "甲", "tags": []}, {"id": "b", "name": "乙", "tags": []}]))
    assert len(result["characters"]) == 2
    assert "target#pushing" in result["characters"][0]["positive"]
    assert "source#pushing" in result["characters"][1]["positive"]
    assert "pushing" not in result["characters"][0]["positive"].split(", ")
    assert "pushing" not in result["characters"][1]["positive"].split(", ")
