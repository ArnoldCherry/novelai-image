import asyncio
import json

import httpx
import pytest

from novelai_image.exceptions import NovelAIError
from novelai_image.nanogpt import NanoGPTTranslator
from novelai_image.tag_index import DanbooruTagIndex


def handler_for(content, captured=None, *, expand=True):
    def handler(request):
        if captured is not None:
            captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(content, ensure_ascii=False)},
        }]})
    return handler


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("NANOGPT_API_KEY", "MOCK_ONLY_NOT_A_REAL_CREDENTIAL")


def test_always_calls_model_so_tags_get_enriched(key):
    captured = {}
    translator = NanoGPTTranslator(transport=httpx.MockTransport(
        handler_for({"positive": "nude, solo, blush, best quality"}, captured)))

    # 「裸体」已被词表完全覆盖，仍必须请求模型，否则就没有任何补充标签。
    result = asyncio.run(translator.translate_many({"positive": "裸体"}))

    assert "nude" in result["positive"] and "solo" in result["positive"]
    assert "EXPAND it into concise" in captured["payload"]["messages"][0]["content"]


def test_explicit_tags_pass_through_unfiltered(key):
    translator = NanoGPTTranslator(transport=httpx.MockTransport(
        handler_for({"positive": "pussy, fingers in pussy, large breasts, best quality"})))
    result = asyncio.run(translator.translate_many({"positive": "用手指抠自己"}))
    assert "fingers in pussy" in result["positive"]


def test_missing_key_reports_configuration(monkeypatch):
    monkeypatch.setattr("novelai_image.nanogpt.resolve_nanogpt_key", lambda: "")
    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler_for({"positive": "cat"})))
    with pytest.raises(NovelAIError, match="NANOGPT_API_KEY"):
        asyncio.run(translator.translate_many({"positive": "猫"}))


def test_auth_failure_is_reported(key):
    def handler(request):
        return httpx.Response(401, json={})
    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler))
    with pytest.raises(NovelAIError, match="认证"):
        asyncio.run(translator.translate_many({"positive": "猫"}))


def test_multiple_indexes_supply_candidates(key, tmp_path):
    general = tmp_path / "index.json"
    general.write_text(json.dumps({"version": 1, "category": 0, "rows": [
        {"name": "wooden_table", "zh": "木桌", "post_count": 500},
    ]}, ensure_ascii=False), encoding="utf-8")
    character = tmp_path / "character_index.json"
    character.write_text(json.dumps({"version": 1, "category": 4, "rows": [
        {"name": "furina_(genshin_impact)", "zh": "芙宁娜", "post_count": 14095},
    ]}, ensure_ascii=False), encoding="utf-8")
    captured = {}
    translator = NanoGPTTranslator(
        transport=httpx.MockTransport(handler_for({"positive": "furina_(genshin_impact), wooden_table"}, captured)),
        tag_index=[DanbooruTagIndex(general), DanbooruTagIndex(character, category=4)],
    )

    result = asyncio.run(translator.translate_many({"positive": "芙宁娜坐在木桌旁"}))

    instruction = captured["payload"]["messages"][0]["content"]
    assert "furina_(genshin_impact)" in instruction and "wooden_table" in instruction
    assert "furina_(genshin_impact)" in result["positive"] and "wooden_table" in result["positive"]


def test_candidates_injected_and_result_normalized(key, tmp_path):
    index_file = tmp_path / "index.json"
    index_file.write_text(json.dumps({
        "version": 1, "category": 0,
        "rows": [{"name": "wooden_table", "zh": "木桌", "post_count": 500, "aliases": ["wooden table"]}],
    }, ensure_ascii=False), encoding="utf-8")
    captured = {}
    translator = NanoGPTTranslator(
        transport=httpx.MockTransport(handler_for({"positive": "wooden table"}, captured)),
        tag_index=DanbooruTagIndex(index_file),
    )

    result = asyncio.run(translator.translate_many({"positive": "木桌"}))

    # 「木桌」不在词表里，应该作为候选词典提示交给模型
    assert "wooden_table" in captured["payload"]["messages"][0]["content"]
    # 模型给的写法会被本地词典规范化成正式标签
    assert "wooden_table" in result["positive"].split(", ")


