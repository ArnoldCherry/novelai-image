from novelai_image.exceptions import NovelAIError
from novelai_image.parser import MODEL_ALIASES, SIZE_PRESETS, SMALL_SIZE_PRESETS, parse_args
import pytest

@pytest.mark.parametrize("ratio", list(SMALL_SIZE_PRESETS))
def test_small_size_table(ratio):
    # /画小图 用这张表：像素值必须与 Normal 档区分开，否则落不到订阅无限额度那一档。
    req = parse_args("castle --比例 " + ratio, size_presets=SMALL_SIZE_PRESETS)
    assert (req.width, req.height) == SMALL_SIZE_PRESETS[ratio]
    assert (req.width, req.height) != SIZE_PRESETS[ratio]

def test_size_table_keys_stay_aligned():
    assert list(SMALL_SIZE_PRESETS) == list(SIZE_PRESETS)

def test_plain():
    req = parse_args("星空下的城堡")
    assert req.prompt == "星空下的城堡"
    assert req.model == MODEL_ALIASES["普通"]
    assert 0 <= req.seed < 2**32

@pytest.mark.parametrize("ratio", list(SIZE_PRESETS))
def test_ratios(ratio):
    req = parse_args("castle --比例 " + ratio)
    assert (req.width, req.height) == SIZE_PRESETS[ratio]

@pytest.mark.parametrize("args", ["--比例 横图 --种子 123 --步数 28 --数量 2 --模型 高清 --负面 low quality, bad hands", "--ratio 横图 --seed 123 --steps 28 --count 2 --model 高清 --negative 'low quality, bad hands'"])
def test_aliases_spaces(args):
    req = parse_args("castle " + args)
    assert req.seed == 123 and req.count == 2 and req.model == MODEL_ALIASES["高清"]
    assert req.negative.startswith("low quality, bad hands")

@pytest.mark.parametrize("token,expected", [("4.5", "普通"), ("v4.5", "普通"), ("5", "高清"), ("v5", "高清")])
def test_model_can_be_named_by_version(token, expected):
    # 除 /画图 外默认 5；想回 4.5 就直接写 --模型 4.5。
    assert parse_args(f"castle --模型 {token}").model == MODEL_ALIASES[expected]

def test_configured_alias_wins_over_version_shorthand():
    req = parse_args("castle --模型 5", model_aliases={"5": "custom-model"})
    assert req.model == "custom-model"

@pytest.mark.parametrize("args", ["", "--seed 1", "castle --seed -1", "castle --seed 4294967296", "castle --steps 0", "castle --steps 51", "castle --count 3", "castle --unknown foo", "castle --steps", "castle --seed 1 --seed 2", "castle --ratio 任意", "castle --model fake", 'castle --negative "bad'])
def test_invalid(args):
    with pytest.raises(NovelAIError):
        parse_args(args)

def test_length():
    with pytest.raises(NovelAIError):
        parse_args("a" * 3001)

def test_nsfw_prompt_passes_through():
    req = parse_args("nsfw nude portrait")
    assert req.prompt == "nsfw nude portrait"
    assert "nsfw" not in req.negative
