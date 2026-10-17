from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import re

import pytest

from novelai_image.parser import MODEL_ALIASES, parse_args
from novelai_image.plugin import NovelAIConfig, NovelAIImagePlugin, weighted_style_tags
from novelai_image.presets import PRESETS, StylePreset


@pytest.fixture
def planner(tmp_path):
    obj = NovelAIImagePlugin()
    obj._plugin_config_instance = NovelAIConfig()
    obj._plugin_config_instance.characters.enabled = False
    obj._plugin_config_instance.generation.default_style_weight = 1.0
    obj._ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path), send=SimpleNamespace())
    asyncio.run(obj.on_load())
    return obj


@pytest.fixture
def elaina_planner(planner, monkeypatch):
    monkeypatch.setattr("novelai_image.plugin.resolve_official_split",
                        lambda index, text, candidate: (["elaina_(majo_no_tabitabi)"], [], text))
    return planner


def plan(obj, prompt):
    req = parse_args(prompt, default_model=MODEL_ALIASES["高清"])
    return asyncio.run(obj._plan_request(req, "lab", "qq"))


def test_all_artists_keep_independent_weight_blocks_in_real_plan(planner):
    # 全部通过实际请求规划，防止手搭提示词漏掉再次包裹这一层。
    for preset in PRESETS:
        if not preset.key.startswith("artist_"):
            continue
        name = next(keyword for keyword in preset.keywords if keyword.isascii())
        final, payload, _, _, _, _ = plan(planner, f"1girl, upper body, {name} --画风权重 1")
        assert "::artist:" in final.prompt, preset.key
        assert not re.search(r"\d+(?:\.\d+)?::\s*\d+(?:\.\d+)?::", final.prompt), preset.key
        assert payload["input"] == final.prompt


@pytest.mark.parametrize("model", list(MODEL_ALIASES.values()))
@pytest.mark.parametrize("preset", PRESETS, ids=lambda preset: preset.key)
def test_multi_keeps_each_preset_recipe_isolated(planner, monkeypatch, model, preset):
    """人数变化不得替换各自画风块、预设参数或偷偷注入默认柔光。"""
    planner._plugin_config_instance.translation.backend = "nanogpt"
    planner._character_index = lambda: None
    async def scene(text, negative, identities, **kwargs):
        return {"base": "3girls", "negative": negative,
                "description": "Three girls stand together.", "characters": [
                    {"identity_id": "", "positive": "girl", "negative": "", "position": "auto"}
                    for _ in range(3)]}
    translator = SimpleNamespace(translate_scene=AsyncMock(side_effect=scene),
                                translate_many=AsyncMock(return_value={"positive": "1girl"}))
    monkeypatch.setattr(planner, "_translator", lambda: translator)
    # 固定选择被审查档；关键词匹配由现有独立测试覆盖。
    monkeypatch.setattr("novelai_image.plugin.match_preset", lambda _: preset)
    single, single_payload, *_ = asyncio.run(planner._plan_request(
        parse_args("1girl", default_model=model), "lab", "qq"))
    multi, multi_payload, *_ = asyncio.run(planner._plan_request(
        parse_args("3girls", default_model=model), "lab", "qq"))
    blocks = lambda prompt: re.findall(r"[-\d.]+::.*?::", prompt)
    assert blocks(single.prompt) == blocks(multi.prompt)
    assert (single.scale, single.steps, single.sampler, single.style_weight) == (
        multi.scale, multi.steps, multi.sampler, multi.style_weight)
    assert set(single.negative.split(", ")) <= set(multi.negative.split(", "))
    assert set(multi.negative.split(", ")) - set(single.negative.split(", ")) <= {
        "close-up", "cropped", "out of frame", "solo focus"}
    for key in ("director_reference_images", "director_reference_strength_values", "director_reference_secondary_strength_values"):
        assert single_payload["parameters"].get(key) == multi_payload["parameters"].get(key)


@pytest.mark.parametrize("weight,artist_weight", [(1, "1.05"), (0.8, "0.84"), (1.5, "1.575")])
def test_manual_weight_multiplies_artist_and_render_without_nesting(planner, monkeypatch, weight, artist_weight):
    preset = StylePreset("artist_test", "test", ("test-style",),
                         ("1.05::artist:test::", "soft shading", "0.7::thin lines, colored lineart::"))
    monkeypatch.setattr("novelai_image.plugin.match_preset", lambda _: preset)
    final, _, _, _, _, _ = plan(planner, f"1girl, test-style --画风权重 {weight}")
    assert f"{artist_weight}::artist:test::" in final.prompt
    assert f"{weight:g}::soft shading ::" in final.prompt
    assert f"{0.7 * weight:g}::thin lines, colored lineart::" in final.prompt


