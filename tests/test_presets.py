"""特色画风预设：命中、互斥优先级、负面覆盖，以及参考图素材齐全。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import base64
import json
from io import BytesIO

import pytest
from PIL import Image

from novelai_image.parser import parse_args
from novelai_image.plugin import weighted_style_tags
from test_plugin import plugin

from novelai_image.adaptive import STYLE_REQUEST
from novelai_image.presets import (
    preview_entries,
    preview_groups,
    HARD_EDGE_NEGATIVES,
    PRESETS,
    StylePreset,
    apply_preset_negatives,
    generation_tuning,
    match_preset,
    preset_names,
    preset_render_tags,
)


def test_named_styles_hit_by_chinese_and_english():
    cases = {
        "厚涂风": "painterly",
        "painterly style": "painterly",
        "赛璐璐上色": "cel",
        "cel shading": "cel",
        "改成水彩": "watercolor",
        "watercolor": "watercolor",
        "素描感": "sketch",
        "pencil sketch": "sketch",
        "please sketch drawing it": "sketch",
        "像素风": "pixel",
        "pixel art": "pixel",
        "写实一点": "photoreal",
        "photorealistic": "photoreal",
    }
    for text, expected in cases.items():
        preset = match_preset(text)
        assert preset is not None and preset.key == expected, text


def test_plain_scene_names_no_preset():
    for text in ("1girl, sitting on bed, bedroom, night, lamp light",
                 "1girl, in a 3d room, old style dress",
                 "1girl, holding a sketch book, looking at viewer"):
        assert match_preset(text) is None, text


def test_half_realistic_is_not_swallowed_by_realistic():
    # 「半写实」同时含「写实」，最长关键词优先让厚涂截住它。
    assert match_preset("半写实厚涂").key == "painterly"


def test_longest_keyword_wins_over_table_order():
    # 「锐利线稿」含素描的「线稿」；素描在表里更靠前，按顺序会被截走。
    assert match_preset("锐利线稿").key == "sharp"
    assert match_preset("锐利线条，清晰）").key == "sharp"
    assert match_preset("铅笔素描").key == "sketch"
    assert match_preset("画师ciloranko锐利").key == "artist_ciloranko"


def test_preset_tags_are_self_contained():
    # 每档整档替换；允许合法的独立权重块，不携带示例人物场景或默认雾化组。
    for preset in PRESETS:
        assert preset.tags, preset.key
        for tag in ("atmospheric haze", "soft bloom", "elaina_(majo_no_tabitabi)", "witch hat", "indoors", "plain wall"):
            assert tag not in preset.tags, preset.key


def test_cel_drops_the_conflicting_base_negatives():
    cel = next(preset for preset in PRESETS if preset.key == "cel")
    assert set(cel.drop_negatives) == set(HARD_EDGE_NEGATIVES)
    base = ["lowres", "sharp lines", "crisp lineart", "cel shading", "flat colors", "dramatic lighting"]
    result = apply_preset_negatives(base, cel)
    assert "lowres" in result and "dramatic lighting" in result
    for tag in HARD_EDGE_NEGATIVES:
        assert tag not in result
    assert "pixel art" in result


def test_presets_without_drops_keep_the_base_negatives():
    painterly = next(preset for preset in PRESETS if preset.key == "painterly")
    base = ["lowres", "sharp lines", "flat colors"]
    result = apply_preset_negatives(base, painterly)
    assert result[:len(base)] == base
    assert result[len(base):] == [tag for tag in painterly.extra_negatives if tag not in base]


def test_soft_artist_only_lets_thin_lines_through():
    # 柔光系画师只该放行「细线」。整组摘掉会把 cel shading / flat colors 一并放行，
    # 出来的就是通用平涂——torino aqua 那档原来就死在这里。
    torino = next(preset for preset in PRESETS if preset.key == "artist_torino_aqua")
    assert set(torino.drop_negatives) == {"sharp lines", "crisp lineart"}
    base = ["lowres", "sharp lines", "crisp lineart", "cel shading", "flat colors", "posterized"]
    result = apply_preset_negatives(base, torino)
    assert "sharp lines" not in result and "crisp lineart" not in result
    for tag in ("cel shading", "flat colors", "posterized"):
        assert tag in result, tag


def test_soft_artist_ships_its_signature_motifs():
    torino = next(preset for preset in PRESETS if preset.key == "artist_torino_aqua")
    for tag in ("iridescent hair", "prismatic highlights", "glowing skin", "gradient eyes", "bloom"):
        assert tag in torino.tags, tag
    assert "high contrast" in torino.extra_negatives


def test_every_preset_ships_a_reference_image():
    for preset in PRESETS:
        assert preset.reference, preset.key
        assert preset.reference_path.is_file(), f"{preset.key} 缺参考图 {preset.reference_path}"
        assert 0 <= preset.reference_strength <= 1
        assert 0 <= preset.reference_fidelity <= 1


def test_preset_names_are_unique_and_default_absent():
    names = preset_names()
    assert len(names) == len(set(names))
    assert "default" not in names


def test_style_request_no_longer_fires_on_scene_words():
    for text in ("1girl, in a 3d room", "1girl, old style dress", "1girl, holding a sketch book",
                 "1girl, reading a manga"):
        assert not STYLE_REQUEST.search(text), text
    for text in ("画风柔和一点", "换成水彩", "赛璐璐", "厚涂风", "art style: watercolor"):
        assert STYLE_REQUEST.search(text), text


LISTED_GROUP = "100000000"


def _valid_png():
    buffer = BytesIO()
    Image.new("RGB", (32, 48), (25, 80, 170)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _draw(group_id, monkeypatch, forward_result=True, forward_side_effect=None, prompt="1girl, sitting on bed, 像素风"):
    obj = plugin()
    translator = AsyncMock()
    translator.translate_many.return_value = {"positive": "1girl, sitting on bed"}
    monkeypatch.setattr(obj, "_translator", lambda: translator)
    monkeypatch.setattr(obj, "_client", lambda: SimpleNamespace(
        generate=AsyncMock(return_value=[{"image": _valid_png(), "seed": 1}, {"image": _valid_png(), "seed": 2}])))
    obj._plugin_config_instance.generation.collapse_groups = [LISTED_GROUP]
    obj._ctx.config = SimpleNamespace(get=AsyncMock(side_effect=lambda key, default=None: {
        "bot.qq_account": "10000", "bot.nickname": "麦麦"}.get(key, default)))
    obj._ctx.send = SimpleNamespace(text=AsyncMock(), image=AsyncMock(return_value=True),
                                    forward=AsyncMock(return_value=forward_result, side_effect=forward_side_effect))
    asyncio.run(obj.draw(stream_id="s", user_id="u0", group_id=group_id, platform="qq",
                         matched_groups={"kind": "画图", "args": prompt}))
    return obj


def test_listed_group_collapses_even_for_plain_content(monkeypatch):
    # 名单里的群不管画什么内容都折叠，不做内容判定。
    obj = _draw(LISTED_GROUP, monkeypatch)
    obj._ctx.send.forward.assert_awaited_once()
    obj._ctx.send.image.assert_not_awaited()
    messages = obj._ctx.send.forward.await_args.args[0]
    assert len(messages) == 2
    assert messages[0]["segments"][0]["type"] == "image"
    assert obj._ctx.send.forward.await_args.kwargs["timeout_ms"] >= 60000


def test_other_group_sends_directly(monkeypatch):
    obj = _draw("999999", monkeypatch)
    obj._ctx.send.forward.assert_not_awaited()
    assert obj._ctx.send.image.await_count == 2


def test_private_chat_sends_directly(monkeypatch):
    obj = _draw("", monkeypatch)
    obj._ctx.send.forward.assert_not_awaited()
    assert obj._ctx.send.image.await_count == 2


def test_ambiguous_forward_timeout_is_not_retried_and_originals_are_saved(monkeypatch):
    obj = _draw(LISTED_GROUP, monkeypatch, forward_side_effect=RuntimeError("timeout"))
    obj._ctx.send.forward.assert_awaited_once()
    obj._ctx.send.image.assert_not_awaited()
    notice = obj._ctx.send.text.await_args_list[-1].args[0]
    assert "合并转发发送失败" in notice and "原图已保留" in notice
    job_id = notice.split("任务号：", 1)[1].split("）", 1)[0]
    metadata_path = Path(obj.ctx.paths.data_dir) / "pending_deliveries" / job_id / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["stream_id"] == "s"
    assert [record["seed"] for record in metadata["images"]] == [1, 2]
    assert all((metadata_path.parent / record["filename"]).is_file() for record in metadata["images"])


def test_definitive_forward_rejection_retries_smaller_then_saves(monkeypatch):
    obj = _draw(LISTED_GROUP, monkeypatch, forward_result={"success": False})
    assert obj._ctx.send.forward.await_count == 2
    obj._ctx.send.image.assert_not_awaited()
    retry_nodes = obj._ctx.send.forward.await_args_list[1].args[0]
    retried = base64.b64decode(retry_nodes[0]["segments"][0]["content"])
    assert retried.startswith(b"\xff\xd8\xff")
    notice = obj._ctx.send.text.await_args_list[-1].args[0]
    assert "原图已保留" in notice


def test_forward_images_are_normalized_to_jpeg():
    from novelai_image.plugin import shrink_for_forward

    buffer = BytesIO()
    Image.new("RGBA", (32, 32), (255, 0, 0, 255)).save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

    forwarded = base64.b64decode(shrink_for_forward(encoded))
    assert forwarded.startswith(b"\xff\xd8\xff")


def test_missing_bot_account_skips_collapse(monkeypatch):
    # 名单内群取不到机器人账号也不能降级直发，原图持久保留。
    obj = plugin()
    translator = AsyncMock()
    translator.translate_many.return_value = {"positive": "1girl, sitting on bed"}
    monkeypatch.setattr(obj, "_translator", lambda: translator)
    monkeypatch.setattr(obj, "_client", lambda: SimpleNamespace(generate=AsyncMock(return_value=[{"image": _valid_png(), "seed": 1}])))
    obj._plugin_config_instance.generation.collapse_groups = [LISTED_GROUP]
    obj._ctx.config = SimpleNamespace(get=AsyncMock(return_value=""))
    obj._ctx.send = SimpleNamespace(text=AsyncMock(), image=AsyncMock(return_value=True), forward=AsyncMock(return_value=True))
    asyncio.run(obj.draw(stream_id="s", user_id="u0", group_id=LISTED_GROUP, platform="qq",
                         matched_groups={"kind": "画图", "args": "1girl, sitting on bed"}))
    obj._ctx.send.forward.assert_not_awaited()
    obj._ctx.send.image.assert_not_awaited()
    assert "原图已保留" in obj._ctx.send.text.await_args_list[-1].args[0]


def _plan(prompt, monkeypatch):
    obj = plugin()
    translator = AsyncMock()
    translator.translate_many.return_value = {"positive": "1girl, on bed"}
    monkeypatch.setattr(obj, "_translator", lambda: translator)
    return asyncio.run(obj._plan_request(parse_args(prompt), "a", "qq"))


def test_named_preset_replaces_default_and_attaches_reference(monkeypatch):
    final, payload, note, _, _, _ = _plan("1girl, on bed, 像素风", monkeypatch)
    assert "pixel art" in final.prompt
    # 整档替换：默认那套柔光标签不该还在。
    assert "soft shading" not in final.prompt
    assert "muted colors" not in final.prompt
    assert payload["parameters"]["director_reference_strength_values"] == [0.7]
    assert "pixel.png" in note


def test_preset_keeps_scene_words_from_killing_the_default(monkeypatch):
    final, payload, _, _, _, _ = _plan("1girl, in a 3d room, sitting on bed", monkeypatch)
    assert "anime style" in final.prompt
    assert "parameters" in payload


def test_hd_preset_falls_back_to_tags_only(monkeypatch):
    final, payload, note, _, _, _ = _plan("1girl, on bed, 像素风 --模型 高清", monkeypatch)
    assert "pixel art" in final.prompt
    assert "director_reference_images" not in payload["parameters"]
    assert "仅走标签" in note


def test_artist_rendering_uses_selected_immutable_recipe(monkeypatch):
    mika, _, _, _, _, _ = _plan("1girl, on bed, mika pikazo --模型 高清", monkeypatch)
    mifuru, _, _, _, _, _ = _plan("1girl, on bed, mifuru --模型 高清", monkeypatch)
    for req, key in ((mika, "artist_mika_pikazo"), (mifuru, "artist_mifuru")):
        preset = next(p for p in PRESETS if p.key == key)
        expected = weighted_style_tags(list(preset_render_tags(preset, True)), req.style_weight)
        assert all(tag in req.prompt for tag in expected)
        tuning = generation_tuning(preset, True)
        expected_weight = tuning.style_weight if tuning and tuning.style_weight is not None else plugin().config.generation.default_style_weight
        assert req.style_weight == expected_weight
        assert "elaina" not in req.prompt and "witch hat" not in req.prompt
        assert "on bed" in req.prompt


def test_presets_select_saved_generation_tuning_without_image_metadata():
    from novelai_image.parser import SAMPLERS
    for preset in PRESETS:
        for v5 in (False, True):
            tuning = generation_tuning(preset, v5)
            assert tuning is (preset.generation_v5 if v5 else preset.generation)
            if tuning is None:
                continue
            assert tuning.scale is None or 0 <= tuning.scale <= 10
            assert tuning.steps is None or 1 <= tuning.steps <= 50
            assert tuning.sampler is None or tuning.sampler in SAMPLERS
            assert tuning.style_weight is None or 0 <= tuning.style_weight <= 2
            assert tuning.negative_tags is None or isinstance(tuning.negative_tags, tuple)
    torino = next(p for p in PRESETS if p.key == "artist_torino_aqua")
    assert preset_render_tags(torino, True) == torino.tags


def test_preset_reference_accepts_manual_strength_after_attaching(monkeypatch):
    req, payload, _, _, _, _ = _plan(
        "1girl, on bed, watercolor --画风强度 0.4 --画风保真度 0.6 --画风权重 0.8 --引导 6.2", monkeypatch)
    assert payload["parameters"]["director_reference_strength_values"] == [0.4]
    assert payload["parameters"]["director_reference_secondary_strength_values"] == [0.6]
    assert req.scale == 6.2 and req.style_weight == 0.8


def test_style_weight_zero_disables_the_preset(monkeypatch):
    final, payload, _, _, _, _ = _plan("1girl, on bed, 像素风 --画风权重 0", monkeypatch)
    assert "pixel art" not in final.prompt
    assert final.style_weight == 0


def _plan_with_negative(prompt, negative, monkeypatch):
    obj = plugin()
    translator = AsyncMock()
    translator.translate_many.return_value = {"positive": "1girl, on bed"}
    monkeypatch.setattr(obj, "_translator", lambda: translator)
    return asyncio.run(obj._plan_request(parse_args(prompt, default_negative=negative), "a", "qq"))


def test_config_negative_does_not_switch_the_style_preset(monkeypatch):
    # 配置的 default_negative_prompt 里有 cel shading / flat colors，那是挡平涂用的，
    # 不该被当成用户点名了赛璐璐——否则每一张普通图都会静默换成赛璐璐档。
    final, _, note, _, _, _ = _plan_with_negative(
        "1girl, on bed", "low quality, sharp lines, cel shading, flat colors", monkeypatch)
    assert "official art" not in final.prompt
    assert "预设画风" not in note
    assert "soft shading" in final.prompt


def test_style_words_in_the_negative_do_not_pick_a_preset(monkeypatch):
    # 负面说的是不要什么，「不要写实」照字面命中写实档是反的。
    final, _, note, _, _, _ = _plan_with_negative(
        "1girl, on bed", "不要写实，不要 photoshop", monkeypatch)
    assert "预设画风" not in note
    assert "photographic" not in final.prompt


def test_multiple_catalog_roles_get_one_caption_each(monkeypatch):
    # 摊平成一条会让两个人的发色瞳色互相竞争，画出来是一个混血的人。
    from novelai_image.characters import Character
    first = Character("康思勉", (), ("黑色短发，深蓝色棒球帽。",), (), Path("catalog.json"))
    second = Character("尤筠炫", (), ("浅棕色长发，蓝色眼睛。",), (), Path("catalog.json"))
    obj = plugin()
    translator = AsyncMock()
    translator.translate_many.return_value = {"positive": "standing together"}
    monkeypatch.setattr(obj, "_translator", lambda: translator)
    monkeypatch.setattr("novelai_image.plugin.load_characters", lambda *a: [first, second])
    final, payload, note, _, _, _ = asyncio.run(
        obj._plan_request(parse_args("康思勉和尤筠炫站在一起", default_negative="low quality"), "a", "qq"))
    captions = payload["parameters"]["v4_prompt"]["caption"]["char_captions"]
    assert len(captions) == 2
    texts = [item["char_caption"] for item in captions]
    assert texts == ["short hair", "light brown hair, long hair"]
    # 身份标签已随各自 caption 走，base 里不能再留一份，否则又混回一起。
    assert "short hair" not in final.prompt
    assert "long hair" not in final.prompt


def test_artist_names_need_whole_word_match():
    # hiten 落在 whiten 里、rella 落在 corella 里，纯子串会把它们误当成点名。
    assert match_preset("1girl, whiten the background") is None
    assert match_preset("1girl, with a corella bird") is None
    assert match_preset("1girl, hiten style").key == "artist_hiten"
    assert match_preset("1girl, wlop").key == "artist_wlop"


def _models_with_sheets(obj):
    """哪几套参考图已经在磁盘上。v5 那套是分批生成的，测试不能假设它齐全。"""
    cfg = obj.config
    return [model for model, v5 in ((cfg.api.model, False), (cfg.api.hd_model, True))
            if any(path.is_file() for _, path in preview_entries(v5=v5))]


def test_style_list_puts_a_table_first_and_artists_in_a_nested_forward(monkeypatch):
    obj = plugin()
    obj._ctx.config = SimpleNamespace(get=AsyncMock(side_effect=lambda key, default=None: {
        "bot.qq_account": "10000", "bot.nickname": "麦麦"}.get(key, default)))
    obj._ctx.send = SimpleNamespace(text=AsyncMock(), image=AsyncMock(return_value=True), forward=AsyncMock(return_value=True))
    result = asyncio.run(obj.style_list(stream_id="s"))
    assert result[0] is True
    # 第一条转发：总览 + 媒介档；画师档按体积另分若干条。
    messages = obj._ctx.send.forward.await_args_list[0].args[0]
    media, artists = preview_groups()
    # 第一条先是纯文字总览
    head = messages[0]["segments"]
    assert len(head) == 1 and head[0]["type"] == "text"
    assert "可用画风" in head[0]["content"] and "【媒介】" in head[0]["content"]
    # 媒介档平铺：一档一个节点，名字在上，之后每个模型一对（短标 + 参考图）
    assert [item["nickname"] for item in messages[1:]] == [label for label, _ in media]
    for item in messages[1:]:
        types = [segment["type"] for segment in item["segments"]]
        assert types[0] == "text"
        assert types[1:] == ["text", "image"] * ((len(types) - 1) // 2) and len(types) > 1
    # 画师档按体积另分若干条转发，每条里每个画师再各自包一层；
    # 一条转发塞太多图 NapCat 会整条上传失败，所以按预算切。
    batches = obj._ctx.send.forward.await_args_list[1:]
    assert batches, "画师档应当另起转发"
    artists_in_batches = []
    for call in batches:
        nested = call.args[0][-1]["segments"][-1]
        assert nested["type"] == "forward"
        artists_in_batches.extend(nested["data"])
    assert len(artists_in_batches) == len(artists) > 0
    for item in artists_in_batches:
        assert item["user_nickname"] and "nickname" not in item
        assert [segment["type"] for segment in item["content"]] == ["forward"]
        inner = item["content"][0]["data"]
        assert len(inner) == 1
        kinds = [segment["type"] for segment in inner[0]["content"]]
        assert kinds[0] == "text" and kinds[1:] == ["text", "image"] * ((len(kinds) - 1) // 2)
        # 嵌套那层宿主不做归一化，字段必须直接写成适配器读的形状：少 user_nickname 节点名
        # 全变「QQ用户」，文字少了 data 变空白，图片少了 binary_data_base64 变 [image] 占位。
        for segment in inner[0]["content"]:
            assert segment["data"] if segment["type"] == "text" else segment["binary_data_base64"]
    obj._ctx.send.image.assert_not_awaited()


def test_style_list_marks_every_model_it_shows(monkeypatch):
    obj = plugin()
    obj._ctx.config = SimpleNamespace(get=AsyncMock(side_effect=lambda key, default=None: {
        "bot.qq_account": "10000", "bot.nickname": "麦麦"}.get(key, default)))
    obj._ctx.send = SimpleNamespace(text=AsyncMock(), image=AsyncMock(return_value=True), forward=AsyncMock(return_value=True))
    asyncio.run(obj.style_list(stream_id="s"))
    head = obj._ctx.send.forward.await_args_list[0].args[0]
    node = next(item for item in head if item["nickname"] == "默认柔光")
    labels = [segment["content"] for segment in node["segments"] if segment["type"] == "text"]
    expected = ["v4.5" if "4-5" in model else "v5" for model in _models_with_sheets(obj)]
    assert labels == ["默认柔光", *expected]


def test_style_sheet_forwards_one_sheet_per_model(monkeypatch):
    obj = plugin()
    obj._ctx.config = SimpleNamespace(get=AsyncMock(side_effect=lambda key, default=None: {
        "bot.qq_account": "10000", "bot.nickname": "麦麦"}.get(key, default)))
    obj._ctx.send = SimpleNamespace(text=AsyncMock(), image=AsyncMock(return_value=True), forward=AsyncMock(return_value=True))
    result = asyncio.run(obj.style_sheet(stream_id="s"))
    assert result[0] is True
    # 两张完整拼接图逐条直发，不走合并转发也不拆分：模型标注已经画在图顶。
    assert obj._ctx.send.image.await_count == len(_models_with_sheets(obj))
    obj._ctx.send.forward.assert_not_awaited()
    sent = [call.args[0] for call in obj._ctx.send.image.await_args_list]
    for data in sent:
        assert len(data) > 10_000


def test_style_sheet_falls_back_to_plain_images(monkeypatch):
    obj = plugin()
    obj._ctx.config = SimpleNamespace(get=AsyncMock(return_value="10000"))
    obj._ctx.send = SimpleNamespace(text=AsyncMock(), image=AsyncMock(return_value=True),
                                    forward=AsyncMock(side_effect=RuntimeError("timeout")))
    assert asyncio.run(obj.style_sheet(stream_id="s"))[0] is True
    # 回退时每条对照表各发一张，切了几片就发几张
    assert obj._ctx.send.image.await_count >= len(_models_with_sheets(obj))


def test_style_list_falls_back_when_forward_fails(monkeypatch):
    # 回退也要把两个模型都发全——只发第一套会让人以为 v5 那套不存在。
    obj = plugin()
    obj._ctx.config = SimpleNamespace(get=AsyncMock(return_value="10000"))
    obj._ctx.send = SimpleNamespace(text=AsyncMock(), image=AsyncMock(return_value=True),
                                    forward=AsyncMock(side_effect=RuntimeError("timeout")))
    asyncio.run(obj.style_list(stream_id="s"))
    assert obj._ctx.send.image.await_count == len(preview_entries()) * len(_models_with_sheets(obj))


def test_artist_names_match_fuzzy_spellings():
    # 没人记得 Danbooru 的确切拼法，分隔符、大小写、括号后缀都要能认。
    for text in ("misaki kurehito", "misaki_kurehito", "misakikurehito", "Misaki Kurehito"):
        assert match_preset(text).key == "artist_misaki_kurehito", text
    for text in ("mocha", "mocha (cotton)", "mocha_cotton"):
        assert match_preset(text).key == "artist_mocha", text
    for text in ("so-bin", "sobin", "SO-BIN"):
        assert match_preset(text).key == "artist_so-bin", text
    # 中文与日文写法
    assert match_preset("画师momoko").key == "artist_momoko"
    assert match_preset("ももこ").key == "artist_momoko"
    assert match_preset("紺屋鴉江").key == "artist_konya_karasue"


def test_fuzzy_matching_still_respects_word_boundaries():
    # 放宽的只是分隔符和后缀，不是子串边界。
    assert match_preset("1girl, whiten the background") is None
    assert match_preset("1girl, with a corella bird") is None
    assert match_preset("1girl, sitting on bed") is None


def test_artist_name_matching_is_fuzzy():
    from novelai_image.presets import match_artist, artist_presets
    assert len(artist_presets()) == 38
    # 严格写法、大小写、分隔符、只写一半都要认
    assert match_artist("torino aqua").key == "artist_torino_aqua"
    assert match_artist("torino").key == "artist_torino_aqua"
    assert match_artist("wlOp").key == "artist_wlop"
    assert match_artist("misaki_kurehito").key == "artist_misaki_kurehito"
    assert match_artist("mika").key == "artist_mika_pikazo"
    # 太短不给匹配，避免一个字命中一大片
    assert match_artist("a") is None
    assert match_artist("不存在的画师xyz") is None
    # 媒介档不能被当成画师
    assert match_artist("厚涂") is None


def test_view_artist_command_forwards_both_models(monkeypatch):
    obj = plugin()
    obj._ctx.config = SimpleNamespace(get=AsyncMock(side_effect=lambda key, default=None: {
        "bot.qq_account": "10000", "bot.nickname": "麦麦"}.get(key, default)))
    obj._ctx.send = SimpleNamespace(text=AsyncMock(), image=AsyncMock(return_value=True),
                                    forward=AsyncMock(return_value=True))
    result = asyncio.run(obj.style_artist(stream_id="s", matched_groups={"name": "torino"}))
    assert result[0] is True
    messages = obj._ctx.send.forward.await_args.args[0]
    cfg = obj.config
    # 第一条是说明，之后每个模型一条，发的是原图而不是缩略图
    assert "torino aqua" in messages[0]["segments"][0]["content"]
    assert [m["segments"][0]["content"] for m in messages[1:]] == [
        f"v4.5 · {cfg.api.model}", f"v5 · {cfg.api.hd_model}"]
    for item in messages[1:]:
        assert [segment["type"] for segment in item["segments"]] == ["text", "image"]
    obj._ctx.send.image.assert_not_awaited()


def test_view_artist_reports_unknown_names(monkeypatch):
    obj = plugin()
    obj._ctx.send = SimpleNamespace(text=AsyncMock(), image=AsyncMock(return_value=True),
                                    forward=AsyncMock(return_value=True))
    result = asyncio.run(obj.style_artist(stream_id="s", matched_groups={"name": "没这个人"}))
    assert result[0] is True
    assert "没找到画师" in obj._ctx.send.text.call_args.args[0]
    obj._ctx.send.forward.assert_not_awaited()


def test_consumed_cel_keyword_is_not_sent_to_translation(monkeypatch):
    obj = plugin()
    translator = AsyncMock()
    translator.translate_many.return_value = {"positive": "rain, wet clothes"}
    monkeypatch.setattr(obj, "_translator", lambda: translator)
    asyncio.run(obj._plan_request(parse_args("雨中湿身，赛璐璐 --模型 高清"), "a", "qq"))
    sent = translator.translate_many.call_args.args[0]["positive"]
    assert "赛璐璐" not in sent
    assert "雨中湿身" in sent
    assert "预设画风" in translator.translate_many.call_args.kwargs["style_context"]