def test_short_scene_has_suggested_budget_without_minimum(key):
    captured = {}
    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler_for({"positive": ["rain", "wet clothes"], "negative": "blurry"}, captured)))
    result = asyncio.run(translator.translate_many({"positive": "雨中湿身", "negative": "模糊"}))
    assert result["positive"] == "rain, wet clothes"
    assert result["negative"] == "blurry"
    schema = captured["payload"]["response_format"]["json_schema"]["schema"]["properties"]["positive"]
    assert "minItems" not in schema and "maxItems" not in schema
    assert "enum" not in schema["items"]
    assert captured["payload"]["max_tokens"] == 1024
    assert "Sparse prompts: aim for roughly 18-28" in captured["payload"]["messages"][0]["content"]


def test_character_has_no_minimum_and_selected_style_disables_default(key):
    captured = {}
    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler_for({"character": "ganyu_(genshin_impact)"}, captured)))
    result = asyncio.run(translator.translate_many({"character": "甘雨"}, style_context="预设画风：赛璐璐"))
    instruction = captured["payload"]["messages"][0]["content"]
    assert "Default art style when" not in instruction
    assert "Do not add default gacha-game art" in instruction
    assert "Never invent clothing, props" in instruction
    assert result["character"] == "ganyu_(genshin_impact)"


def test_completeness_guidance_preserves_all_explicit_details(key):
    from novelai_image.nanogpt import content_tag_budget
    guidance = content_tag_budget("雨中湿身")
    assert "Sparse prompts: aim for roughly 18-28" in guidance
    assert "Detailed prompts: faithfully" in guidance
    assert "no target count" in guidance
    assert content_tag_budget("雨" * 121) == guidance
    tags = [f"detail_{i}" for i in range(70)]
    captured = {}
    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler_for({"positive": tags}, captured)))
    assert len(asyncio.run(translator.translate_many({"positive": "细节" * 100}))["positive"].split(", ")) == 70
    assert captured["payload"]["max_tokens"] == 2048


def test_style_isolated_but_wet_translucency_allowed(key):
    content = ["wet hair", "wet clothes", "translucent fabric", "cel shading", "artist:torino", "digital illustration"]
    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler_for({"positive": content})))
    tags = asyncio.run(translator.translate_many({"positive": "雨中湿身"}, style_context="watercolor"))["positive"].split(", ")
    assert "translucent fabric" in tags
    assert not {"cel shading", "artist:torino", "digital illustration"} & set(tags)


def test_renderer_detail_expansion_is_filtered_but_rain_and_wet_clothing_survive(key):
    generated = [
        "rain", "wet clothes", "water droplets", "clinging fabric", "translucent fabric",
        "detailed skin", "detailed face", "detailed hair", "beautiful detailed eyes",
        "iris texture", "detailed eyelashes", "specular highlights", "glossy skin",
        "porcelain skin", "silky hair",
    ]
    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler_for({"positive": generated})))
    tags = set(asyncio.run(translator.translate_many({"positive": "雨中湿衣"})) ["positive"].split(", "))
    assert {"rain", "wet clothes", "water droplets", "clinging fabric", "translucent fabric"} <= tags
    assert not {
        "detailed skin", "detailed face", "detailed hair", "beautiful detailed eyes",
        "iris texture", "detailed eyelashes", "specular highlights", "glossy skin",
        "porcelain skin", "silky hair",
    } & tags


def test_explicit_appearance_detail_and_style_are_preserved(key):
    translator = NanoGPTTranslator(transport=httpx.MockTransport(
        handler_for({"positive": ["rain", "detailed eyes", "watercolor"]})))
    tags = asyncio.run(translator.translate_many(
        {"positive": "雨中，细致的眼睛，水彩风格"}, style_context="本轮描述"))["positive"].split(", ")
    assert {"rain", "detailed eyes", "watercolor"} <= set(tags)