def test_default_style_also_flattens_its_existing_weights(planner):
    final, _, _, _, _, _ = plan(planner, "1girl, upper body --画风权重 1.5")
    assert "1.05::colored lineart, thin lines::" in final.prompt
    assert "1.5::0.7::" not in final.prompt


def test_manual_negative_keeps_conflicting_term_and_replaces_style_negatives(planner, monkeypatch):
    preset = StylePreset("cel_test", "cel", (), ("cel shading",),
                         extra_negatives=("preset exclusion",), drop_negatives=("cel shading",))
    monkeypatch.setattr("novelai_image.plugin.match_preset", lambda _: preset)
    final, _, _, _, _, _ = plan(planner, "1girl --负面 cel shading, user exclusion")
    assert final.negative == "cel shading, user exclusion"


@pytest.mark.parametrize("model", [MODEL_ALIASES["普通"], MODEL_ALIASES["高清"]])
@pytest.mark.parametrize("style,conflicts", [
    ("photorealistic", ("photorealistic",)),
    ("sharp lineart", ("high contrast", "vivid colors")),
])
def test_photoreal_and_sharp_remove_only_inherited_conflicting_negatives(planner, model, style, conflicts):
    # 同时覆盖配置继承与模型各自的基础负面，显式负面不经过预设清理。
    inherited = ", ".join(("low quality", "custom baseline", *conflicts))
    req = parse_args(f"1girl, {style}", default_model=model, default_negative=inherited)
    final, _, _, _, _, _ = asyncio.run(planner._plan_request(req, "lab", "qq"))
    negatives = set(final.negative.split(", "))
    assert not set(conflicts) & negatives
    assert {"low quality", "custom baseline"} <= negatives
    assert all(term in final.prompt for term in conflicts)

    explicit = ", ".join((*conflicts, "user exclusion"))
    req = parse_args(f"1girl, {style} --负面 {explicit}", default_model=model,
                     default_negative=inherited)
    final, _, _, _, _, _ = asyncio.run(planner._plan_request(req, "lab", "qq"))
    assert final.negative == explicit


def test_translation_cannot_change_preselected_style(planner, monkeypatch):
    preset = StylePreset("chosen", "chosen", (), ("1.05::artist:test::", "soft shading"))
    matcher = lambda text: preset if "指定画风" in text else None
    monkeypatch.setattr("novelai_image.plugin.match_preset", matcher)
    translator = SimpleNamespace(translate_many=AsyncMock(return_value={"positive": "1girl, pixel art"}))
    monkeypatch.setattr(planner, "_translator", lambda: translator)
    final, _, note, _, _, _ = plan(planner, "1girl, 指定画风")
    assert "1.05::artist:test::" in final.prompt and "soft shading" in final.prompt
    assert "预设画风：chosen" in note


def test_planning_does_not_open_png_metadata(planner, monkeypatch):
    def broken_metadata(*args, **kwargs):
        raise AssertionError("请求规划不应打开预览图读取参数")
    monkeypatch.setattr("PIL.Image.open", broken_metadata)
    final, payload, _, _, _, _ = plan(planner, "1girl, rella")
    assert "artist:rella" in final.prompt
    assert payload["parameters"]["steps"] == final.steps


def test_immutable_tuning_applies_only_without_manual_overrides(planner, monkeypatch):
    tuning = SimpleNamespace(negative_tags=("measured negative",), scale=4.7, steps=31,
                             sampler="k_euler", style_weight=None)
    monkeypatch.setattr("novelai_image.plugin.generation_tuning", lambda *a, **kw: tuning)
    final, payload, _, _, _, _ = plan(planner, "1girl, rella")
    assert (final.scale, final.steps, final.sampler) == (4.7, 31, "k_euler")
    assert "measured negative" in final.negative
    assert payload["parameters"]["steps"] == 31
    final, _, _, _, _, _ = plan(planner, "1girl, rella --引导 6.2 --步数 22 --采样器 ddim_v3 --负面 custom")
    assert (final.scale, final.steps, final.sampler) == (6.2, 22, "ddim_v3")
    assert final.negative == "custom"
    final, _, _, _, _, _ = plan(planner, "1girl, rella --自动参数 off --画风权重 1")
    assert final.steps == 28 and final.scale == planner.config.generation.default_scale
    assert final.sampler == planner.config.generation.default_sampler


