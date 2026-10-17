from types import SimpleNamespace
from unittest.mock import AsyncMock
from novelai_image.exceptions import NovelAIError
from novelai_image.parser import MODEL_ALIASES
from novelai_image.plugin import ADMIN_ID, JobGate, NovelAIConfig, NovelAIImagePlugin
import asyncio
import base64
import re
import tempfile
import time

import pytest

def test_gate():
    gate = JobGate(1, 25)
    gate.reserve("a")
    with pytest.raises(NovelAIError, match="处理中"):
        gate.reserve("a")
    with pytest.raises(NovelAIError, match="忙碌"):
        gate.reserve("b")
    gate.release("a")
    with pytest.raises(NovelAIError, match="冷却"):
        gate.reserve("a")
    gate.reserve("b")

def test_gate_takes_a_per_job_cooldown():
    # 高清按张数累计：默认 25 秒，5 Full 一张 40 秒。
    gate = JobGate(4, 25)
    gate.reserve("heavy", 80)
    gate.release("heavy")
    with pytest.raises(NovelAIError, match="冷却"):
        gate.reserve("heavy")
    gate.reserve("light")
    gate.release("light")
    with pytest.raises(NovelAIError, match="冷却"):
        gate.reserve("light")
    assert gate.until["heavy"] - gate.until["light"] == pytest.approx(55, abs=1)

class _CtxConfig:
    """宿主配置的异步 get 垫片；多页帮助要用它取机器人账号做合并转发。"""

    def __init__(self, values):
        self._values = values

    async def get(self, key, default=""):
        return self._values.get(key, default)


def plugin():
    obj = NovelAIImagePlugin()
    obj._plugin_config_instance = NovelAIConfig()
    obj._ctx = SimpleNamespace(
        send=SimpleNamespace(text=AsyncMock(), image=AsyncMock(return_value=True),
                             forward=AsyncMock(return_value=True)),
        paths=SimpleNamespace(data_dir=tempfile.mkdtemp()),
        config=_CtxConfig({"bot.qq_account": "10000", "bot.nickname": "麦麦"}),
    )
    asyncio.run(obj.on_load())
    return obj

def test_help():
    obj = plugin()
    asyncio.run(obj.help(stream_id="stream"))
    # 帮助排成图发出去，不再丢一大段文字。多页时走合并转发，单页时直接发图。
    sent = obj._ctx.send.image.call_args or obj._ctx.send.forward.call_args
    assert sent is not None
    obj._ctx.send.text.assert_not_awaited()

def test_help_stays_on_its_own_name():
    from maibot_sdk import components
    from novelai_image.plugin import HELP
    pattern = getattr(NovelAIImagePlugin.help, components._COMPONENT_INFO_ATTR).command_pattern
    assert re.match(pattern, "/画图帮助")
    # /绘图帮助 是 codex 绘图插件的，本插件不接，免得两个插件抢同一条指令。
    assert not re.match(pattern, "/绘图帮助")
    assert not re.match(pattern, "/画图帮助我")
    # 现有指令都要在里面，别漏了新增的 /画小图。
    for name in ("/画图 ", "/画图画风", "/画图检查", "/高清画图", "/画小图",
                 "/打开画图", "/关闭画图", "/查看画风", "/画风预览"):
        assert name in HELP, name

def test_host_lifecycle_contract():
    from maibot_sdk import MaiBotPlugin
    assert NovelAIImagePlugin.on_load is not MaiBotPlugin.on_load
    assert NovelAIImagePlugin.on_unload is not MaiBotPlugin.on_unload
    assert NovelAIImagePlugin.on_config_update is not MaiBotPlugin.on_config_update
    obj = plugin()
    asyncio.run(obj.on_config_update("self", {}, "1"))
    asyncio.run(obj.on_unload())

