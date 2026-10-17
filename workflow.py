"""可撤销的个人绘图工作流；所有来源均限制在同平台、聊天和用户。"""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Tuple
import asyncio
import re
import shlex

from maibot_sdk import Command

from .exceptions import NovelAIError
from .parser import DrawRequest, MODEL_ALIASES, parse_args
from .presets import match_preset, preset_render_tags
from .workflow_store import WorkflowStore
from .workflow_help import workflow_help_text


def workflow_parts(op: str, args: str):
    """按操作解析空格参数；仅在序列/替换位置使用连字符。兼容旧分栏语法。"""
    if "|" in args:
        return [part.strip() for part in args.split("|")]
    if not args:
        return [""]
    if op == "收藏":
        return args.rsplit(None, 1) if args != "列表" else [args]
    matched = re.fullmatch(r'''(?:"([^"]+)"|'([^']+)'|(\S+))(?:\s+([\s\S]*))?''', args)
    if not matched:
        raise NovelAIError("参数格式无效，请查看 /画图工作流 的示例。")
    head = next(value for value in matched.groups()[:3] if value is not None)
    tail = (matched[4] or "").strip()
    if op == "对比":
        styles = head.split("-", 1)
        return [*styles, tail] if len(styles) == 2 else [head]
    if op == "系列":
        # 汉语场景可直接用 - 衔接；英文连字符单词保留，英文场景用空格 - 空格衔接。
        scenes = re.split(r"\s+-\s+|-(?=[\u4e00-\u9fff])|(?<=[\u4e00-\u9fff])-", tail)
        return [head, *[scene.strip() for scene in scenes]] if tail else [head]
    if op == "导演":
        return [head, *tail.split(None, 1)] if tail else [head]
    return [head, tail] if tail else [head]


class WorkflowPayload(dict):
    """JSON 请求保持原样，画风来源只作为本地属性传递。"""
    def __init__(self, payload, style, identity=None):
        super().__init__(payload)
        self.style = style
        self.identity = identity or []


WORKFLOW_HELP = workflow_help_text()