def test_measured_style_weight_and_manual_override(planner, monkeypatch):
    tuning = SimpleNamespace(negative_tags=None, scale=None, steps=None,
                             sampler=None, style_weight=0.9)
    preset = StylePreset("artist_test", "test", (), ("1.05::artist:test::", "soft shading"))
    monkeypatch.setattr("novelai_image.plugin.match_preset", lambda _: preset)
    monkeypatch.setattr("novelai_image.plugin.generation_tuning", lambda *a, **kw: tuning)
    final, _, _, _, _, _ = plan(planner, "1girl, test-style")
    assert final.style_weight == 0.9 and "0.945::artist:test::" in final.prompt
    final, _, _, _, _, _ = plan(planner, "1girl, test-style --画风权重 1.2")
    assert final.style_weight == 1.2 and "1.26::artist:test::" in final.prompt


def test_multiple_preweighted_blocks_in_one_config_item_are_flattened():
    assert weighted_style_tags(["0.7::thin lines::, soft shading, 1.2::detailed eyes::"], 1.5) == [
        "1.5::soft shading ::", "1.05::thin lines::", "1.8::detailed eyes::"]


def test_negative_numerical_emphasis_remains_negative():
    assert weighted_style_tags(["-1.5::monochrome::", ".5::flat colors::"], 0.8) == [
        "-1.2::monochrome::", "0.4::flat colors::"]


@pytest.mark.parametrize("scene", ["sunset", "at night", "neon lights", "rim lighting", "front light", "夕阳下"])
def test_explicit_scene_lighting_wins_over_artist_defaults(planner, monkeypatch, scene):
    preset = StylePreset("artist_test", "test", (),
                         ("1.05::artist:test::", "diffused lighting", "front lighting",
                          "0.7::cool lighting, gradient shading, thin lines::", "rim lighting", "cool shadows"))
    monkeypatch.setattr("novelai_image.plugin.match_preset", lambda _: preset)
    translator = SimpleNamespace(translate_many=AsyncMock(return_value={"positive": "1girl, sunset"}))
    monkeypatch.setattr(planner, "_translator", lambda: translator)
    final, _, _, _, _, _ = plan(planner, f"1girl, test-style, {scene} --画风权重 1")
    for lighting in ("diffused lighting", "front lighting", "cool lighting", "rim lighting"):
        # 本轮本身提到的光照依旧保留；默认画风部分不再带同类照明。
        assert lighting not in final.prompt.split("1girl")[0]
    assert "0.7::gradient shading, thin lines::" in final.prompt
    assert "cool shadows" in final.prompt and "1.05::artist:test::" in final.prompt
    assert ("sunset" if scene == "夕阳下" else scene) in final.prompt


def test_plain_scene_and_negative_lighting_keep_artist_defaults(planner, monkeypatch):
    preset = StylePreset("artist_test", "test", (),
                         ("1.05::artist:test::", "diffused lighting", "0.7::rim lighting, thin lines::"))
    monkeypatch.setattr("novelai_image.plugin.match_preset", lambda _: preset)
    final, _, _, _, _, _ = plan(planner, "1girl, test-style, indoors --负面 sunset, night")
    assert "diffused lighting" in final.prompt
    assert "0.7::rim lighting, thin lines::" in final.prompt


def test_torino_lighting_recipe_is_preserved_for_explicit_scene(planner, monkeypatch):
    preset = StylePreset("artist_torino_aqua", "torino", (),
                         ("1.05::artist:torino::", "diffused lighting", "0.7::rim lighting, thin lines::"))
    monkeypatch.setattr("novelai_image.plugin.match_preset", lambda _: preset)
    final, _, _, _, _, _ = plan(planner, "1girl, torino, sunset --画风权重 1")
    assert "diffused lighting" in final.prompt
    assert "0.7::rim lighting, thin lines::" in final.prompt
    assert "sunset" in final.prompt


def test_bad_nested_configuration_reports_weight_error():
    from novelai_image.exceptions import NovelAIError
    with pytest.raises(NovelAIError, match="不要嵌套权重"):
        weighted_style_tags(["1::1.05::artist:test::::"], 1)


