"""雾化组的场景门控：暗室内景保留，依赖色彩或光线的场景摘掉并改压过曝。"""
from novelai_image.adaptive import (
    BRIGHT_NEGATIVE,
    DEFAULT_NEGATIVE_STYLE,
    DEFAULT_STYLE,
    HAZE_STYLE,
    HD_DEFAULT_NEGATIVE_STYLE,
    HD_DEFAULT_ONLY_NEGATIVE_STYLE,
    HD_DEFAULT_STYLE,
    default_negative_style,
    drop_haze,
    scene_keeps_haze,
    scene_negatives,
)


def test_dim_indoor_keeps_haze():
    for text in ("1girl, solo, sitting in a dark room at night, looking at viewer",
                 "1girl, lying on bed, bedroom, warm lamp light",
                 "1girl, nude, on bed, indoors"):
        assert scene_keeps_haze(text) is True


def test_color_critical_scenes_drop_haze():
    for text in ("1girl, standing on a city street at night, rain, neon lights",
                 "2girls, classroom, daytime, desks",
                 "1girl, walking in a forest, sunlight through trees, daytime",
                 "1girl, standing in snow, winter, outdoors, coat",
                 "1girl, beach, sunset, ocean, standing",
                 "1boy, sitting in a cafe, indoors, window",
                 "1girl, standing on a rooftop, city, sky"):
        assert scene_keeps_haze(text) is False


def test_drop_haze_keeps_soft_lighting_and_desaturation():
    # 用户认可的是柔色本身，过亮才要压；去色组必须留下。
    kept = drop_haze(list(DEFAULT_STYLE))
    for tag in ("soft lighting", "diffused lighting", "soft shading", "muted colors",
                "desaturated", "low saturation", "subdued colors", "low contrast"):
        assert tag in kept
    for tag in HAZE_STYLE:
        assert tag not in kept


def test_bright_scene_negatives_block_blowout_and_keep_color_discipline():
    bright = scene_negatives("1girl, beach, sunset, ocean", DEFAULT_NEGATIVE_STYLE)
    for tag in BRIGHT_NEGATIVE:
        assert tag in bright
    # 去色档不能放开：放开会让暖调场景整体偏橘。
    for tag in ("vivid colors", "saturated", "high contrast"):
        assert tag in bright
    # 舞台光仍然要挡，不能因为亮场景就放进来。
    assert "dramatic lighting" in bright


def test_dim_scene_negatives_unchanged():
    assert scene_negatives("1girl, sitting in a dark room", DEFAULT_NEGATIVE_STYLE) == list(DEFAULT_NEGATIVE_STYLE)


def test_haze_tags_are_all_in_default_style():
    assert HAZE_STYLE <= set(DEFAULT_STYLE)


def test_hd_style_drops_muting_and_adds_color():
    # 5 自身偏柔偏灰，去色与雾化整组都不列，改成补对比与饱和。
    hd = set(HD_DEFAULT_STYLE)
    for tag in ("muted colors", "dull colors", "desaturated", "low saturation",
                "faded colors", "subdued colors", "low contrast",
                "soft bloom", "hazy", "atmospheric haze", "dreamy"):
        assert tag not in hd
    for tag in ("contrast", "saturated colors", "vivid colors"):
        assert tag in hd
    # 柔光与柔化渲染是共用的，5 也要保留。
    for tag in ("soft lighting", "diffused lighting", "soft shading"):
        assert tag in hd


def test_hd_style_is_pinned_not_derived():
    # 5 的画风已定稿，写死存放：4.5 每轮调整都不该顺手改掉 5。
    assert set(HD_DEFAULT_STYLE) <= set(DEFAULT_STYLE) | {"contrast", "saturated colors", "vivid colors"}
    for tag in ("airbrushed shading", "glossy hair", "blurred lineart", "soft focus"):
        assert tag in HD_DEFAULT_STYLE
    assert "cel shading" not in HD_DEFAULT_STYLE


def test_hd_style_carries_the_anime_anchor():
    # 缺这组锚点时，5 在 R18 下会把脸一起渲染成写实；普通场景不会暴露，所以要显式钉住。
    for tag in ("anime style", "2d", "clean lineart"):
        assert tag in HD_DEFAULT_STYLE
        assert tag in DEFAULT_STYLE


def test_hd_negative_keeps_color_open():
    # 恢复原始柔光排除词，不混入后续实验用的额外默认排除。
    assert set(HD_DEFAULT_NEGATIVE_STYLE) == {"dramatic lighting", "harsh shadows"}
    assert tuple(HD_DEFAULT_ONLY_NEGATIVE_STYLE) == ()
    for tag in ("vivid colors", "saturated", "high contrast"):
        assert tag not in HD_DEFAULT_NEGATIVE_STYLE
        assert tag in DEFAULT_NEGATIVE_STYLE


def test_default_negative_style_picks_by_model():
    assert default_negative_style(hd=True) == list(HD_DEFAULT_NEGATIVE_STYLE)
    assert default_negative_style(hd=False) == list(DEFAULT_NEGATIVE_STYLE)


def test_default_style_has_no_oily_skin_group():
    # 油光组会把身体推成半写实，细节改由 painterly 那几支承担。
    for tag in ("oiled skin", "shiny skin", "luminous skin"):
        assert tag not in DEFAULT_STYLE


def test_default_style_keeps_detail_without_heavy_muting():
    for tag in ("anime style", "2d", "clean lineart", "painterly", "detailed texture",
                "delicate features"):
        assert tag in DEFAULT_STYLE
    # 重压去色会连暖色一起压掉，皮肤变灰粉，整条不再保留。
    assert not any(tag.startswith("1.3::muted colors") for tag in DEFAULT_STYLE)


def test_default_negatives_block_the_photo_drift():
    for tag in ("realistic skin", "photorealistic", "hyperrealistic", "3d render", "skin pores"):
        assert tag in DEFAULT_NEGATIVE_STYLE
    # 舞台光仍然要挡。
    assert "dramatic lighting" in DEFAULT_NEGATIVE_STYLE
