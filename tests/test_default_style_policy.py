import asyncio
import json

import httpx

from novelai_image.default_style_policy import FIXED_STYLE_TAIL, normalize_default_style
from novelai_image.nanogpt import NanoGPTTranslator


def test_missing_or_variable_cloud_style_restores_same_original_tail():
    scene = ["masterpiece", "rain", "wet_hair", "wet_clothes", "puddle", "holding umbrella", "blue dress"]
    first = normalize_default_style(scene, "雨中穿蓝裙打伞")
    second = normalize_default_style(scene + ["anime_style", "cel_shading", "detailed_skin", "rim lighting",
                                             "artist:torino", "watercolor", "glossy highlights on skin and hair",
                                             "detailed eyes", "cool color palette", "soft_lighting", "3d render",
                                             "cgi render", "subsurface scattering", "skin specular"], "雨中穿蓝裙打伞")
    assert first == second == ["masterpiece", *FIXED_STYLE_TAIL, *scene[1:]]


def test_explicit_details_remain_scoped_to_requested_features():
    source = "雨中打伞，皮肤白皙，精细的眼睛，头发高光，水彩，轮廓光，artist:torino，soft shadows，丝绸连衣裙"
    tags = ["rain", "detailed_eyes", "hair highlights", "watercolor", "rim lighting", "artist:torino",
            "soft shadows", "silky dress", "detailed skin", "skin highlights", "glossy hair", "artist:rella"]
    result = normalize_default_style(tags, source)
    assert all(tag in result for tag in tags[:8])
    assert not set(tags[8:]) & set(result)


def test_explicit_negative_does_not_authorize_positive_or_fixed_style():
    result = normalize_default_style(["rain", "cel shading", "detailed skin", "watercolor"],
                                     "雨中，不要赛璐璐，不要精细皮肤，不要水彩")
    assert "cel shading" not in result
    assert "detailed skin" not in result and "watercolor" not in result
    assert "rain" in result


def test_fixed_tail_does_not_truncate_existing_sixty_content_tags():
    content = [f"scene fact {index}" for index in range(59)] + ["embroidered red dress"]
    result = normalize_default_style(content, "红色刺绣连衣裙")
    assert result == [*FIXED_STYLE_TAIL, *content]


def test_explicit_english_renderer_request_is_preserved():
    tags = ["3d render", "cgi render", "subsurface scattering", "skin specular"]
    assert normalize_default_style(tags, "女孩, " + ", ".join(tags)) == [*FIXED_STYLE_TAIL, *tags]


def test_policy_is_only_default_positive_and_preserves_legacy_budget(monkeypatch):
    monkeypatch.setenv("NANOGPT_API_KEY", "MOCK_ONLY_NOT_A_REAL_CREDENTIAL")
    captured = []

    def handler(request):
        payload = json.loads(request.content)
        captured.append(payload)
        inputs = json.loads(payload["messages"][1]["content"])
        result = {key: "rain, detailed skin, watercolor" for key in inputs}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps(result)}}]})

    translator = NanoGPTTranslator(transport=httpx.MockTransport(handler))
    default = asyncio.run(translator.translate_many({"positive": "雨中", "negative": "皮肤", "character": "甘雨"},
                                                   profile="default_20261015_1917"))
    assert default["positive"] == ", ".join([*FIXED_STYLE_TAIL, "rain"])
    assert default["negative"] == default["character"] == "rain, detailed skin, watercolor"
    assert captured[0]["max_tokens"] == 16384
    assert captured[0]["response_format"] == {"type": "json_object"}
    modern = asyncio.run(translator.translate_many({"positive": "雨中，水彩，精细皮肤"}))
    assert "watercolor" in modern["positive"]
    assert "cel shading" not in modern["positive"]
    assert "DEFAULT SOFT-LIGHT PROFILE OVERRIDE" not in captured[1]["messages"][0]["content"]
