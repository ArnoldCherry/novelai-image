"""使用本机配置和插件真实请求助手，生成工作流帮助页的 NovelAI 示例素材。"""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import asyncio
import base64
import copy
import hashlib
import json
import sys
import tempfile
import time
import tomllib
import shutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from novelai_image.plugin import ADMIN_ID, NovelAIConfig, NovelAIImagePlugin
from novelai_image.client import build_payload
from novelai_image.workflow import WorkflowPayload
from novelai_image.workflow_api import director

OUTPUT = ROOT / "assets" / "help" / "workflow"
BASELINE = ROOT / "assets" / "help" / "default-softlight-baseline" / "parameters.json"
RECIPE_VERSION = 7
V5_MODEL = "nai-diffusion-5-full"
ELAINA_TAG = "elaina_(majo_no_tabitabi)"


def restore_original_baseline(payload, identity):
    """从原图的 NovelAI Comment 恢复 V5 请求，只替换独立角色栏。"""
    baseline = json.loads(BASELINE.read_text("utf-8"))
    source = baseline["parameters"]
    restored = copy.deepcopy(payload)
    restored["model"] = V5_MODEL
    restored["input"] = source["prompt"]
    params = restored["parameters"]
    # 只搬采样相关的值，不整包复制图片元数据：那份 Comment 里带着 lora_unet_weights、
    # controlnet_model、director_reference_* 等 v5 接口会直接拒绝的字段。
    for key in ("width", "height", "seed", "steps", "scale", "sampler"):
        params[key] = copy.deepcopy(source[key])
    params["negative_prompt"] = source["uc"]
    params["v4_prompt"]["caption"]["base_caption"] = source["prompt"]
    params["v4_prompt"]["caption"]["char_captions"] = [
        {"char_caption": ELAINA_TAG, "centers": [{"x": 0.5, "y": 0.5}]}
    ]
    params["v4_negative_prompt"]["caption"]["base_caption"] = source["uc"]
    params["v4_negative_prompt"]["caption"]["char_captions"] = []
    style = copy.deepcopy(payload.style)
    if style is not None:
        style["tags"] = ["cel shading" if tag == "0.5::cel shading::" else tag for tag in style.get("tags", [])]
    audit_path = OUTPUT / "original-baseline.request.json"
    audit = {
        "source": baseline["source"],
        "source_model": baseline["source_model"],
        "pixel_identical_to_user_reference": baseline["pixel_identical_to_user_reference"],
        "reference_sha256": hashlib.sha256((BASELINE.parent / "reference.png").read_bytes()).hexdigest(),
        "source_parameters": source,
        "derived_model": restored["model"],
        "character_caption_replacement": {"from": "ganyu_(genshin_impact)", "to": ELAINA_TAG},
        "planner_identity": identity,
        "series_style": style,
        "derived_payload": restored,
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), "utf-8")
    return WorkflowPayload(restored, style, identity)