@pytest.mark.parametrize("source,expected", [
    ("眼睛高光", {"eye highlights"}),
    ("虹膜高光", {"iris highlights"}),
    ("不要头发高光，眼睛高光", {"eye highlights"}),
    ("不要眼睛高光，头发高光", {"hair highlights"}),
    ("女孩站立", set()),
])
def test_renderer_highlights_require_the_explicit_feature(key, source, expected):
    details = {"eye highlights", "iris highlights", "hair highlights", "skin specular highlights"}
    translator = NanoGPTTranslator(transport=httpx.MockTransport(
        handler_for({"positive": sorted(details) + ["standing"]})))
    tags = set(asyncio.run(translator.translate_many(
        {"positive": source}, style_context="artist:rella"))["positive"].split(", "))
    assert tags & details == expected
    assert "standing" in tags


@pytest.mark.parametrize("source,expected", [
    ("景深明显，背景散景", {"depth of field", "bokeh"}),
    ("清晰对焦的女孩", {"sharp focus"}),
    ("女孩，depth of field, bokeh", {"depth of field", "bokeh"}),
    ("不要散景，女孩站立", set()),
    ("女孩站立", set()),
])
def test_explicit_focus_translation_survives_selected_style(key, source, expected):
    focus = {"depth of field", "bokeh", "sharp focus"}
    translator = NanoGPTTranslator(transport=httpx.MockTransport(
        handler_for({"positive": sorted(focus) + ["standing", "cel shading"]})))
    tags = set(asyncio.run(translator.translate_many(
        {"positive": source}, style_context="artist:rella"))["positive"].split(", "))
    assert tags & focus == expected
    assert "cel shading" not in tags


@pytest.mark.parametrize("context", ["", "本轮描述", "artist:rella"])
@pytest.mark.parametrize("source,expected", [
    ("自定义水墨画风", set()),
    ("画师风格的水墨画", set()),
    ("水墨画，artist:someone unknown", {"artist:someone unknown"}),
    ("水墨画，artist:someone unknown，女孩", {"artist:someone unknown"}),
    ("水墨画，torino aqua", {"artist:torino aqua"}),
    ("水墨画，画师torino", {"artist:torino aqua"}),
    ("水墨画，不要torino aqua", set()),
])
def test_artist_requires_explicit_name_for_every_style_context(key, context, source, expected):
    artists = {"artist:torino aqua", "artist:someone unknown"}
    translator = NanoGPTTranslator(transport=httpx.MockTransport(
        handler_for({"positive": sorted(artists) + ["ink drawing", "watercolor", "detailed skin"]})))
    tags = set(asyncio.run(translator.translate_many(
        {"positive": source}, style_context=context))["positive"].split(", "))
    assert tags & artists == expected
    assert "ink drawing" in tags
    assert "detailed skin" not in tags


def test_format_retry_temperature_drops_by_point_one(key):
    temperatures = []

    def handler(request):
        payload = json.loads(request.content)
        temperatures.append(payload["temperature"])
        content = {"positive": "中文"} if len(temperatures) == 1 else {"positive": ["rain", "standing"]}
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(content, ensure_ascii=False)},
        }]})

    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler))
    result = asyncio.run(translator.translate_many({"positive": "雨中站立"}))
    assert result["positive"] == "rain, standing"
    assert temperatures == [0.1, 0.1]


@pytest.mark.parametrize("constraint", ["不要透明衣料", "衣料不透明", "opaque clothing", "no transparency"])
def test_explicit_opaque_constraint_rejects_translucent_inference(key, constraint):
    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler_for({"positive": ["wet clothes", "opaque fabric", "translucent fabric", "see_through"]})))
    tags = asyncio.run(translator.translate_many({"positive": "雨中湿身，" + constraint}))["positive"].split(", ")
    assert "opaque fabric" in tags
    assert "translucent fabric" not in tags and "see_through" not in tags