def test_v5_default_restores_softening_and_preserves_manual_scaling(elaina_planner):
    req = parse_args("1girl, rain --画风权重 1.5", default_model=MODEL_ALIASES["高清"],
                     default_negative="low quality, cel shading, hard edges, crisp lineart, flat colors, custom baseline")
    final, payload, _, _, _, _ = asyncio.run(elaina_planner._plan_request(req, "lab", "qq"))
    assert "cel shading" not in final.prompt
    assert final.prompt.startswith("1.5::soft shading")
    assert "soft lighting" in final.prompt and "diffused lighting" in final.prompt
    assert "hair highlights" in final.prompt and "detailed skin" in final.prompt
    assert not {"big eyes", "round face", "child", "volumetric lighting"} & set(final.prompt.split(", "))
    assert "1.05::colored lineart, thin lines::" in final.prompt
    assert "airbrushed shading" in final.prompt and "glossy hair" in final.prompt
    assert "blurred lineart" in final.prompt and "soft focus" in final.prompt
    negatives = set(final.negative.split(", "))
    assert {"cel shading", "flat colors"} <= negatives
    assert {"hard edges", "crisp lineart", "low quality", "custom baseline"} <= negatives
    # 伊蕾娜专属的 0.35::photorealistic, 3d render:: 锁定负面已移除：和 cel 0.5 叠加
    # 会把脸压得发灰、眼睛发闷，实测比甘雨等角色的默认档更差。
    assert "0.35::photorealistic, 3d render::" not in final.negative
    assert "volumetric lighting" not in negatives
    assert payload["parameters"]["v4_negative_prompt"]["caption"]["base_caption"] == final.negative


def test_v5_default_preserves_explicit_negative_exactly(elaina_planner):
    negative = "cel shading, hard edges, crisp lineart, custom exclusion"
    final, _, _, _, _, _ = plan(elaina_planner, "1girl, rain --负面 " + negative)
    assert final.negative == negative
    assert "cel shading" not in final.prompt


def test_v5_default_has_verified_realism_guards(elaina_planner):
    final, _, _, _, _, _ = plan(elaina_planner, "1girl, rain")
    assert elaina_planner.config.style.hd_default_only_negative_tags == []
    from novelai_image.adaptive import BRIGHT_NEGATIVE
    assert set(final.negative.split(", ")) == {"low quality", "blurry", "dramatic lighting", "harsh shadows",
                                               *BRIGHT_NEGATIVE}
    assert set(elaina_planner.config.style.hd_default_negative_tags) == {"dramatic lighting", "harsh shadows"}


@pytest.mark.parametrize("artist", ["rella", "wanke", "torino", "sakimichan"])
def test_v5_default_changes_do_not_leak_into_artist_plans(planner, artist):
    before = plan(planner, "1girl, rain, " + artist + " --seed 123")[1]
    planner.config.style.hd_default_tags = ["default only marker"]
    planner.config.style.hd_default_only_negative_tags = ["default only exclusion"]
    after = plan(planner, "1girl, rain, " + artist + " --seed 123")[1]
    assert after == before


def test_default_cel_negative_cleanup_does_not_apply_to_v45_or_manual_mode(elaina_planner):
    elaina_planner.config.style.default_negative_tags = ["base marker"]
    for option in ("--模型 普通", "--自动参数 off"):
        req = parse_args("1girl, rain " + option, default_model=MODEL_ALIASES["高清"],
                         default_negative="cel shading, hard edges, crisp lineart")
        final, _, _, _, _, _ = asyncio.run(elaina_planner._plan_request(req, "lab", "qq"))
        assert {"cel shading", "hard edges", "crisp lineart"} <= set(final.negative.split(", "))
        assert not set(elaina_planner.config.style.hd_default_only_negative_tags) & set(final.negative.split(", "))
        assert "volumetric lighting" not in final.negative
        assert "0.35::photorealistic, 3d render::" not in final.negative


