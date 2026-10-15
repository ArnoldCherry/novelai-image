"""露骨标签分层：通用质感 / 女性解剖 / 男根 / 具体行为，并挡住自发生长的兽耳。"""
from novelai_image.adaptive import (
    R18_BASE,
    R18_FACE,
    R18_FEMALE,
    R18_NEGATIVE_STYLE,
    R18_PENIS,
    explicit_negatives,
    explicit_tags,
)


def test_explicit_scene_reasserts_face_detail():
    # 露骨场景的注意力被解剖细节带走，脸会被挤小、拉长、眼睛被发丝挡住；
    # 面部细节要无条件补回，不能随性别分支（女性解剖层）一起被跳过。
    assert R18_FACE
    for text in ("1girl, nude", "1boy, male, penis, masturbation"):
        tags = explicit_tags(text)
        for tag in R18_FACE:
            assert tag in tags, f"{text} 缺 {tag}"
    # 非露骨场景不得掺进来。
    assert explicit_tags("1girl, sitting in a dark room, looking at viewer") == []


def test_sfw_scene_gets_nothing():
    for text in ("1girl, sitting in a dark room, looking at viewer",
                 "1girl, solo, standing in the rain, umbrella",
                 "2girls, classroom, daytime"):
        assert explicit_tags(text) == []
        assert explicit_negatives(text) == []


def test_base_and_female_layers_always_applied():
    tags = explicit_tags("1girl, nude")
    for tag in R18_BASE + R18_FEMALE:
        assert tag in tags


def test_masturbation_act_adds_spread_and_focus_without_penis():
    tags = explicit_tags("刻晴脱光衣服自慰，用手抠自己阴部")
    for tag in ("spread legs", "pussy focus", "fingering", "solo"):
        assert tag in tags
    # 单人自慰加男根标签是反向破坏。
    for tag in R18_PENIS:
        assert tag not in tags


def test_doggystyle_act_adds_partner_tags():
    tags = explicit_tags("甘雨被后入，插入")
    for tag in ("from behind", "doggystyle", "ass focus"):
        assert tag in tags
    for tag in R18_PENIS:
        assert tag in tags


def test_blowjob_act_is_not_pussy_focused():
    tags = explicit_tags("1girl, blowjob, fellatio")
    for tag in ("fellatio", "penis in mouth", "saliva"):
        assert tag in tags
    # 口交不是私处焦点场景，不该带上张开腿那套。
    assert "spread pussy" not in tags
    assert "pussy focus" not in tags


def test_creampie_act_adds_overflow_tags():
    tags = explicit_tags("1girl, creampie, 中出")
    for tag in ("cum in pussy", "semen", "overflow"):
        assert tag in tags


def test_male_only_scene_skips_female_anatomy():
    tags = explicit_tags("1boy, male, penis, masturbation")
    assert R18_PENIS[0] in tags
    for tag in R18_FEMALE:
        assert tag not in tags


def test_added_acts_carry_their_own_tags():
    cases = {
        "1girl, 素股, thigh sex": "penis between thighs",
        "1girl, 深喉, deepthroat": "irrumatio",
        "1girl, nude, 舔阴, cunnilingus": "licking pussy",
        "1girl, nude, 浴室, shower": "wet hair",
        "1girl, nude, lingerie": "lingerie",
        "1girl, nude, 三人, threesome": "3some",
        "1girl, nude, 捆绑, shibari": "shibari",
    }
    for text, expected in cases.items():
        assert expected in explicit_tags(text), text


def test_non_explicit_scenes_never_gate_in():
    # 「比基尼海滩」「在浴室」本身不是露骨内容，不该被追加 R18 标签。
    for text in ("1girl, bikini, beach, daytime", "1girl, in the bathroom, brushing teeth",
                 "1girl, panties, changing clothes"):
        assert explicit_tags(text) == []
        assert explicit_negatives(text) == []


def test_framing_layer_matches_the_shot():
    assert "close-up" in explicit_tags("1girl, nude, close-up")
    assert "full body" in explicit_tags("1girl, nude, full body")
    assert "from above" in explicit_tags("1girl, nude, 俯视")
    assert "male pov" in explicit_tags("1girl, nude, 第一人称")
    # 只是看向镜头不算第一人称视角。
    tags = explicit_tags("1girl, nude, looking at viewer")
    assert "pov" not in tags
    assert "male pov" not in tags


def test_explicit_negatives_block_censorship_and_hand_defects():
    neg = explicit_negatives("1girl, nude, pussy")
    for tag in ("censored", "mosaic censoring", "bar censor", "fused fingers", "malformed hands"):
        assert tag in neg
    assert set(neg) == set(R18_NEGATIVE_STYLE)


def test_topic_words_do_not_trigger():
    # 「胸针」「露出度」这类词不该被当成露骨内容。
    assert explicit_tags("1girl, wearing a 胸针, 露出度低") == []


def test_female_anatomy_is_weighted_and_keeps_the_wet_group_local():
    # 柔化画风会磨平结构，分面标签要加权。
    assert any(tag.startswith("1.3::") and "inner labia" in tag for tag in R18_FEMALE)
    for tag in ("detailed vulva", "vaginal opening"):
        assert tag in R18_FEMALE
    # 湿滑组撑的是表面高光，不能摘；但必须留在私处的局部加权块里——
    # 放回 R18_BASE 会作用于整张图，把全身画成油亮写实，跟普通图的柔雾画风明显不同。
    assert any("glistening" in tag for tag in R18_FEMALE)
    assert any("wet pussy" in tag for tag in R18_FEMALE)
    for word in ("wet", "glistening", "moist"):
        assert not any(word in tag for tag in R18_BASE), word


def test_soft_pink_shading_is_gone():
    # 它会把粉红染出私处范围，整片腹股沟发红。
    assert "soft pink shading" not in R18_BASE