@pytest.mark.parametrize("kind,alias", [("画图",None),("高清画图",None),("画图","高清")])
def test_model_routing(kind, alias, monkeypatch):
    monkeypatch.setenv("NOVELAI_API_TOKEN", "MOCK_ONLY_NOT_A_REAL_CREDENTIAL")
    mock = AsyncMock(return_value=[{"image":"fake", "seed":123}])
    monkeypatch.setattr("novelai_image.plugin.NovelAIClient.generate", mock)
    obj = plugin()
    asyncio.run(obj.draw(stream_id="s",user_id=ADMIN_ID,platform="qq",matched_groups={"kind":kind,"args":"castle" + (" --模型 " + alias if alias else "")}))
    expected = MODEL_ALIASES["高清" if kind == "高清画图" or alias else "普通"]
    assert mock.call_args.args[0]["model"] == expected
    assert obj._ctx.send.image.call_args.args == ("fake", "s")

@pytest.mark.parametrize("args,size", [
    ("少女坐在窗边", (512, 768)),
    ("少女坐在窗边 --比例 横图", (768, 512)),
    ("少女全身立绘", (512, 768)),          # 自动比例改画布，也必须留在 Small 档
])
def test_small_command_stays_on_the_small_canvas(args, size, monkeypatch):
    # /画小图 的意义就是落在订阅无限额度覆盖的那档画布上。尺寸被自动比例改回 Normal
    # 档就白扣一次 Anlas，所以固定比例和自动比例两条路径都要盯住。
    monkeypatch.setenv("NOVELAI_API_TOKEN", "MOCK_ONLY_NOT_A_REAL_CREDENTIAL")
    mock = AsyncMock(return_value=[{"image": "fake", "seed": 123}])
    monkeypatch.setattr("novelai_image.plugin.NovelAIClient.generate", mock)
    obj = plugin()
    translator = SimpleNamespace(translate_many=AsyncMock(return_value={"positive": "1girl, sitting, window"}))
    monkeypatch.setattr(obj, "_translator", lambda: translator)
    asyncio.run(obj.draw(stream_id="s", user_id=ADMIN_ID, platform="qq",
                         matched_groups={"kind": "画小图", "args": args}))
    payload = mock.call_args.args[0]
    assert payload["model"] == MODEL_ALIASES["高清"]
    assert (payload["parameters"]["width"], payload["parameters"]["height"]) == size

@pytest.mark.parametrize("kind", ["画小图", "高清画图"])
def test_only_the_plain_hd_command_gets_the_long_cooldown(kind, monkeypatch):
    # 小图那档不计费，按 5 Full 的 40 秒/张卡着只是让人白等；正常画布的高清则照旧。
    monkeypatch.setenv("NOVELAI_API_TOKEN", "MOCK_ONLY_NOT_A_REAL_CREDENTIAL")
    monkeypatch.setattr("novelai_image.plugin.NovelAIClient.generate",
                        AsyncMock(return_value=[{"image": "fake", "seed": 1}]))
    obj = plugin()
    asyncio.run(obj.draw(stream_id="s", user_id=ADMIN_ID, platform="qq",
                         matched_groups={"kind": kind, "args": "castle"}))
    limits = obj.config.limits
    elapsed = obj._gate.until["qq:" + ADMIN_ID] - time.monotonic()
    expected = limits.cooldown_seconds if kind == "画小图" else limits.hd_cooldown_seconds
    assert expected - 5 <= elapsed <= expected


@pytest.mark.parametrize("kind,args", [("高清画图","castle"),("画图","castle --模型 高清")])
def test_hd_allowed_for_non_admin(kind,args,monkeypatch):
    monkeypatch.setenv("NOVELAI_API_TOKEN", "MOCK_ONLY_NOT_A_REAL_CREDENTIAL")
    mock = AsyncMock(return_value=[{"image":"fake", "seed":123}])
    monkeypatch.setattr("novelai_image.plugin.NovelAIClient.generate", mock)
    obj = plugin()
    asyncio.run(obj.draw(stream_id="s",user_id="other",platform="qq",matched_groups={"kind":kind,"args":args}))
    assert mock.call_args.args[0]["model"] == MODEL_ALIASES["高清"]

