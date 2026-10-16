"""V5 配方来源和多角色动作语义的回归保护。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import hashlib
import json

from PIL import Image
import pytest

from novelai_image.parser import MODEL_ALIASES, parse_args
from novelai_image.plugin import NovelAIConfig, NovelAIImagePlugin
from novelai_image.presets import ASSET_DIR, PRESETS, generation_tuning, preset_render_tags


@pytest.mark.parametrize("preset", [p for p in PRESETS if p.key != "artist_torino_aqua"], ids=lambda p: p.key)
def test_v5_recipe_matches_its_own_reference_metadata(preset):
    data = json.loads((ASSET_DIR.parent / "style_recipes_v5.json").read_text(encoding="utf-8"))["recipes"][preset.key]
    path = preset.reference_v5_path
    assert hashlib.sha256(path.read_bytes()).hexdigest() == data["sha256"]
    with Image.open(path) as image:
        source = json.loads(image.info["Comment"])
    # 快照不得继承示例人物、衣服、构图、种子。
    assert data["tags"] == [source["prompt"].split("1girl", 1)[0].strip(" ,")]
    assert preset_render_tags(preset, True) == tuple(data["tags"])
    assert preset_render_tags(preset, False) == preset.tags
    tuning = generation_tuning(preset, True)
    assert (tuning.scale, tuning.steps, tuning.sampler) == (source["scale"], source["steps"], source["sampler"])
    assert set(tuning.negative_tags) | set(data.get("removed_reference_conflicts", [])) == set(source["uc"].split(", "))
    assert not set(tuning.negative_tags) & set(data["drop_negatives"])


def test_confirmed_torino_is_not_replaced_by_reference_snapshot():
    preset = next(p for p in PRESETS if p.key == "artist_torino_aqua")
    assert preset.tags_v5 is None
    assert preset.extra_negatives_v5 is None
    assert preset.drop_negatives_v5 is None


def test_all_v5_presets_have_an_explicit_recipe_or_confirmed_exception():
    recipes = json.loads((ASSET_DIR.parent / "style_recipes_v5.json").read_text(encoding="utf-8"))["recipes"]
    assert set(recipes) == {p.key for p in PRESETS if p.key != "artist_torino_aqua"}


@pytest.mark.parametrize("option,landscape", [("", False), (" --比例 横图", True)])
def test_original_v5_action_survives_translation_and_manual_canvas(tmp_path, option, landscape):
    obj = NovelAIImagePlugin()
    obj._plugin_config_instance = NovelAIConfig()
    obj._plugin_config_instance.translation.backend = "nanogpt"
    obj._plugin_config_instance.characters.enabled = False
    obj._ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path))
    obj._character_index = lambda: None
    async def translate(text, negative, identities, **kwargs):
        return {"base": "3girls", "negative": negative, "description": "Three girls hold hands.", "characters": [
            {"identity_id": "", "positive": "girl", "negative": "", "position": "auto"} for _ in range(3)]}
    obj._translator = lambda: SimpleNamespace(translate_scene=AsyncMock(side_effect=translate))
    original = "三个女生一起抓住手"
    planned, payload, *_ = asyncio.run(obj._plan_request(
        parse_args(original + option, default_model=MODEL_ALIASES["高清"]), "lab", "qq"))
    assert original in planned.prompt
    assert (planned.width > planned.height) == landscape
    assert planned.scale == 5.5
    for positive in payload["parameters"]["v4_prompt"]["caption"]["char_captions"]:
        assert "full body" in positive["char_caption"]