def test_non_latin_response_is_rejected_even_when_schema_is_ignored(key):
    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler_for({"positive": ["дождь"] + [f"detail_{i}" for i in range(54)]}, expand=False)))
    with pytest.raises(NovelAIError, match="非拉丁"):
        asyncio.run(translator.translate_many({"positive": "雨中"}))


def test_explicit_scene_lighting_survives_style_isolation(key):
    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler_for({"positive": ["soft lighting", "diffuse daylight", "overcast sky"] + [f"detail_{i}" for i in range(50)]}, expand=False)))
    result = asyncio.run(translator.translate_many({"positive": "雨天柔和光照"}, style_context="watercolor"))["positive"].split(", ")
    assert {"soft lighting", "diffuse daylight", "overcast sky"} <= set(result)


@pytest.mark.parametrize("source,expected", [
    ("雨中打伞的甘雨", {"umbrella", "holding umbrella"}),
    ("雨中撑着伞的甘雨", {"umbrella", "holding umbrella"}),
    ("甘雨穿衬衫和短裙", {"shirt", "skirt"}),
    ("甘雨穿连衣裙和长袖", {"dress", "long sleeves"}),
    ("甘雨 wearing shirt, umbrella", {"shirt", "umbrella"}),
])
def test_explicit_clothing_and_props_survive_chinese_pipeline(key, source, expected):
    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler_for({"positive": ["rain", "umbrella", "holding umbrella", "shirt", "skirt", "dress", "long sleeves", "underwear"]})))
    tags = set(asyncio.run(translator.translate_many({"positive": source}, style_context="watercolor"))["positive"].split(", "))
    assert expected <= tags
    assert "underwear" not in tags


@pytest.mark.parametrize("source", ["雨中甘雨，不要伞", "雨中甘雨，不打伞", "打伞的甘雨，但不要伞", "甘雨，不穿裙子", "甘雨 no umbrella"])
def test_negated_glossary_never_forces_positive_content(key, source):
    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler_for({"positive": ["rain", "umbrella", "holding umbrella", "skirt"]})))
    tags = set(asyncio.run(translator.translate_many({"positive": source}))["positive"].split(", "))
    assert not {"umbrella", "holding umbrella", "skirt"} & tags


def test_explicit_artist_is_preserved_with_selected_style(key):
    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler_for({"positive": ["rain", "artist:torino", "cel shading"]})))
    tags = asyncio.run(translator.translate_many({"positive": "雨中甘雨，artist:torino"}, style_context="watercolor"))["positive"].split(", ")
    assert "artist:torino" in tags
    assert "cel shading" not in tags


def test_format_retry_shares_total_deadline(key, caplog):
    calls = []

    async def handler(request):
        calls.append(request.extensions["timeout"])
        await asyncio.sleep(0.08)
        return httpx.Response(200, json={"choices": [{"finish_reason": "length"}],
                                        "usage": {"completion_tokens": 1024}})

    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler))
    translator.timeout = 0.12

    async def run():
        started = asyncio.get_running_loop().time()
        with pytest.raises(NovelAIError, match="总耗时超出上限"):
            await translator.translate_many({"positive": "保密输入"})
        return asyncio.get_running_loop().time() - started

    with caplog.at_level("INFO", logger="novelai_image.nanogpt"):
        elapsed = asyncio.run(run())
    assert len(calls) == 2
    assert calls[1]["read"] < calls[0]["read"]
    assert elapsed < 0.3  # 留出调度余量；异常断言同时确保第二次响应未完成。
    assert "请求开始 bytes=" in caplog.text
    assert "响应头 elapsed=" in caplog.text
    assert "finish_reason=length" in caplog.text
    assert "completion_tokens" in caplog.text
    assert "stage=等待响应头 type=TimeoutError" in caplog.text
    assert "保密输入" not in caplog.text
    assert "MOCK_ONLY_NOT_A_REAL_CREDENTIAL" not in caplog.text