@pytest.mark.parametrize("result", [False, {"sent":False,"message_id":None}])
def test_send_failure(monkeypatch, result):
    monkeypatch.setenv("NOVELAI_API_TOKEN", "MOCK_ONLY_NOT_A_REAL_CREDENTIAL")
    monkeypatch.setattr("novelai_image.plugin.NovelAIClient.generate", AsyncMock(return_value=[{"image":"fake", "seed":1}]))
    obj = plugin()
    obj._ctx.send.image.return_value = result
    asyncio.run(obj.draw(stream_id="s",user_id=ADMIN_ID,platform="qq",matched_groups={"kind":"画图","args":"castle"}))
    assert "发送失败" in obj._ctx.send.text.call_args.args[0]
    assert not obj._gate.active

@pytest.mark.parametrize("platform", ["discord", "telegram"])
def test_hd_allowed_on_any_platform(platform, monkeypatch):
    monkeypatch.setenv("NOVELAI_API_TOKEN", "MOCK_ONLY_NOT_A_REAL_CREDENTIAL")
    mock = AsyncMock(return_value=[{"image":"fake", "seed":123}])
    monkeypatch.setattr("novelai_image.plugin.NovelAIClient.generate", mock)
    obj = plugin()
    asyncio.run(obj.draw(stream_id="s",user_id="other",platform=platform,matched_groups={"kind":"高清画图","args":"castle"}))
    assert mock.call_args.args[0]["model"] == MODEL_ALIASES["高清"]

def test_send_result_success(monkeypatch):
    monkeypatch.setenv("NOVELAI_API_TOKEN", "MOCK_ONLY_NOT_A_REAL_CREDENTIAL")
    monkeypatch.setattr("novelai_image.plugin.NovelAIClient.generate", AsyncMock(return_value=[{"image":"fake", "seed":1}]))
    obj = plugin()
    obj._ctx.send.image.return_value = {"sent": True, "message_id": "1"}
    asyncio.run(obj.draw(stream_id="s",user_id=ADMIN_ID,platform="qq",matched_groups={"kind":"画图","args":"castle"}))
    assert "生成完成" in obj._ctx.send.text.call_args.args[0]


def test_each_command_is_declared_on_its_own_handler():
    # @Command 是 setattr 覆盖式的：一个函数叠两个装饰器，前面那个的名字与正则会被覆盖掉。
    # 曾经 novelai_draw 与 style_list 叠在同一个函数上，结果 /画图 走进了画风列表，
    # 而 /查看画风 谁都匹配不上、被 LLM 接管。
    from maibot_sdk import components
    import inspect
    seen = {}
    for name, fn in inspect.getmembers(NovelAIImagePlugin, predicate=inspect.isfunction):
        info = getattr(fn, components._COMPONENT_INFO_ATTR, None)
        if info is None:
            continue
        command = getattr(info, "name", name)
        assert command not in seen, f"{command} 同时挂在 {seen[command]} 与 {name} 上"
        seen[command] = name


def test_draw_and_style_list_do_not_cross_wires():
    from maibot_sdk import components
    from novelai_image.plugin import NovelAIImagePlugin
    def pattern_of(handler):
        info = getattr(handler, components._COMPONENT_INFO_ATTR, None)
        assert info is not None, f"{handler.__name__} 没有注册成命令"
        return info.command_pattern
    draw = pattern_of(NovelAIImagePlugin.draw)
    style = pattern_of(NovelAIImagePlugin.style_list)
    for text in ("/画图 猫", "/高清画图 猫", "/画小图 猫"):
        assert re.match(draw, text), text
        assert not re.match(style, text), text
    for text in ("/查看画风", "/画风列表"):
        assert re.match(style, text), text
        assert not re.match(draw, text), text
