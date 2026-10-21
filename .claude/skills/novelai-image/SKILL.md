---
name: novelai-image
description: Work on the MaiBot novelai_image plugin — NovelAI image generation, style presets (media/artist), multi-character prompts, workflow commands (replay/edit/compare/favorite/series/director/local), and the card-style help deck. Use when editing or debugging this plugin, adding presets, regenerating reference images, or running its tests.
---

# novelai_image 插件

MaiBot 的 NovelAI 文生图插件。仓库根目录就是插件目录（`plugins/novelai_image`）。

## 快速上手

```bash
# 依赖已在 MaiBot 的 venv 里
cd <MaiBot 根目录>
./.venv/Scripts/python.exe -m pytest plugins/novelai_image/tests -q
```

- 运行环境：`MaiBot\.venv\Scripts\python.exe`（不是插件目录下的 venv）。
- 令牌只从环境变量 `NOVELAI_API_TOKEN` 读取；**任何文件、日志、提交里都不得出现令牌**。
- `config.toml` 已 gitignore，改动配置不要提交。

## 目录结构

| 路径 | 作用 |
|---|---|
| `plugin.py` | 插件主体：命令、请求规划、配置模型、帮助发送 |
| `parser.py` | 参数解析（`--模型 4.5/5`、比例、种子……） |
| `presets.py` | 画风预设：媒介档 + 画师档、关键词匹配、参考图 |
| `default_style_policy.py` | 默认柔光与多人柔光的核心标签 |
| `adaptive.py` | R18 标签注入、动作/构图规则 |
| `multichar.py` | 多角色分栏、坐标、caption 组装 |
| `nanogpt.py` / `translator.py` | 中文翻译（NanoGPT / Ollama） |
| `local_edit.py` | `/画图局部`：视觉定位 + 整图 img2img + 选区回贴 |
| `workflow.py` / `workflow_store.py` | 任务、收藏、版本、系列、导演等 |
| `workflow_api.py` | 导演工具接口（`/ai/augment-image`） |
| `workflow_help.py` | 卡片式帮助图（普通帮助 + 工作流示例） |
| `client.py` | NovelAI HTTP 客户端（生成、导演） |
| `assets/styles/` | 各画风参考图（4.5 用）与 `v5/`（仅预览比对） |

## 关键约定

### 模型默认值
- `/画图`、`/画图检查`、`/画图预览`：4.5 Full。
- 其余命令默认 5 Full；`--模型 4.5`（或 `v4.5`）切回，`--模型 5` 显式指定。
- 继承型命令（复刻/修改/局部/系列/导演）沿用来源任务模型；`/画图画风` 只能 4.5。

### 画风预设
- 两段式：先 `match_preset()` 命中，再按模型取 `tags` 或 `tags_v5`。
- 关键词走整词匹配，边界只按 `[A-Za-z0-9_]` 判定（`rella画风` 能命中，`corella` 不会）。
- 新增媒介档：在 `PRESETS` 加 `StylePreset`，准备 `assets/styles/<key>.png` 与 `v5/<key>.png`，
  并在 `assets/style_recipes_v5.json` 写入 V5 快照（tags/negative/scale/steps/sampler）。
- 新增画师档：条目要带参考图；4.5 的参考图会作为 Precise Reference 发送。
- **4.5 里部分画师标签（如 `artist:machi`）几乎无效**：同种子加/不加标签差异极小。
  需要 4.5 与 V5 对齐时，用「同配方的 V5 出图」当 4.5 的参考图，比堆文字标签有效。
- V5 参考图常自己脱衣服；要让参考图保持统一服装，提示词里显式写 `black witch hat, black coat,
  white shirt, gold bowtie`。

### 局部重绘
- V5 的 `infill` 端点**不认遮罩**（角落遮罩也会整张重绘），不要回退到 infill。
- 现行方案：整图 `img2img` 0.75 保构图 + 物体选区回贴。
- 取色遮罩：定位框外扩 3%，核心高饱和 + 仅在核心附近放宽；被替换的旧标签写进负面。

### 帮助图
- `/画图帮助`：15 页（5 普通 + 10 工作流），编号连续；`/画图工作流` 只发 10 页。
- 卡片版式：命令格式（大字）→ 示例（小字缩进）→ 说明 → 生成图。
- 发送降级：整条合并转发 → 对半拆 → 逐张直发 → 文字。NapCat 的转发资源上传会偶发整条失败。

### 多角色
- 结构化拆分走 `translate_scene()`；输出预算 `768 + 512×人数`，写满动作的长描述会被截断。
- 分栏写法：`全局：…-角色1：…-角色2：…`，最多 6 人。

## 常用脚本

```bash
# 生成工作流帮助素材（会调用 NovelAI，注意计费）
./.venv/Scripts/python.exe plugins/novelai_image/scripts/build_workflow_demo.py
./.venv/Scripts/python.exe plugins/novelai_image/scripts/build_workflow_extra_demo.py
```

## 排错

- `NovelAI 参数或模型不兼容`：多半是把图片元数据里的字段整包塞进了请求；只复制
  width/height/seed/steps/scale/sampler 与 prompt/uc。
- `上传转发消息资源失败`：NapCat 侧问题，不是插件；保留逐张降级即可。
- 帮助图里有透明区域时用棋盘格显示；判定阈值是 alpha < 128（NovelAI 的 alpha 常为 254）。
