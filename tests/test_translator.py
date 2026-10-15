import asyncio
import json

import httpx
import pytest

from novelai_image.exceptions import NovelAIError
from novelai_image.translator import _SYSTEM_PROMPT, OllamaTranslator, validate_english_prompt


def build(handler, **kwargs):
    return OllamaTranslator(transport=httpx.MockTransport(handler), **kwargs)


def reply(content, done_reason="stop"):
    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json={
            "message": {"role": "assistant", "content": json.dumps(content, ensure_ascii=False)},
            "done_reason": done_reason,
        })
    return handler


def test_translate_many_sends_system_prompt_and_glossary_first():
    captured = {}

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={
            "message": {"role": "assistant", "content": json.dumps({"positive": "standing by the window", "negative": "blurry"})},
            "done_reason": "stop",
        })

    result = asyncio.run(build(handler).translate_many({"positive": "裸体站在窗边", "negative": "模糊"}))
    # 词表命中「裸体」在前，模型结果在后
    assert result["positive"] == "nude, standing by the window"
    assert result["negative"] == "blurry"
    assert captured["messages"][0]["content"] == _SYSTEM_PROMPT
    # 已由词表处理的「裸体」不应再发给模型
    assert "裸体" not in json.loads(captured["messages"][1]["content"])["positive"]


def test_english_only_skips_model_entirely():
    def handler(request):
        raise AssertionError("纯英文不应触达模型")

    assert asyncio.run(build(handler).translate_many({"positive": "castle, sunset"})) == {}


def test_fallback_model_runs_when_primary_returns_chinese():
    calls = []

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        model = json.loads(request.content)["model"]
        calls.append(model)
        content = {"positive": "仍含中文"} if model == "primary" else {"positive": "clean tags"}
        return httpx.Response(200, json={"message": {"role": "assistant", "content": json.dumps(content, ensure_ascii=False)}, "done_reason": "stop"})

    result = asyncio.run(build(handler, model="primary", fallback_model="backup").translate_many({"positive": "中文"}))
    assert result["positive"] == "clean tags"
    assert calls == ["primary", "backup"]


def test_key_mismatch_rejected():
    with pytest.raises(NovelAIError, match="不匹配"):
        asyncio.run(build(reply({"wrong": "key"})).translate_many({"positive": "中文"}))


def test_length_limit_rejected():
    with pytest.raises(NovelAIError, match="长度上限"):
        asyncio.run(build(reply({"positive": "x"}, done_reason="length")).translate_many({"positive": "中文"}))


def test_thinking_content_rejected():
    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json={
            "message": {"role": "assistant", "content": json.dumps({"positive": "cat"}), "thinking": "hmm"},
            "done_reason": "stop",
        })

    with pytest.raises(NovelAIError, match="无效内容"):
        asyncio.run(build(handler).translate_many({"positive": "猫"}))


def test_colon_inside_a_tag_is_accepted():
    # Danbooru 的 re:zero、sin:_nanatsu_no_taizai 自带冒号。一律拒绝会让
    # 凡涉及这些作品的中文提示词整条失败（翻译模型会把它们写进正面提示词）。
    assert validate_english_prompt("1girl, rem_(re:zero), re:zero, window") == "1girl, rem_(re:zero), re:zero, window"
    assert "lucifer_(sin:_nanatsu_no_taizai)" in validate_english_prompt("1girl, lucifer_(sin:_nanatsu_no_taizai)")


def test_prose_colons_and_sentence_marks_are_still_rejected():
    for text in ("1girl, sitting. looking at viewer", "prompt: 1girl, solo", "1girl, notes: something"):
        with pytest.raises(NovelAIError):
            validate_english_prompt(text)
