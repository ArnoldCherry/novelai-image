"""生成中文视觉局部编辑、多人分栏和导演上色的实际帮助素材。"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import asyncio
import base64
import json
import os
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
MAIBOT = ROOT.parents[1]
sys.path[:0] = [str(ROOT.parent), str(MAIBOT)]
sys.stdout.reconfigure(encoding="utf-8")

from novelai_image.client import build_payload
from novelai_image.local_edit import prepare_text_edit, preserve_outside
from novelai_image.plugin import NovelAIConfig, NovelAIImagePlugin
from novelai_image.workflow_api import director

OUTPUT = ROOT / "assets/help/workflow"
V5_MODEL = "nai-diffusion-5-full"
# 局部编辑用整图 img2img，模型仍是 V5 本体。
V5_MODELS = {V5_MODEL}
ELAINA_TAG = "elaina_(majo_no_tabitabi)"


class HostVision:
    async def generate(self, prompt, model="vlm", temperature=0, max_tokens=700):
        from src.services import llm_service
        from src.common.data_models.llm_service_data_models import LLMServiceRequest
        result = await llm_service.generate(LLMServiceRequest(task_name=model, request_type="novelai.help-demo", prompt=prompt, temperature=temperature, max_tokens=max_tokens))
        return result.to_capability_payload()


async def main():
    os.chdir(MAIBOT)
    manifest_path = OUTPUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    config = NovelAIConfig.model_validate(tomllib.loads((ROOT / "config.toml").read_text("utf-8")))
    plugin = NovelAIImagePlugin()
    plugin._plugin_config_instance = config
    with tempfile.TemporaryDirectory(prefix="novelai-extra-help-") as temp_dir:
        plugin._ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=Path(temp_dir)), llm=HostVision())
        await plugin.on_load()
        try:
            client = plugin._client()
            base = json.loads((OUTPUT / "base.request.json").read_text("utf-8"))["payload"]
            if base.get("model") != V5_MODEL:
                raise RuntimeError(f"Help demo source must use {V5_MODEL}; got {base.get('model')!r}")
            if "multichar" not in manifest["images"]:
                req = plugin._workflow_parse(f"2girls, flower garden, holding hands, standing, full body, wide shot, facing viewer | {ELAINA_TAG}, silver hair, long hair, blue eyes, black witch outfit, holding hands | ganyu_(genshin_impact), blue hair, long hair, purple eyes, horns, holding hands --模型 高清 --比例 横图 --种子 20261018")
                if req.model != V5_MODEL:
                    raise RuntimeError(f"Help demo multi-character request must use {V5_MODEL}; got {req.model!r}")
                translation = config.translation.enabled
                config.translation.enabled = False
                try:
                    _, payload, *_ = await plugin._plan_request(req, "help-demo", "qq")
                finally:
                    config.translation.enabled = translation
                characters = payload["parameters"]["v4_prompt"]["caption"]["char_captions"]
                if payload.get("model") != V5_MODEL:
                    raise RuntimeError(f"Help demo multi-character generation must use {V5_MODEL}; got {payload.get('model')!r}")
                image = (await client.generate(payload))[0]
                (OUTPUT / "multichar.png").write_bytes(base64.b64decode(image["image"]))
                manifest["images"]["multichar"] = {"file": "multichar.png", "model": req.model, "seed": req.seed, "prompt": payload["input"], "characters": characters,
                    "command": "/高清画图 全局：两人站在花园里牵手，全身-角色1：左侧伊蕾娜，浅灰长发，蓝色眼睛，黑色魔女袍-角色2：右侧甘雨，蓝发紫瞳，头上有角 --比例 横图 --种子 20261018"}
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
                print("saved multichar", flush=True)
            if "local_blue" not in manifest["images"]:
                modified = await plugin._workflow_edit(base, "red scarf")
                source = base64.b64encode((OUTPUT / "modified.png").read_bytes()).decode("ascii")
                if modified.get("model") != V5_MODEL:
                    raise RuntimeError(f"Help demo edit plan must use {V5_MODEL}; got {modified.get('model')!r}")
                payload, mask, location = await prepare_text_edit(modified, source, "把伊蕾娜脖子上的红围巾改成蓝色", plugin.ctx.llm, plugin._workflow_translate_delta)
                if payload.get("model") not in V5_MODELS:
                    raise RuntimeError(f"Help demo local edit generation must use V5; got {payload.get('model')!r}")
                print("location", json.dumps(location, ensure_ascii=False), flush=True)
                image = (await client.generate(payload))[0]
                result = preserve_outside(source, image["image"], mask)
                (OUTPUT / "local_blue.png").write_bytes(base64.b64decode(result))
                (OUTPUT / "local_internal_mask.png").write_bytes(base64.b64decode(mask))
                manifest["images"]["local_blue"] = {"file": "local_blue.png", "source_file": "modified.png", "model": payload["model"], "location": location, "command": "/画图局部 回复 把伊蕾娜脖子上的红围巾改成蓝色"}
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
                print("saved local_blue", flush=True)
            if "director_colorize" not in manifest["images"]:
                lineart = base64.b64encode((OUTPUT / "director_lineart.png").read_bytes()).decode("ascii")
                image = (await director(client, lineart, "colorize", "silver hair, blue eyes, black witch outfit, rainy street, wet hair, rain"))[0]
                (OUTPUT / "director_colorize.png").write_bytes(base64.b64decode(image["image"]))
                print("saved director_colorize", flush=True)
            for tool in ("emotion", "lineart", "removebg", "colorize"):
                manifest["images"]["director_" + tool] = {"file": f"director_{tool}.png", "api": "/ai/augment-image", "source_file": "director_lineart.png" if tool == "colorize" else "base.png", "input_prepared_size": "512x748"}
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
        finally:
            await plugin.on_unload()


if __name__ == "__main__":
    asyncio.run(main())