class WorkflowMixin:
    _WORKFLOW_RECORDS_PER_PAGE = 5

    def _workflow_init(self) -> None:
        cfg = self.config.workflow
        self._workflow_store = WorkflowStore(Path(self.ctx.paths.data_dir) / "workflow", ttl_hours=cfg.retention_hours, retain_images=cfg.persist_images)
        self._workflow_tasks = {}
        self._workflow_replies = {}

    async def _workflow_cleanup(self):
        while True:
            await asyncio.sleep(60)
            self._workflow_store.prune()
            owners = {owner for owner, _ in self._workflow_replies}
            live_ids = {row["id"] for owner in owners for row in self._workflow_store.list_jobs(owner)}
            self._workflow_replies = {key: value for key, value in self._workflow_replies.items() if value in live_ids}

    def _workflow_owner(self, platform: str, stream_id: str, user_id: str) -> Tuple[str, str, str]:
        owner = (str(platform), str(stream_id), str(user_id))
        if not all(owner):
            raise NovelAIError("无法识别平台、聊天或用户。")
        return owner

    def _workflow_access(self, user_id: str, group_id: str) -> None:
        cfg = self.config
        if not cfg.plugin.enabled or self._drawing_disabled():
            raise NovelAIError("绘图当前已关闭。")
        if cfg.permission.mode == "whitelist" and user_id not in cfg.permission.user_ids and group_id not in cfg.permission.group_ids:
            raise NovelAIError("你或当前群不在绘图允许名单中。")

    def _workflow_recipe(self, text: str, model: str) -> Dict[str, Any]:
        preset = match_preset(text)
        hd = model == MODEL_ALIASES["高清"]
        if preset:
            return {"label": preset.label, "tags": list(preset_render_tags(preset, v5=hd)), "negative": list(preset.extra_negatives), "drop_negative": list(preset.drop_negatives)}
        cfg = self.config.style
        return {"label": "个人默认画风", "tags": list(cfg.hd_default_tags if hd else cfg.default_tags), "negative": list(cfg.hd_default_negative_tags if hd else cfg.default_negative_tags), "drop_negative": []}

    def _workflow_parse(self, text: str) -> DrawRequest:
        """对比/套用是从零起一张新图：除 /画图 外一律默认 V5，写 --模型 4.5 才回 4.5。"""
        cfg = self.config
        return parse_args(text, default_model=cfg.api.hd_model, default_ratio=cfg.generation.default_ratio, default_steps=cfg.generation.default_steps, default_negative=cfg.generation.default_negative_prompt, max_images=cfg.generation.max_images, max_prompt_length=cfg.generation.max_prompt_length, model_aliases=cfg.api.model_aliases)

    @staticmethod
    def _workflow_req(payload: Dict[str, Any]) -> DrawRequest:
        p = payload["parameters"]
        return DrawRequest(payload["input"], p.get("negative_prompt", ""), payload["model"], p["width"], p["height"], p["steps"], p["seed"], p["n_samples"])

    @staticmethod
    def _workflow_text(payload: Dict[str, Any], text: str, append: bool = True) -> Dict[str, Any]:
        if not text.strip() or len(text) > 1500:
            raise NovelAIError("英文标签须为1～1500字符。")
        if any(ord(c) > 127 for c in text):
            raise NovelAIError("本次标签转换未完成，请重试中文描述。")
        result = deepcopy(payload)
        result["input"] = (result["input"] + ", " if append else "") + text
        if len(result["input"]) > 6000:
            raise NovelAIError("组合后的提示词过长。")
        result["parameters"]["v4_prompt"]["caption"]["base_caption"] = result["input"]
        result["parameters"]["n_samples"] = 1
        return result

    async def _workflow_translate_delta(self, text: str, *, exact: bool = False) -> str:
        if not text or len(text) > 1500:
            raise NovelAIError("修改内容须为1～1500字符。")
        if any("\u4e00" <= char <= "\u9fff" for char in text):
            if not self.config.translation.enabled:
                raise NovelAIError("当前关闭了中文翻译，请使用英文标签。")
            translator = self._translator()
            if self.config.translation.backend == "nanogpt":
                if exact:
                    text = await translator.translate_edit_tag(text)
                    translated = {"positive": text}
                else:
                    translated = await translator.translate_many({"positive": text}, style_context="沿用原任务画风；仅适度扩充本次场景或动作，不扩充画风、光影、脸部质感或角色身份")
            else:
                translated = await translator.translate_many({"positive": text})
            text = translated.get("positive", "").strip()
            if not text:
                raise NovelAIError("修改内容翻译为空。")
        return text

    async def _workflow_edit(self, payload, delta):
        if "=>" not in delta and "-" in delta:
            caption = payload["parameters"]["v4_prompt"]["caption"]
            texts = [payload["input"]] + [item["char_caption"] for item in caption.get("char_captions", [])]
            tags = {tag.strip().casefold() for text in texts for tag in text.split(",")}
            candidates = []
            for match in re.finditer("-", delta):
                old, new = delta[:match.start()].strip(), delta[match.end():].strip()
                explicit = " - " in delta or (delta.count("-") == 1 and re.search(r"[\u4e00-\u9fff]", delta))
                if old and new and (old.casefold() in tags or explicit):
                    candidates.append((old, new))
            if len(candidates) == 1:
                delta = "=>".join(candidates[0])
            elif len(candidates) > 1:
                raise NovelAIError("替换关系不明确，请使用 旧标签 - 新标签，一次只替换一组。")
        natural = re.search(r"(衣服|服装|连衣裙|裙子|外套|衬衫|裤子|头发|眼睛)(?:换成|改成|变成|改为)(黑|白|红|蓝|绿|金|银|粉|紫|棕)(?:色)?", delta)
        if natural and "=>" not in delta:
            colors = {"黑": "black", "白": "white", "红": "red", "蓝": "blue", "绿": "green", "金": "blonde" if natural[1] == "头发" else "gold", "银": "silver", "粉": "pink", "紫": "purple", "棕": "brown"}
            targets = {"衣服": r"dress|skirt|shirt|blouse|coat|jacket|outfit|clothes|clothing|pants|trousers|uniform", "服装": r"dress|skirt|shirt|blouse|coat|jacket|outfit|clothes|clothing|pants|trousers|uniform", "连衣裙": "dress", "裙子": "dress|skirt", "外套": "coat|jacket", "衬衫": "shirt|blouse", "裤子": "pants|trousers", "头发": "hair", "眼睛": "eyes"}
            pattern = re.compile(r"\b(?:black|white|red|blue|green|gold|golden|blonde|silver|pink|purple|brown|grey|gray)([ _]+)(" + targets[natural[1]] + r")\b", re.I)
            result = deepcopy(payload)
            caption = result["parameters"]["v4_prompt"]["caption"]
            texts = [result["input"]] + [item["char_caption"] for item in caption.get("char_captions", [])]
            changed = [pattern.sub(lambda match: colors[natural[2]] + match[1] + match[2], text) for text in texts]
            if changed == texts:
                raise NovelAIError("原任务里没有可精确识别的该部位颜色标签，请使用 旧标签-新标签 显式替换。")
            result["input"] = caption["base_caption"] = changed[0]
            for item, text in zip(caption.get("char_captions", []), changed[1:], strict=True):
                item["char_caption"] = text
            result["parameters"]["n_samples"] = 1
            return result
        if "=>" not in delta:
            return self._workflow_text(payload, await self._workflow_translate_delta(delta))
        old, new = [part.strip() for part in delta.split("=>", 1)]
        old = await self._workflow_translate_delta(old, exact=True)
        new = await self._workflow_translate_delta(new, exact=True)
        result = deepcopy(payload)
        caption = result["parameters"]["v4_prompt"]["caption"]
        texts = [result["input"]] + [item["char_caption"] for item in caption.get("char_captions", [])]
        # 替换必须精确命中完整标签，拒绝在没有命中的情况下留下相互矛盾的描述。
        changed = False
        replaced = []
        for text in texts:
            tags = [tag.strip() for tag in text.split(",")]
            updated = [new if tag.casefold() == old.casefold() else tag for tag in tags]
            changed = changed or tags != updated
            replaced.append(", ".join(updated))
        if not changed:
            raise NovelAIError("旧标签未精确命中。请从 /画图检查 的英文标签复制旧词，再使用 旧标签-新标签。")
        result["input"] = caption["base_caption"] = replaced[0]
        for item, text in zip(caption.get("char_captions", []), replaced[1:], strict=True):
            item["char_caption"] = text
        result["parameters"]["n_samples"] = 1
        return result

    def _workflow_resolve(self, owner, job_id: str, message: Any):
        if job_id == "回复":
            if not isinstance(message, dict):
                raise NovelAIError("当前消息没有回复元数据，请填写任务号。")
            segments = message.get("raw_message", [])
            reply = str(message.get("reply_to") or "")
            if not reply:
                reply = next((str(p.get("target_message_id") or p.get("id") or p.get("message_id") or (p.get("data", {}).get("target_message_id") if isinstance(p.get("data"), dict) else "") or "") for p in segments if isinstance(p, dict) and p.get("type") == "reply"), "")
            job_id = self._workflow_replies.get((owner, reply), "")
            if not job_id:
                raise NovelAIError("无法关联此回复，请使用生成完成消息中的任务号。")
        try:
            result = self._workflow_store.get(owner, job_id)
        except ValueError:
            raise NovelAIError("任务号格式无效，请复制生成完成消息中的任务号。") from None
        if result is None:
            raise NovelAIError("任务不存在、已过期，或不属于本人在当前聊天的记录。")
        return result

    def _workflow_favorite(self, owner, name):
        row = self._workflow_store.get_favorite(owner, name)
        if row is None or not row.get("snapshot"):
            raise NovelAIError("收藏画风不存在，请使用 /画图收藏 列表 查看名称。")
        return row

    async def _workflow_record(self, owner, payload, images, label: str, style=None):
        identity = payload.identity if isinstance(payload, WorkflowPayload) else []
        job_ids = []
        for index, image in enumerate(images, 1):
            selected = deepcopy(payload)
            selected["parameters"]["n_samples"] = 1
            if isinstance(image.get("seed"), int):
                selected["parameters"]["seed"] = image["seed"]
            image_label = label + (f" · 图{index}" if len(images) > 1 else "")
            job_id = self._workflow_store.record(owner, selected, [image], image_label, style=style, identity=identity)
            job_ids.append(job_id)
            sent = await self.ctx.send.text(f"{'图' + str(index) + ' · ' if len(images) > 1 else ''}任务号：{job_id}（仅本人在当前聊天可用；/画图记录 查看）", owner[1], return_details=True)
            if isinstance(sent, dict) and sent.get("sent") is True and sent.get("message_id"):
                self._workflow_replies[(owner, str(sent["message_id"]))] = job_id
        return job_ids

    async def _workflow_deliver(self, owner, group_id, payload, images, label, style=None):
        job_ids = await self._workflow_record(owner, payload, images, label, style)
        req = self._workflow_req(payload)
        if group_id and group_id in self.config.generation.collapse_groups:
            if not await self._send_collapsed(images, req, owner[1]):
                self._save_pending_delivery(images, owner[1])
                raise NovelAIError("图片已生成，合并转发失败，原图已保存供管理员补发。任务号：" + "、".join(job_ids))
        else:
            for index, image in enumerate(images):
                job_id = job_ids[index]
                sent = await self.ctx.send.image(image["image"], owner[1], return_details=True)
                if sent is not True and not (isinstance(sent, dict) and sent.get("sent") is True):
                    raise NovelAIError("图片生成成功，但发送失败；任务号：" + job_id)
                if isinstance(sent, dict):
                    message_id = str(sent.get("message_id") or sent.get("id") or "")
                    if message_id:
                        self._workflow_replies[(owner, message_id)] = job_id
        await self.ctx.send.text(f"{label}完成；种子：" + "、".join(str(image.get("seed", req.seed)) for image in images), owner[1])

    @Command("novelai_workflow", description="个人绘图任务、画风收藏和多图工作流", pattern=r"^/画图(?P<op>工作流|复刻|修改|对比|收藏|套用|版本|回滚|系列|导演|局部|反馈|推荐|记录|清除|取消|状态)(?:\s+(?P<args>[\s\S]*))?$", timeout_ms=900_000)
    async def workflow(self, stream_id: str = "", user_id: str = "", group_id: str = "", platform: str = "", matched_groups=None, message=None, **kwargs):
        owner = None
        gate = None
        gate_key = ""
        registered = False
        try:
            self._validate_config()
            owner = self._workflow_owner(platform, stream_id, user_id)
            self._workflow_access(str(user_id), str(group_id))
            op = (matched_groups or {}).get("op", "工作流")
            args = ((matched_groups or {}).get("args") or "").strip()
            parts = workflow_parts(op, args)
            store = self._workflow_store
            if op == "工作流":
                await self._send_help_images(stream_id, workflow_only=True)
                return True, "工作流帮助", 1
            if op == "取消":
                task = self._workflow_tasks.get(owner)
                if task and task is not asyncio.current_task():
                    task.cancel()
                pending = self._pending_style.pop(owner, None)
                await self.ctx.send.text("已取消等待和本地任务；若请求已送达 NovelAI，费用可能仍产生。" if task or pending else "当前没有进行中的工作流任务。", stream_id)
                return True, "取消已处理", 1
            if op == "状态":
                await self.ctx.send.text("生成处理中" if owner in self._workflow_tasks else ("等待画风参考图" if owner in self._pending_style else "空闲"), stream_id)
                return True, "状态", 1
            if op == "清除":
                if owner in self._workflow_tasks:
                    raise NovelAIError("请先 /画图取消，待任务结束后再清除。")
                store.clear(owner)
                self._pending_style.pop(owner, None)
                self._workflow_replies = {key: value for key, value in self._workflow_replies.items() if key[0] != owner}
                await self.ctx.send.text("已清除本人在当前聊天的任务、图片、收藏和反馈。", stream_id)
                return True, "已清除", 1
            if op == "记录":
                await self._workflow_send_records(owner, stream_id, args)
                return True, "任务记录", 1
            if op in ("收藏", "版本", "回滚", "反馈", "推荐"):
                await self._workflow_personal(owner, op, args, parts, message)
                return True, "个人画风已处理", 1
            if owner in self._workflow_tasks:
                raise NovelAIError("当前工作流正在处理中，请先等待或 /画图取消。")
            from .plugin import JobGate
            settings = (self.config.limits.max_concurrent_jobs, self.config.limits.cooldown_seconds)
            if self._gate is None or (self._gate_settings != settings and not self._gate.active):
                self._gate, self._gate_settings = JobGate(*settings), settings
            gate, gate_key = self._gate, platform + ":" + str(user_id)
            gate.reserve(gate_key)
            self._workflow_tasks[owner] = asyncio.current_task()
            registered = True
            await self.ctx.send.text("工作流已开始，完成的每一张都会独立发送任务号；/画图取消 可取消后续步骤。", stream_id)
            async with gate.semaphore:
                async with asyncio.timeout(800):
                    await self._workflow_generate(owner, str(group_id), op, parts, message)
        except asyncio.CancelledError:
            if stream_id:
                await self.ctx.send.text("工作流已取消；已提交的请求可能仍计费。", stream_id)
            raise
        except NovelAIError as exc:
            await self.ctx.send.text(str(exc), stream_id)
        except TimeoutError:
            await self.ctx.send.text("工作流超时，已停止后续步骤。", stream_id)
        except Exception as exc:
            from .plugin import logger
            logger.error("绘图工作流失败，异常类型：%s", type(exc).__name__)
            await self.ctx.send.text("工作流失败，请管理员检查插件日志。已完成的任务可以在 /画图记录 查看。", stream_id)
        finally:
            if owner is not None and self._workflow_tasks.get(owner) is asyncio.current_task():
                self._workflow_tasks.pop(owner, None)
            if registered:
                gate.release(gate_key)
        return True, "工作流已处理", 1

    async def _workflow_generate(self, owner, group_id: str, op: str, parts, message):
        if op in ("对比", "套用"):
            expected = 3 if op == "对比" else 2
            if len(parts) != expected or not all(parts):
                raise NovelAIError("用法：/画图对比 画风A-画风B 描述；/画图套用 收藏名 描述")
            req = self._workflow_parse(parts[-1])
            # 先独立准备无画风的场景和人物，只翻译一次；两侧仅替换画风配方。
            base_req = replace(req, count=1, style_weight=0, reference_mode="off")
            _, base, _, _, _, _ = await self._plan_request(base_req, owner[2], owner[0])
            for label in parts[:-1]:
                self._workflow_access(owner[2], group_id)
                if op == "套用":
                    style = self._workflow_favorite(owner, label)["snapshot"]
                else:
                    preset = match_preset(label)
                    if preset is None:
                        style = self._workflow_favorite(owner, label)["snapshot"]
                    else:
                        style = self._workflow_recipe(label, req.model)
                payload = self._workflow_apply_style(base, style)
                images = await self._client().generate(payload)
                await self._workflow_deliver(owner, group_id, payload, images, ("画风对比 · " if op == "对比" else "收藏画风 · ") + label, style)
            return
        if not parts or not parts[0]:
            raise NovelAIError("请填写任务号；/画图工作流 查看用法。")
        job = self._workflow_resolve(owner, parts[0], message)
        self._workflow_access(owner[2], group_id)
        payload = WorkflowPayload(deepcopy(job["payload"]), job.get("style"), job.get("identity"))
        if op in ("复刻", "修改", "系列") and not job.get("replay_available", True):
            raise NovelAIError("此任务的图片参考未持久保存，重启后无法原参数复用。请重新生成，或由管理员启用 workflow.persist_images 后生成新任务。")
        if op == "复刻":
            if len(parts) != 1:
                raise NovelAIError("用法：/画图复刻 任务号")
            # 使用记录的已翻译、已匹配角色的请求，禁止重新规划造成漂移。
            images = await self._client().generate(payload)
            await self._workflow_deliver(owner, group_id, payload, images, "原参数复刻", job.get("style"))
        elif op == "修改":
            if len(parts) != 2:
                raise NovelAIError("用法：/画图修改 任务号 给甘雨加一条红围巾")
            payload = await self._workflow_edit(payload, parts[1])
            images = await self._client().generate(payload)
            await self._workflow_deliver(owner, group_id, payload, images, "同种子增补（构图可能变化）", job.get("style"))
        elif op == "系列":
            if not 2 <= len(parts) <= 4:
                raise NovelAIError("用法：/画图系列 任务号 花园读书-海边散步（1～3幕）")
            chars = payload["parameters"]["v4_prompt"]["caption"].get("char_captions", [])
            if not chars and job.get("identity"):
                chars = [{"char_caption": text, "centers": [{"x": 0.5, "y": 0.5}]} for text in job["identity"]]
                payload["parameters"]["v4_prompt"]["caption"]["char_captions"] = chars
            if not chars:
                source_images = job.get("images", [])
                if not source_images:
                    raise NovelAIError("此任务无角色栏且源图未保留，无法确认单人身份，请重新生成源图。")
                from .local_edit import infer_single_identity

                identity = await infer_single_identity(payload, source_images[0]["image"], self.ctx.llm)
                chars = [{"char_caption": identity, "centers": [{"x": .5, "y": .5}]}]
                payload["parameters"]["v4_prompt"]["caption"]["char_captions"] = chars
                await self.ctx.send.text("已识别原图为单个角色并自动固定身份，无需预先分栏。", owner[1])
            source = job.get("images", [])
            if payload["model"] == MODEL_ALIASES["普通"] and source:
                # 身份参考只锚定人物，不继承原图场景；V5只使用固定角色栏。
                p = payload["parameters"]
                p["director_reference_images"] = [source[0]["image"]]
                p["director_reference_descriptions"] = [{"caption": {"base_caption": "character", "char_captions": []}, "use_coords": False, "use_order": True}]
                p["director_reference_information_extracted"] = [1.0]
                p["director_reference_strength_values"] = [0.75]
                p["director_reference_secondary_strength_values"] = [0.6]
                await self.ctx.send.text("系列固定角色栏与种子；4.5使用原图角色参考，每幕额外5 Anlas。身份一致性仍受模型影响。", owner[1])
            else:
                await self.ctx.send.text("系列固定角色栏与种子；当前模型或图片状态只支持文字身份约束。", owner[1])
            for index, scene in enumerate(parts[1:], 1):
                self._workflow_access(owner[2], group_id)
                current = self._workflow_text(payload, await self._workflow_translate_delta(scene), append=False)
                style = job.get("style")
                if style:
                    current = self._workflow_apply_style(current, style)
                images = await self._client().generate(current)
                await self._workflow_deliver(owner, group_id, current, images, f"角色系列 · 第{index}幕", style)
        elif op == "导演":
            if not 2 <= len(parts) <= 3:
                raise NovelAIError("用法：/画图导演 任务号 表情 开心微笑；也可使用 去背景、线稿、上色")
            if not job.get("images"):
                raise NovelAIError("原图已过期或重启后未保留，请重新生成。")
            from .workflow_api import director
            prompt = await self._workflow_translate_delta(parts[2]) if len(parts) == 3 else ""
            images = await director(self._client(), job["images"][0]["image"], parts[1], prompt)
            # 导演输出参数与生成参数不同，保留来源供继续编辑，复刻明确回到生成源。
            await self._workflow_deliver(owner, group_id, payload, images, "导演工具 · " + parts[1] + "（复刻会使用生成源参数）", job.get("style"))
        elif op == "局部":
            if len(parts) != 2 or not parts[1]:
                raise NovelAIError("用法：/画图局部 任务号 把脖子上的围巾改成蓝色")
            if not job.get("images"):
                raise NovelAIError("原图已过期或未保留，请先重新生成。")
            from .local_edit import prepare_text_edit, preserve_outside

            source = job["images"][0]["image"]
            edited_payload, mask, location = await prepare_text_edit(
                payload, source, parts[1], self.ctx.llm, self._workflow_translate_delta,
            )
            await self.ctx.send.text(f"已通过{location['method']}选中「{location.get('target', '指定区域')}」，正在局部重绘。", owner[1])
            images = await self._client().generate(edited_payload)
            for image in images:
                image["image"] = await asyncio.to_thread(preserve_outside, source, image["image"], mask)
            await self._workflow_deliver(owner, group_id, edited_payload, images, "中文局部重绘", job.get("style"))
        else:
            raise NovelAIError("未知工作流操作。")

    @staticmethod
    def _workflow_apply_style(base, style):
        payload = deepcopy(base)
        tags = style.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise NovelAIError("收藏画风格式无效。")
        # 画风块必须排在最前：原请求就是这么生成的，追加到末尾会被场景标签压过去，
        # 同一套默认柔光在系列幕和套用结果里会明显偏赛璐璐。
        payload["input"] = ", ".join(tags) + ", " + payload["input"]
        params = payload["parameters"]
        params["v4_prompt"]["caption"]["base_caption"] = payload["input"]
        negatives = [tag.strip() for tag in params.get("negative_prompt", "").split(",") if tag.strip() and tag.strip() not in style.get("drop_negative", [])]
        negatives += style.get("negative", [])
        params["negative_prompt"] = ", ".join(dict.fromkeys(negatives))
        params["v4_negative_prompt"]["caption"]["base_caption"] = params["negative_prompt"]
        return payload

    async def _workflow_send_records(self, owner, stream_id: str, args: str) -> None:
        """按页合并发送任务号和各自的结果图，旧任务仍显示可复制的任务号。"""
        from .plugin import FORWARD_TIMEOUT_MS, shrink_for_forward

        page_text = args.strip()
        if page_text and (not page_text.isdigit() or int(page_text) < 1):
            raise NovelAIError("用法：/画图记录 [页码]，页码从1开始。")
        page = int(page_text) if page_text else 1
        rows = self._workflow_store.list_jobs(owner)
        if not rows:
            await self.ctx.send.text("没有尚未过期的任务记录。", stream_id)
            return

        page_size = self._WORKFLOW_RECORDS_PER_PAGE
        page_count = (len(rows) + page_size - 1) // page_size
        if page > page_count:
            raise NovelAIError(f"只有 {page_count} 页记录，请输入 /画图记录 {page_count} 查看最后一页。")
        selected = rows[(page - 1) * page_size : page * page_size]
        account, nickname = await self._bot_identity()
        forward_nodes = []
        direct_records = []
        for row in selected:
            job = self._workflow_store.get(owner, row["id"])
            if job is None:
                continue
            payload = job.get("payload", {})
            parameters = payload.get("parameters", {}) if isinstance(payload, dict) else {}
            model = str(payload.get("model", "未知模型")) if isinstance(payload, dict) else "未知模型"
            width, height = parameters.get("width"), parameters.get("height")
            size_text = f"{width}×{height}" if isinstance(width, int) and isinstance(height, int) else "未知"
            seed = parameters.get("seed", "未知")
            caption = (
                f"任务号：{row['id']}\n"
                f"记录：{job.get('label') or row.get('label') or '绘图'}\n"
                f"模型：{model} · 尺寸：{size_text} · 种子：{seed}\n"
                f"快捷操作：/画图复刻 {row['id']}\n"
                f"修改示例：/画图修改 {row['id']} 给甘雨加一条红围巾"
            )
            images = job.get("images", [])
            image_data = images[0].get("image") if images and isinstance(images[0], dict) else None
            if not isinstance(image_data, str) or not image_data:
                caption += "\n图片当前不可读取（默认图片只保存在内存，重启或内存回收后无法恢复）。"
                image_data = None
            direct_records.append((caption, image_data))
            segments = [{"type": "text", "content": caption}]
            if image_data:
                image_data = await asyncio.to_thread(shrink_for_forward, image_data, compact=True)
                segments.append({"type": "image", "content": image_data})
            forward_nodes.append({"user_id": account, "nickname": nickname, "segments": segments})

        if not forward_nodes:
            await self.ctx.send.text("这一页的任务记录已过期，请用 /画图记录 查看最新记录。", stream_id)
            return

        if account:
            try:
                result = await self.ctx.send.forward(forward_nodes, stream_id, timeout_ms=FORWARD_TIMEOUT_MS)
            except Exception as exc:
                from .plugin import _describe_error, logger

                logger.warning("绘图记录合并转发结果不确定：%s", _describe_error(exc))
                await self.ctx.send.text(
                    "绘图记录合并转发结果不确定，为避免重复投递没有自动重发；请稍后重新发送 /画图记录。",
                    stream_id,
                )
                return
            sent = result is True or (
                isinstance(result, dict)
                and result.get("success") is not False
                and result.get("sent", result.get("success")) is True
            )
            if sent:
                return

            definitive_failure = result is False or (
                isinstance(result, dict)
                and (result.get("success") is False or result.get("sent") is False)
            )
            if not definitive_failure:
                await self.ctx.send.text(
                    "绘图记录合并转发结果不确定，为避免重复投递没有自动重发；请稍后重新发送 /画图记录。",
                    stream_id,
                )
                return

            from .plugin import logger

            logger.warning("绘图记录合并转发未送达，改为逐条发送")

        await self.ctx.send.text(
            f"绘图记录第 {page}/{page_count} 页（合并转发不可用，以下逐条发送）：", stream_id
        )
        for caption, image_data in direct_records:
            await self.ctx.send.text(caption, stream_id)
            if image_data:
                await self.ctx.send.image(image_data, stream_id)

    async def _workflow_personal(self, owner, op, args, parts, message):
        store = self._workflow_store
        if op == "收藏":
            if args == "列表":
                rows = store.list_favorites(owner)
                text = "\n".join(f"{row['id']} · {row.get('label', '')} · v{row.get('version', 1)}" for row in rows) or "尚无收藏。"
            else:
                if len(parts) != 2 or not parts[0]:
                    raise NovelAIError("用法：/画图收藏 名称 任务号；重复名称创建新版本。")
                job = self._workflow_resolve(owner, parts[1], message)
                if not job.get("style"):
                    raise NovelAIError("此任务没有可独立提取的文字画风，请先使用 /画图对比 生成。")
                row = store.save_favorite(owner, parts[0], job["style"], label=parts[0])
                text = f"画风已收藏：{parts[0]}，版本{row.get('version', 1)}。只保存画风标签；不保存任务人物或场景。"
        elif op == "版本":
            row = self._workflow_favorite(owner, args)
            text = "\n".join(f"v{item['version']} · {item.get('created_at', '')}" for item in row.get("history", [])) or f"当前版本：{row.get('version', 1)}"
        elif op == "回滚":
            tokens = shlex.split(args)
            if len(tokens) != 2 or not tokens[1].isdigit():
                raise NovelAIError("用法：/画图回滚 收藏名 版本号")
            self._workflow_favorite(owner, tokens[0])
            try:
                row = store.rollback_favorite(owner, tokens[0], int(tokens[1]))
            except ValueError:
                raise NovelAIError("该版本不存在或已超出保留范围，请用 /画图版本 查看。") from None
            text = f"已回滚画风 {tokens[0]}；当前版本：{row.get('version', 1)}。"
        elif op == "反馈":
            tokens = args.split()
            if len(tokens) != 2 or tokens[1] not in ("喜欢", "不喜欢"):
                raise NovelAIError("用法：/画图反馈 任务号 喜欢（或 不喜欢）")
            job = self._workflow_resolve(owner, tokens[0], message)
            store.record_feedback(owner, job["id"], liked=tokens[1] == "喜欢")
            text = "已记录明确反馈，只用于本人在当前聊天的画风推荐；不会自动改动默认画风。"
        else:
            rows = store.recommended(owner)
            text = "\n".join(f"{row.get('id', '')} · {row.get('label', '')}" for row in rows) or "尚无有明确好评的收藏画风。先收藏画风，再对相应任务发送 喜欢 反馈。"
        await self.ctx.send.text(text, owner[1])