def test_v5_default_preserves_both_configured_negative_sources(elaina_planner):
    elaina_planner.config.style.hd_default_negative_tags = ["CEL_SHADING", "flat colors", "hard shading", "style marker"]
    elaina_planner.config.style.hd_default_only_negative_tags = ["two-tone shading", "only marker"]
    req = parse_args("1girl, rain", default_model=MODEL_ALIASES["高清"],
                     default_negative="cel shading, flat colors, two-tone shading, base marker")
    final, payload, *_ = asyncio.run(elaina_planner._plan_request(req, "lab", "qq"))
    negatives = set(final.negative.split(", "))
    assert {"CEL_SHADING", "cel shading", "flat colors", "two-tone shading"} <= negatives
    assert {"base marker", "style marker", "only marker", "hard shading"} <= negatives
    assert "0.35::photorealistic, 3d render::" not in final.negative
    assert payload["parameters"]["negative_prompt"] == final.negative


@pytest.mark.parametrize("style", ["torino", "watercolor", "cel shading"])
def test_v5_default_fixed_negative_policy_does_not_touch_named_styles(elaina_planner, style):
    elaina_planner.config.style.hd_default_only_negative_tags = ["default policy marker"]
    req = parse_args("1girl, rain, " + style, default_model=MODEL_ALIASES["高清"],
                     default_negative="low quality, photorealistic")
    final, *_ = asyncio.run(elaina_planner._plan_request(req, "lab", "qq"))
    negatives = set(final.negative.split(", "))
    assert "default policy marker" not in negatives


def test_v5_style_schema_and_shipped_configs_use_same_recipe():
    from pathlib import Path
    import tomllib
    from novelai_image.adaptive import HD_DEFAULT_STYLE, HD_DEFAULT_NEGATIVE_STYLE, HD_DEFAULT_ONLY_NEGATIVE_STYLE
    style = NovelAIConfig().style
    assert style.hd_default_tags == list(HD_DEFAULT_STYLE)
    assert style.hd_default_negative_tags == list(HD_DEFAULT_NEGATIVE_STYLE)
    assert style.hd_default_only_negative_tags == list(HD_DEFAULT_ONLY_NEGATIVE_STYLE)
    root = Path(__file__).resolve().parents[1]
    for name in ("config.toml", "config.example.toml"):
        configured = tomllib.loads((root / name).read_text(encoding="utf-8"))["style"]
        assert configured["hd_default_tags"] == style.hd_default_tags
        assert configured["hd_default_negative_tags"] == style.hd_default_negative_tags
        assert configured["hd_default_only_negative_tags"] == style.hd_default_only_negative_tags


def test_manual_mode_without_inherited_style_reports_no_style_without_crashing(planner):
    req = parse_args("1girl, rain --自动参数 off --画风权重 1 --负面 cel shading, custom",
                     default_model=MODEL_ALIASES["高清"])
    final, payload, note, _, _, _ = asyncio.run(planner._plan_request(req, "lab", "qq"))
    assert req.style_weight == 1 and req.auto is False
    assert "style-weight" in final.explicit
    assert final.negative == "cel shading, custom"
    assert final.prompt == "1girl, rain"
    assert final.style_weight == 0  # 没有继承画风时没有实际施加权重，也不自动补默认画风。
    assert "画风来源：无" in note
    assert payload["input"] == final.prompt


@pytest.mark.parametrize("option", ["", "--模型 普通"])
def test_automatic_default_routes_translation_to_historical_profile(planner, monkeypatch, option):
    translator = SimpleNamespace(translate_many=AsyncMock(return_value={"positive": "1girl, cel shading, detailed skin"}))
    monkeypatch.setattr(planner, "_translator", lambda: translator)
    final, _, note, _, _, _ = plan(planner, "女孩站在雨中 " + option)
    assert translator.translate_many.call_args.kwargs == {"profile": "default_20261015_1917"}
    assert "cel shading" in final.prompt and "detailed skin" in final.prompt
    assert "默认细线柔光预设" in note


@pytest.mark.parametrize("option", ["--自动参数 off", "--画风权重 0", "水彩画风", "rella"])
def test_nondefault_styles_keep_modern_translation(planner, monkeypatch, option):
    translator = SimpleNamespace(translate_many=AsyncMock(return_value={"positive": "1girl, standing"}))
    monkeypatch.setattr(planner, "_translator", lambda: translator)
    plan(planner, "女孩站在雨中 " + option)
    assert "profile" not in translator.translate_many.call_args.kwargs