@pytest.mark.parametrize(("error", "message"), [
    (httpx.ConnectTimeout, "连接超时"),
    (httpx.ReadTimeout, "响应超时"),
])
def test_timeout_stage_and_type_are_logged(key, caplog, error, message):
    def handler(request):
        assert request.extensions["timeout"]["connect"] <= 10
        raise error("sensitive provider detail")

    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler))
    with caplog.at_level("INFO", logger="novelai_image.nanogpt"):
        with pytest.raises(NovelAIError, match=message):
            asyncio.run(translator.translate_many({"positive": "保密输入"}))
    assert f"type={error.__name__}" in caplog.text
    assert "stage=等待响应头" in caplog.text
    assert "sensitive provider detail" not in caplog.text


def test_response_body_deadline_reports_read_stage(key, caplog):
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.1)
            yield b"{}"

    translator = NanoGPTTranslator(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=SlowBody())))
    translator.timeout = 0.02
    with caplog.at_level("INFO", logger="novelai_image.nanogpt"):
        with pytest.raises(NovelAIError, match="总耗时超出上限"):
            asyncio.run(translator.translate_many({"positive": "猫"}))
    assert "响应头 elapsed=" in caplog.text
    assert "stage=读取响应正文 type=TimeoutError" in caplog.text


def test_sparse_enrichment_inferences_are_not_filtered_or_replaced_by_style(key):
    scene = ["standing", "hand near chest", "head tilted", "parted lips", "wet ground", "puddle", "ripples", "overcast sky", "clinging fabric", "translucent fabric", "water dripping"]
    captured = {}
    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler_for({"positive": scene + ["artist:rella", "cel shading"]}, captured)))
    tags = set(asyncio.run(translator.translate_many({"positive": "雨中淋湿的甘雨"}, style_context="artist:rella"))["positive"].split(", "))
    assert set(scene) <= tags
    assert not {"artist:rella", "cel shading"} & tags
    instruction = captured["payload"]["messages"][0]["content"]
    assert "Sparse-scene example" in instruction
    assert "require FOUR separate array entries" in instruction
    assert "complete moment" in instruction


def test_historical_default_keeps_original_protocol_and_fixes_style_tail(key):
    captured = {}
    tags = ["cel shading", "glossy highlights", "detailed hair", "detailed skin", "anime style"]
    tags += [f"scene tag {i}" for i in range(70)]
    translator = NanoGPTTranslator(transport=httpx.MockTransport(
        handler_for({"positive": ", ".join(tags)}, captured)))
    result = asyncio.run(translator.translate_many(
        {"positive": "女孩站在雨中"}, profile="default_20261015_1917",
        style_context="must not reach legacy", content_context="must not reach legacy"))
    payload = captured["payload"]
    assert payload["temperature"] == 0.1 and payload["frequency_penalty"] == 0.2
    assert payload["max_tokens"] == 16384
    assert payload["response_format"] == {"type": "json_object"}
    instruction = payload["messages"][0]["content"]
    assert "Aim for 30-45 tags and NEVER exceed 60" in instruction
    assert "Default art style" in instruction and "cel shading with soft gradients" in instruction
    assert "must not reach legacy" not in instruction
    assert all(tag in result["positive"] for tag in ("anime style", "cel shading", "soft_lighting", "detailed"))
    assert not any(tag in result["positive"] for tag in ("glossy highlights", "detailed hair", "detailed skin"))
    assert "DEFAULT SOFT-LIGHT PROFILE OVERRIDE" in instruction
    assert len(result["positive"].split(", ")) == 59  # 旧链先截 60，去掉 5 个浮动画风词后补固定 4 个。


def test_historical_default_snapshots_match_manifest():
    from pathlib import Path
    import hashlib
    manifest_dir = Path(__file__).resolve().parents[1] / "artifacts" / "default_20261015_1917_source"
    manifest = json.loads((manifest_dir / "manifest.json").read_text(encoding="utf-8"))
    for source, details in manifest["files"].items():
        assert hashlib.sha256((manifest_dir / source).read_bytes()).hexdigest() == details["snapshot_sha256"]
        runtime = manifest_dir.parents[1] / details["runtime_file"]
        assert hashlib.sha256(runtime.read_bytes()).hexdigest() == details["runtime_sha256"]
