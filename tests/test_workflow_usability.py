from base64 import b64decode, b64encode
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import json

from PIL import Image
import pytest

from novelai_image.client import build_payload
from novelai_image.exceptions import NovelAIError
from novelai_image.local_edit import infer_single_identity, parse_location, prepare_text_edit, preserve_outside
from novelai_image.parser import MODEL_ALIASES, DrawRequest, parse_args
from novelai_image.plugin import NovelAIConfig
from novelai_image.tag_index import is_ambiguous_hit
from novelai_image.workflow import WorkflowMixin, workflow_parts


def image_data(color="red"):
    buffer = BytesIO()
    Image.new("RGB", (32, 48), color).save(buffer, "PNG")
    return b64encode(buffer.getvalue()).decode("ascii")


def payload():
    req = DrawRequest("portrait, red scarf, soft shading", "low quality", "nai-diffusion-5-full", 32, 48, 28, 123, 1)
    return build_payload(req, 5.5, "k_euler_ancestral", char_captions=["ganyu_(genshin_impact), blue hair"])


@pytest.mark.parametrize("op,args,expected", [
    ("修改", "回复 红围巾-蓝围巾", ["回复", "红围巾-蓝围巾"]),
    ("局部", "回复 把脖子上的围巾改蓝", ["回复", "把脖子上的围巾改蓝"]),
    ("系列", "回复 花园读书-海边散步-窗边喝茶", ["回复", "花园读书", "海边散步", "窗边喝茶"]),
    ("系列", "回复 close-up, t-shirt - full body, seaside", ["回复", "close-up, t-shirt", "full body, seaside"]),
    ("对比", "赛璐璐-水彩 甘雨在花园里读书", ["赛璐璐", "水彩", "甘雨在花园里读书"]),
    ("导演", "回复 表情 开心微笑", ["回复", "表情", "开心微笑"]),
    ("收藏", "甘雨水彩 回复", ["甘雨水彩", "回复"]),
    ("套用", '"甘雨 水彩" 甘雨在窗边喝茶', ["甘雨 水彩", "甘雨在窗边喝茶"]),
])
def test_new_command_separators(op, args, expected):
    assert workflow_parts(op, args) == expected


def test_tag_hyphen_does_not_turn_a_tshirt_into_a_replacement():
    mixin = WorkflowMixin()
    original = payload()
    changed = asyncio.run(mixin._workflow_edit(original, "t-shirt"))
    assert "red scarf" in changed["input"]
    assert changed["input"].endswith("t-shirt")
    replaced = asyncio.run(mixin._workflow_edit(original, "red scarf-blue scarf"))
    assert "red scarf" not in replaced["input"] and "blue scarf" in replaced["input"]
    assert "soft shading" in replaced["input"]


def test_workflow_new_requests_default_to_v5():
    # 对比、套用都是从零起新图：默认 5 Full，只有 /画图 那条命令还用 4.5。
    mixin = WorkflowMixin()
    mixin.config = NovelAIConfig()
    assert mixin._workflow_parse("castle").model == MODEL_ALIASES["高清"]
    assert mixin._workflow_parse("castle --模型 4.5").model == MODEL_ALIASES["普通"]


def test_style_tags_lead_the_prompt_after_a_scene_replacement():
    # 原请求把画风块放在最前面；套用/系列整段替换场景时若追加到末尾，
    # 同一套默认柔光会被场景标签压成赛璐璐。
    mixin = WorkflowMixin()
    styled = mixin._workflow_apply_style(payload(), {"tags": ["soft shading", "cel shading"]})
    assert styled["input"].startswith("soft shading, cel shading, portrait")


def test_named_chinese_columns_preserve_english_compound_tags():
    req = parse_args("全局：花园里的两人，close-up-角色1：左侧甘雨，t-shirt-角色2：右侧刻晴")
    assert req.prompt.count("|") == 2
    assert "close-up" in req.prompt and "t-shirt" in req.prompt
    assert "角色1：" not in req.prompt


def test_garden_is_not_new_jersey_when_used_as_scene():
    assert is_ambiguous_hit("花园", 100000, 4, "甘雨坐在花园里读书")
    assert not is_ambiguous_hit("花园", 100000, 4, "碧蓝航线的花园穿着长裙")


def test_vlm_local_edit_uses_chinese_and_keeps_style_and_unedited_pixels():
    source = image_data()
    llm = SimpleNamespace(generate=AsyncMock(return_value={"success": True, "response": json.dumps({
        "target": "围巾", "confidence": .95, "coordinate_system": "1000",
        "boxes": [[200, 400, 600, 700]], "prompt": "blue scarf", "remove_tags": ["red scarf"],
    })}))
    request, mask, location = asyncio.run(prepare_text_edit(payload(), source, "把甘雨脖子上的红围巾改成蓝色", llm, AsyncMock()))
    # v5 的 infill 端点不认遮罩，改走整图 img2img 再按选区合回。
    assert request["action"] == "img2img"
    assert request["model"] == "nai-diffusion-5-full"
    assert request["parameters"]["strength"] == .75
    assert "soft shading" in request["input"] and "red scarf" not in request["input"]
    assert "blue scarf" in request["input"]
    assert "red scarf" in request["parameters"]["negative_prompt"]
    assert location["boxes"] == [[.2, .4, .6, .7]]
    assert llm.generate.await_args.kwargs["model"] == "vlm"
    combined = preserve_outside(source, image_data("blue"), mask)
    with Image.open(BytesIO(b64decode(combined))) as image:
        assert image.getpixel((0, 0)) == (255, 0, 0)
        assert image.getpixel((12, 25))[2] > 0


def test_uncertain_or_full_frame_grounding_is_rejected():
    for plan in (
        {"confidence": .2, "boxes": [], "prompt": "blue scarf"},
        {"confidence": .99, "boxes": [[0, 0, 1, 1]], "prompt": "blue scarf"},
        {"confidence": .99, "coordinate_system": "pixels", "boxes": [[0, 0, 32, 48]], "prompt": "blue scarf"},
    ):
        with pytest.raises(NovelAIError):
            parse_location(json.dumps(plan))


def test_single_character_without_columns_can_infer_identity():
    original = payload()
    original["parameters"]["v4_prompt"]["caption"]["char_captions"] = []
    llm = SimpleNamespace(generate=AsyncMock(return_value={"success": True, "response": '{"count":1,"identity":"blue hair, purple eyes, horns"}'}))
    result = asyncio.run(infer_single_identity(original, image_data(), llm))
    assert "blue hair" in result
    llm.generate.return_value["response"] = '{"count":2,"identity":""}'
    with pytest.raises(NovelAIError, match="多人系列"):
        asyncio.run(infer_single_identity(original, image_data(), llm))