def test_hd_default_preserves_full_44_recipe_in_bright_scene(planner):
    from novelai_image.adaptive import HD_DEFAULT_STYLE
    final, _, _, _, _, _ = plan(planner, "1girl, rain --seed 1568697089 --引导 5.5 --步数 28 --比例 竖图")
    assert len(HD_DEFAULT_STYLE) == 44
    assert final.prompt.startswith(", ".join(weighted_style_tags(list(HD_DEFAULT_STYLE), 1)))
    assert final.scale == 5.5 and final.steps == 28 and final.seed == 1568697089
    from novelai_image.adaptive import BRIGHT_NEGATIVE
    assert set(final.negative.split(", ")) == {"low quality", "blurry", "dramatic lighting", "harsh shadows", *BRIGHT_NEGATIVE}


@pytest.mark.parametrize("characters", [[], ["ganyu_(genshin_impact)"], ["topaz_(honkai:_star_rail)"],
                                       ["elaina_(majo_no_tabitabi)", "ganyu_(genshin_impact)"]])
def test_elaina_negative_calibration_does_not_affect_other_identities(planner, monkeypatch, characters):
    monkeypatch.setattr("novelai_image.plugin.resolve_official_split",
                        lambda index, text, candidate: (characters, [], text))
    baseline = "low quality, cel shading, flat colors, two-tone shading, hard shading"
    async def scene(text, negative, identities, **kwargs):
        return {"base": "2girls, rain", "negative": negative, "characters": [
            {"identity_id": identity["id"], "positive": identity["name"], "negative": "", "position": "auto"} for identity in identities]}
    monkeypatch.setattr(planner, "_translator", lambda: SimpleNamespace(translate_scene=scene))
    req = parse_args("elaina_(majo_no_tabitabi), rain", default_model=MODEL_ALIASES["高清"],
                     default_negative=baseline)
    final, payload, *_ = asyncio.run(planner._plan_request(req, "lab", "qq"))
    negatives = set(final.negative.split(", "))
    assert set(baseline.split(", ")) <= negatives
    assert "0.35::photorealistic, 3d render::" not in final.negative
    assert payload["parameters"]["negative_prompt"] == final.negative


@pytest.mark.parametrize("negative_option", ["", " --负面 custom exclusion, cel shading"])
def test_elaina_default_cel_weight_is_independent_of_negative_override(elaina_planner, monkeypatch, negative_option):
    translator = SimpleNamespace(translate_many=AsyncMock(return_value={"positive": "1girl, cel shading, rain"}))
    monkeypatch.setattr(elaina_planner, "_translator", lambda: translator)
    final, *_ = plan(elaina_planner, "女孩站在雨中" + negative_option)
    assert "0.5::cel shading::" in final.prompt
    assert "cel shading" not in final.prompt.split(", ")
    if negative_option:
        assert final.negative == "custom exclusion, cel shading"


@pytest.mark.parametrize("option", ["--模型 普通", "--自动参数 off", "水彩画风", "torino"])
def test_elaina_cel_weight_does_not_leak_to_other_styles(elaina_planner, monkeypatch, option):
    translator = SimpleNamespace(translate_many=AsyncMock(return_value={"positive": "1girl, cel shading, rain"}))
    monkeypatch.setattr(elaina_planner, "_translator", lambda: translator)
    final, *_ = plan(elaina_planner, "女孩站在雨中 " + option)
    assert "0.5::cel shading::" not in final.prompt


@pytest.mark.parametrize("characters", [["ganyu_(genshin_impact)"], ["topaz_(honkai:_star_rail)"],
                                       ["elaina_(majo_no_tabitabi)", "ganyu_(genshin_impact)"]])
def test_elaina_cel_weight_does_not_leak_to_other_characters(planner, monkeypatch, characters):
    monkeypatch.setattr("novelai_image.plugin.resolve_official_split",
                        lambda index, text, candidate: (characters, [], text))
    translator = SimpleNamespace(translate_many=AsyncMock(return_value={"positive": "1girl, cel shading, rain"}))
    translator.translate_scene = AsyncMock(return_value={"base": "2girls, cel shading, rain", "negative": "low quality", "characters": [
        {"identity_id": f"official:{i}", "positive": name, "negative": "", "position": "auto"} for i, name in enumerate(characters)]})
    monkeypatch.setattr(planner, "_translator", lambda: translator)
    final, *_ = plan(planner, "女孩站在雨中")
    # 多人默认档会把它抬到 MULTI_CEL_WEIGHT（唯一强制平面媒介的标签）；单人仍是裸标签。
    from novelai_image.default_style_policy import MULTI_CEL_WEIGHT
    expected = f"{MULTI_CEL_WEIGHT:g}::cel shading::" if len(characters) > 1 else "cel shading"
    assert expected in final.prompt.split(", ")
    assert "0.5::cel shading::" not in final.prompt


