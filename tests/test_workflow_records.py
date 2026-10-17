import asyncio
import base64
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image

from novelai_image.exceptions import NovelAIError
from test_plugin import plugin


def _image() -> str:
    buffer = BytesIO()
    Image.new("RGB", (40, 60), (90, 120, 180)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def test_workflow_records_forward_each_task_id_with_its_image():
    obj = plugin()
    owner = ("qq", "stream", "user")
    image = _image()
    ids = [
        obj._workflow_store.record(
            owner,
            {
                "input": f"scene {index}",
                "model": "nai-diffusion-5-full",
                "parameters": {"width": 512, "height": 768, "seed": index},
            },
            [{"image": image, "seed": index}],
            f"绘图 {index}",
        )
        for index in range(6)
    ]
    obj._ctx.config = SimpleNamespace(
        get=AsyncMock(side_effect=lambda key, default=None: {
            "bot.qq_account": "10000",
            "bot.nickname": "麦麦",
        }.get(key, default))
    )
    obj._ctx.send.forward = AsyncMock(return_value=True)

    asyncio.run(obj._workflow_send_records(owner, "stream", "1"))
    first_page = obj._ctx.send.forward.await_args.args[0]
    assert len(first_page) == 5
    first_page_ids = set()
    for node in first_page:
        segments = node["segments"]
        assert segments[0]["type"] == "text"
        assert "快捷操作：/画图复刻" in segments[0]["content"]
        assert len(segments) == 2 and segments[1]["type"] == "image"
        first_page_ids.add(segments[0]["content"].split("任务号：", 1)[1].splitlines()[0])
    assert first_page_ids.issubset(set(ids))

    asyncio.run(obj._workflow_send_records(owner, "stream", "2"))
    second_page = obj._ctx.send.forward.await_args_list[1].args[0]
    assert len(second_page) == 1
    second_page_id = second_page[0]["segments"][0]["content"].split("任务号：", 1)[1].splitlines()[0]
    assert first_page_ids.isdisjoint({second_page_id})
    assert first_page_ids | {second_page_id} == set(ids)


def test_workflow_records_reject_invalid_page():
    obj = plugin()
    owner = ("qq", "stream", "user")

    try:
        asyncio.run(obj._workflow_send_records(owner, "stream", "0"))
    except NovelAIError as exc:
        assert "页码从1开始" in str(exc)
    else:
        raise AssertionError("page zero should be rejected")