async def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    manifest_path = OUTPUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8")) if manifest_path.exists() else {
        "source": "NovelAI official API via novelai_image plugin helpers",
        "seed": 20261018,
        "images": {},
    }
    if manifest.get("recipe_version") != RECIPE_VERSION:
        archive = ROOT.parents[1] / "data/plugins/local.novelai-image" / f"help-demo-archive-20261018-v{manifest.get('recipe_version', 1)}"
        archive.mkdir(parents=True, exist_ok=True)
        for previous in OUTPUT.iterdir():
            if previous.is_file():
                shutil.copy2(previous, archive / previous.name)
        manifest = {"source": "NovelAI official API; native default soft-light planner for Elaina; separate presets only in style examples", "seed": 20261018, "recipe_version": RECIPE_VERSION, "images": {}}
    with (ROOT / "config.toml").open("rb") as handle:
        config = NovelAIConfig.model_validate(tomllib.load(handle))
    plugin = NovelAIImagePlugin()
    plugin._plugin_config_instance = config
    with tempfile.TemporaryDirectory(prefix="novelai-help-demo-") as temp_dir:
        plugin._ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=Path(temp_dir)))
        await plugin.on_load()
        client = plugin._client()

        async def generate(name, payload, command):
            if payload.get("model") != V5_MODEL:
                raise RuntimeError(f"Help demo generation must use {V5_MODEL}; got {payload.get('model')!r}")
            path = OUTPUT / f"{name}.png"
            if path.is_file() and name in manifest["images"]:
                print("cached", name, flush=True)
                return base64.b64encode(path.read_bytes()).decode("ascii")
            started = time.monotonic()
            result = (await client.generate(payload))[0]
            path.write_bytes(base64.b64decode(result["image"]))
            manifest["images"][name] = {
                "file": path.name, "command": command, "model": payload["model"],
                "seed": result.get("seed", payload["parameters"]["seed"]),
                "prompt": payload["input"], "elapsed_seconds": round(time.monotonic() - started, 2),
            }
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
            print("saved", name, manifest["images"][name]["elapsed_seconds"], flush=True)
            return result["image"]

        async def plan(name, text, no_style=False):
            path = OUTPUT / f"{name}.request.json"
            if path.is_file():
                saved = json.loads(path.read_text("utf-8"))
                if saved.get("recipe_version") == RECIPE_VERSION:
                    return WorkflowPayload(saved["payload"], saved["style"], saved["identity"])
            # 全部画风参数经过实际规划器，固定英文只用于排除翻译随机性。
            scene = "1girl, standing, holding umbrella, rain, wet clothes, soaked hair, raindrops, puddles, outdoors, rainy day, overcast sky, street scene"
            if name == "favorite":
                scene = "1girl, sitting, drinking tea, teacup, window, indoors, upper body"
            scene += f" | {ELAINA_TAG}"
            if name == "base":
                scene = f"伊蕾娜在雨中撑伞，全身 | {ELAINA_TAG}"
            req = plugin._workflow_parse(scene + " --模型 高清 --种子 20261018")
            if no_style:
                req = replace(req, count=1, style_weight=0, reference_mode="off")
            translation_enabled = config.translation.enabled
            config.translation.enabled = name == "base" and translation_enabled
            try:
                _, payload, *_ = await plugin._plan_request(req, ADMIN_ID, "qq")
            finally:
                config.translation.enabled = translation_enabled
            if name == "base":
                payload = restore_original_baseline(payload, payload.identity)
            if payload.get("model") != V5_MODEL:
                raise RuntimeError(f"Help demo plan must use {V5_MODEL}; got {payload.get('model')!r}")
            path.write_text(json.dumps({"recipe_version": RECIPE_VERSION, "payload": payload, "style": payload.style, "identity": payload.identity}, ensure_ascii=False, indent=2), "utf-8")
            manifest["plans_version"] = RECIPE_VERSION
            return payload

        try:
            base = await plan("base", "伊蕾娜在雨中撑伞，全身")
            source = await generate("base", base, "/高清画图 伊蕾娜在雨中撑伞，全身 --种子 121823221")
            await generate("replay", base, "/画图复刻 回复")
            modified = await plugin._workflow_edit(base, "red scarf")
            await generate("modified", modified, "/画图修改 回复 | red scarf")
            neutral = await plan("comparison", "伊蕾娜坐在花园里读书，黑色魔女袍，半身", True)
            for key, style_name in (("cel", "赛璐璐"), ("watercolor", "水彩")):
                payload = plugin._workflow_apply_style(neutral, plugin._workflow_recipe(style_name, neutral["model"]))
                await generate(key, payload, "/画图对比 赛璐璐-水彩 伊蕾娜坐在花园里读书 --模型 高清 --种子 20261018")
            tea = await plan("favorite", "伊蕾娜在窗边喝茶，半身", True)
            tea = plugin._workflow_apply_style(tea, plugin._workflow_recipe("水彩", tea["model"]))
            await generate("favorite", tea, "/画图套用 伊蕾娜水彩 伊蕾娜在窗边喝茶 --模型 高清 --种子 20261018")
            # 场景里必须带服装：系列只沿用角色名，漏掉 outfit 时"海边夕阳"会被画成泳装。
            for key, scene in (("series_garden", "sitting on a bench, reading a book, flower garden"), ("series_seaside", "standing, seaside, sunset, looking at viewer, full body, black witch outfit")):
                payload = plugin._workflow_text(base, scene, append=False)
                if base.style:
                    payload = plugin._workflow_apply_style(payload, base.style)
                await generate(key, payload, "/画图系列 回复 | sitting on a bench, reading a book, flower garden | standing, seaside, sunset, looking at viewer")
            for tool, prompt in (("emotion", "happy"), ("lineart", ""), ("removebg", "")):
                key = "director_" + tool
                path = OUTPUT / f"{key}.png"
                if key not in manifest["images"]:
                    result = (await director(client, source, tool, prompt))[0]
                    path.write_bytes(base64.b64decode(result["image"]))
                    manifest["images"][key] = {"file": path.name, "command": f"/画图导演 回复 | {tool}" + (" | " + prompt if prompt else ""), "source_file": "base.png", "api": "/ai/augment-image"}
                    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
                print("saved", key, flush=True)
            path = OUTPUT / "director_colorize.png"
            if "director_colorize" not in manifest["images"]:
                lineart = base64.b64encode((OUTPUT / "director_lineart.png").read_bytes()).decode("ascii")
                # 只给命令里写明的内容；额外加雨天/湿发会让上色结果整体变暗蓝。
                result = (await director(client, lineart, "colorize", "silver hair, blue eyes, black witch outfit"))[0]
                path.write_bytes(base64.b64decode(result["image"]))
                manifest["images"]["director_colorize"] = {"file": path.name, "command": "/画图导演 回复 上色 浅灰发、蓝色眼睛、黑色魔女袍", "source_file": "director_lineart.png", "api": "/ai/augment-image"}
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
            print("DONE", flush=True)
        finally:
            await plugin.on_unload()


if __name__ == "__main__":
    asyncio.run(main())