def test_elaina_saved_translation_replays_user_accepted_payload_exactly(elaina_planner, monkeypatch):
    """保存的译文仍逐字重放；只是不再追加伊蕾娜专属的锁定负面。"""
    from pathlib import Path
    import json
    expected = json.loads((Path(__file__).parent / "fixtures" / "elaina_cel05_accepted.json").read_text(encoding="utf-8"))
    lock = ", 0.35::photorealistic, 3d render::"
    expected["parameters"]["negative_prompt"] = expected["parameters"]["negative_prompt"].removesuffix(lock)
    neg_caption = expected["parameters"]["v4_negative_prompt"]["caption"]
    neg_caption["base_caption"] = neg_caption["base_caption"].removesuffix(lock)
    cloud = "masterpiece" + expected["input"].split("masterpiece", 1)[1]
    cloud = cloud.replace("0.5::cel shading::", "cel shading")
    translator = SimpleNamespace(translate_many=AsyncMock(return_value={"positive": cloud}))
    monkeypatch.setattr(elaina_planner, "_translator", lambda: translator)
    negative = expected["parameters"]["negative_prompt"].removesuffix(", 0.35::photorealistic, 3d render::")
    req = parse_args("雨天打伞的伊蕾娜 --seed 1537797012", default_model=MODEL_ALIASES["高清"],
                     default_negative=negative, size_presets={"竖图": (512, 768)})
    _, payload, *_ = asyncio.run(elaina_planner._plan_request(req, "lab", "qq"))
    assert payload == expected


@pytest.mark.parametrize("style", ["赛璐璐", "rella", "wanke", "torino", "atdan", "iren lovel", "yoneyama mai", "自定义水墨画风"])
def test_other_style_payload_and_translation_route_ignore_default_rollback(planner, monkeypatch, style):
    translator = SimpleNamespace(translate_many=AsyncMock(return_value={"positive": "1girl, standing, rain"}))
    monkeypatch.setattr(planner, "_translator", lambda: translator)
    before = plan(planner, f"女孩站在雨中，{style} --seed 123")
    before_options = translator.translate_many.call_args.kwargs.copy()
    planner.config.style.hd_default_tags = ["default rollback marker"]
    planner.config.style.hd_default_only_negative_tags = ["default rollback exclusion"]
    after = plan(planner, f"女孩站在雨中，{style} --seed 123")
    assert before[1] == after[1]
    assert translator.translate_many.call_args.kwargs == before_options
    assert "profile" not in before_options


@pytest.mark.parametrize("kind", ["upload", "role"])
def test_upload_and_inherited_style_keep_modern_route_and_payload(planner, monkeypatch, kind):
    from pathlib import Path
    from novelai_image.characters import Character
    translator = SimpleNamespace(translate_many=AsyncMock(return_value={"positive": "standing, rain"}))
    monkeypatch.setattr(planner, "_translator", lambda: translator)
    prompt = "女孩站在雨中"
    kwargs = {}
    if kind == "upload":
        kwargs["style_reference"] = "mock_reference_image"
        model = MODEL_ALIASES["普通"]
    else:
        model = MODEL_ALIASES["高清"]
        planner.config.characters.enabled = True
        role = Character("Alice", (), ("银发，蓝瞳，柔光",), (), Path("catalog.json"))
        monkeypatch.setattr("novelai_image.plugin.load_characters", lambda *args: [role])
        prompt = "Alice " + prompt
    req = parse_args(prompt + " --seed 123", default_model=model)
    before = asyncio.run(planner._plan_request(req, "lab", "qq", **kwargs))
    before_options = translator.translate_many.call_args.kwargs.copy()
    planner.config.style.hd_default_tags = ["default rollback marker"]
    planner.config.style.default_tags = ["default rollback marker"]
    planner.config.style.hd_default_only_negative_tags = ["default rollback exclusion"]
    after = asyncio.run(planner._plan_request(req, "lab", "qq", **kwargs))
    assert before[1] == after[1]
    assert translator.translate_many.call_args.kwargs == before_options
    assert "profile" not in before_options
    assert before_options["style_context"] == ("图片参考" if kind == "upload" else "角色资料固定词表")
