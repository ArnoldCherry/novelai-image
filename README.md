# NovelAI 安全文生图插件

独立 MaiBot SDK v2 插件，命令由用户主动触发，直接发送 Base64 到原聊天流，不保存图片。支持文生图、知识库角色参考与上传画风参考；参考图使用官方 Precise Reference。

## 安装与启动

1. 插件目录放在 `MaiBot/plugins/novelai_image`。依赖使用 MaiBot 的虚拟环境：在 MaiBot 目录执行 `uv pip install --python .venv/Scripts/python.exe -r plugins/novelai_image/requirements.txt`。
2. 已提供 `config.example.toml`；复制为同目录 `config.toml`。本次部署实际配置已设为 `plugin.enabled = true`，旧 `local-nai-draw` 已禁用以避免同名命令冲突；旧配置备份为 `config.toml.before-novelai-api.bak`。
3. 在 NovelAI 官方站点的账号设置中找到 Persistent API Token，撤销泄露的旧令牌并生成新令牌。不要把新令牌发到聊天或写入配置。
4. **先停止已有 MaiBot**，在 MaiBot 目录运行 `powershell -ExecutionPolicy Bypass -File .\plugins\novelai_image\scripts\start-maibot-with-token.ps1`。脚本隐藏输入新令牌，只设置当前进程的 `NOVELAI_API_TOKEN`，运行结束后清理；它不会保存令牌。使用该脚本启动时，MaiBot 会继承本次令牌。
5. NapCat 与适配器沿用现有启动方式；此插件不改动它们。

如使用已有服务管理器，应由管理员在服务环境中注入 `NOVELAI_API_TOKEN` 后重启服务。PowerShell 环境变量语法为 `$env:NOVELAI_API_TOKEN = '<仅供示例的新令牌占位符>'`；不要将真实令牌输入会保存历史的命令行，建议使用上述隐藏输入脚本。没有环境变量时插件只返回配置提示，不访问付费接口。

## 命令

```text
/画图帮助
/画图画风 小虚坐在窗边，穿着白色外套
# 随后在原聊天单独上传一张画风参考图
/打开画图
/关闭画图
/画图 castle under a starry sky --比例 横图 --种子 123456
/画图 clean lineart, riverside town --比例 方图 --步数 28
/画图 landscape --负面 low quality, blurry, bad perspective --数量 2
/高清画图 castle, sunset --比例 横图
/画小图 castle, sunset
/查看画风
/画图 少女坐在窗边，锐利线稿
/画图 少女坐在窗边，wlop
```

`/画图` 默认模型为 `nai-diffusion-4-5-full`，`/高清画图` 默认 `nai-diffusion-5-full`。高清命令及通过 `--模型 高清` 选择高清模型均对符合绘图允许名单的用户开放。

`/画小图` 同样是 `nai-diffusion-5-full`，但画布锁在 Small 档：竖 512×768、横 768×512、方 640×640。订阅（Opus）的无限生成只覆盖这一档，超出就按尺寸×步数计 Anlas——所以想要不扣点数就用这条命令，`--比例` 仍可切换朝向，自动比例改画布时也会留在 Small 档。它豁免 `[limits].hd_cooldown_seconds`：那一档不计费，按 5 Full 的长冷却卡着只是让人白等，只走 `cooldown_seconds`。

参数支持 `--比例/--ratio`、`--种子/--seed`、`--步数/--steps`、`--负面/--negative`、`--数量/--count`、`--模型/--model`。模型接受配置中的别名，默认 `普通`、`高清`。提示词应写在参数前，负面文本可以带空格，延续到下一个 `--` 参数，也可使用引号。未知、重复及缺少值的参数报错。

画师预设、上传画风、本轮明确画风或角色资料画风下，中文正面描述与负面描述使用 NanoGPT 的 `qwen/qwen3.8-27b-uncensored`：先把描述翻译为英文 Danbooru 标签，再按信息完整度补充姿态、视角、取景、背景、天气与场景关系。标签预算按稀疏程度提供建议范围，不设最低数，不为凑数添加细节，也不截断明确内容。人物身份资料和画风预设由上游单独处理；翻译层只保留原文明确写出的人物外观、材质与渲染细节，不主动添加皮肤、头发、眼睛刻画或画风。雨、水滴、湿衣及场景明确表达的贴身或半透明效果仍可保留；用户要求不透明、不要透等约束时必须遵守，不推断裸体、新角色或更换服装。官方作品角色必须输出 Danbooru 规范形式 `name_(series)`。只把本轮去掉角色名后的剩余描述发送到 NanoGPT；角色知识库、身份资料、图片均不发送到翻译服务。即使固定词表已完整处理描述，仍会请求模型。无效格式或残留中文会明确报错。

自动选择「默认细线柔光预设」或「高清默认细线柔光预设」时，NanoGPT 翻译及扩写使用隔离的 `default_20261015_1917` 历史链路：云端目标 30～45 标签、旧链最多保留 60 标签，固定画风项另计最多 4 个（不再截断内容），temperature 0.1、frequency_penalty 0.2、max_tokens 16384、JSON object 响应及原重试行为。默认专属 `default_style_policy.py` 在历史请求末尾追加画风边界：只扩角色、服装、动作、视角与具体场景，不自由添加画师、媒介、光照风格或皮肤／眼睛／头发渲染词；结果再由代码过滤并固定补入原始及回滚甘雨共同的 `anime style, cel shading, soft_lighting, detailed`。显式请求的英文标签及已识别中文细节保留，显式否定的固定项不补回。当前伊蕾娜限定默认配方按用户选定 cel05 将独立正向尾词调整为 `0.5::cel shading::`，并保留 `0.35::photorealistic, 3d render::` 弱负面（仅无明确负面时追加）。静态 44 标签、CFG 与采样参数不变；仅高清 V5 自动默认、规范身份精确为单角色 elaina_(majo_no_tabitabi)、无其他角色资料且用户未明确提供负面时，完整保留原默认负面，仅追加低权重 `0.35::photorealistic, 3d render::`（来自原柔光 umbrella 完整 payload 的同 seed 实图 gentle_no_realism），以轻压写实而保留柔光。其他默认角色（包括甘雨、托帕）、多角色、普通 4.5、手动模式、命名预设和用户明确负面不采用此调整。此链路不接收现代 style_context/content_context，也不继承现代扩写预算、材质或 ontology 规则。历史源码快照不修改：来源于本地 Git index，blob ID 与 SHA-256 记录于 `artifacts/default_20261015_1917_source/manifest.json`；本仓库没有可用于证明 19:17 时刻代码的历史提交。手动关闭自动规划、显式画风、画师和图片参考沿用各自当前链路。

用户最终选定伊蕾娜的 `cel05_accepted` 配方：同样限定于 V5 自动默认、官方规范单伊蕾娜且无知识库角色，翻译后独立的 `cel shading` 尾词改为 `0.5::cel shading::`，静态柔光块不变。此正向调权独立于 `--负面` 覆盖；明确负面仍完整保留且不补弱反写实组。其他默认角色、其他画风、普通 4.5 和手动模式不采用该调权。

[danbooru-tag-index](https://github.com/Jannchie/danbooru-tag-index) 是标签数据集，不是模型。本地编译后的 `index.json` 只含非弃用的普通标签（category=0），用于检索少量中文名称候选及精确英文标签规范化。候选只在原文中做字面匹配，由翻译模型判断是否符合词义，不直接往提示词添加标签；不会据此识别画师或角色。空格与下划线形式可以规范为同一官方标签，未知词原样保留，不进行模糊语义替换。数据文件无需放入插件代码仓库。

比例预设为方图 1024×1024、竖图 832×1216、横图 1216×832：均为 64 的倍数，限制在约一百万像素范围。只允许这三个预设，不接受任意宽高。步数 1～50、种子 0～4294967295、每次 1～2 张、提示词和负面词各最多 3000 字符。省略种子时使用安全随机数。

## 配置与权限

`api.base_url` 仅接受 `https://image.novelai.net`，拒绝其他域名、HTTP、私网地址及重定向。客户端一律忽略环境变量里的 HTTP 代理，避免隐式代理读取令牌；确需通过代理访问时，用 `api.proxy` 显式填写（如 `http://127.0.0.1:7897`），留空即直连。该代理能看到 Authorization 头，因此只填自己信任的地址。`api.model`、`api.hd_model`、`api.model_aliases` 集中管理模型路由，当前只支持上述两个经过官方模型文档确认的 Full 模型。

默认引导强度 5、采样器 `k_euler_ancestral`、步数 28，可配置。每用户冷却 25 秒，默认并发 1；忙碌或同用户重复请求立即反馈，不积压付费请求。`permission.mode` 为 `all` 或 `whitelist`，后者允许列在 `user_ids` 中的用户或 `group_ids` 中的群。NovelAI API 超时支持 5～180 秒，翻译超时最多 60 秒；命令运行最长 300 秒以容纳两段请求。

`/打开画图` 与 `/关闭画图` 只允许管理员 QQ `10000` 使用，状态保存在插件数据目录 `drawing.disabled`。NanoGPT 模式下开关只控制绘图与翻译请求，不启动、预加载或卸载 Ollama。已有绘图请求处理期间要求等请求完成再关闭。

`[translation]` 默认 `backend="nanogpt"`，固定官方 API `https://nano-gpt.com/api/v1` 与模型 `qwen/qwen3.8-27b-uncensored`。密钥从 `NANOGPT_API_KEY` 进程环境或当前 Windows 用户保存的环境变量读取，不写入配置。`proxy` 是可选显式代理，留空直连；请求禁用环境代理与重定向。远程失败明确报错，不自动切换到本地。现代翻译链及至多一次格式修正共用最多 60 秒总预算，连接最多 10 秒；短输入输出上限 1024 token，长输入 2048 token，响应上限 64 KiB。超时不自动重试。日志记录响应阶段与耗时，上游正文、提示词与凭证不写入日志。

`tag_index_enabled=true` 时，`tag_index_path` 留空使用 `MaiBot/data/plugins/local.novelai-image/tag_index/index.json`（普通视觉标签）；同目录下的 `character_index.json`（角色与版权）存在时会一并加载，把中文角色名确定性换成规范 `name_(series)` 标签并把候选交给模型。

**索引是全量 Danbooru**，由两个脚本生成，都不在运行时联网：

- `scripts/crawl_danbooru_tags.py <目录>` 按分类抓全部标签（实测角色 406 页 / 版权 70 页 / 普通 617 页 / meta 2 页，单页 1000 条，page 硬上限 1000）。分页落成 JSONL，断点续传。
- `scripts/build_full_index.py` 合并成索引：角色+版权 **460,927 行（40.8 MB）**、普通标签 **613,457 行（46.0 MB）**。
- 不用时删掉 `character_index.json` 即可，缺失自动跳过，不影响普通翻译。

**中文名覆盖**：Danbooru 的 API 不返回中文名，所以先用社区维护的中英对照表（`danbooru_zh.sqlite`，`post_count ≥ 10` 的全部标签）打底，缺口再用 `scripts/translate_tag_names.py` 交给翻译模型补（不确定的让它留空，不音译、不编造）。结果是角色与版权里 **123,747 行有中文**，其中 `post_count ≥ 10` 的只剩 29 条没有；剩下的空白全是 `post_count < 10` 的超冷门标签。

每行字段：`name`、`zh`、`post_count`、`cat`（**逐行**保存 3=版权 / 4=角色——靠名字有没有括号分不开，`gojo_satoru` 就没有括号）、`aliases`。

**匹配规则**（都在字符层面确定性完成，不经过模型）：

- 字面包含 + 音译同类补充（「希格莉卡」对「西格莉卡」）。归一化后同形的（「雷姆」对「蕾姆」）只留热度最高的一个，不再互相判成"被包含"而两个都丢。
- 同一中文名挂在多个标签上时（「路西法」既是七大罪也是 Helltaker），**本轮写出的作品名优先**，其次比热度；同名标签会一起进候选，否则无从挑。
- 两字中文词只认热度 ≥300 的标签：「天野草莓在变身」会撞上 200 帖以内的「草莓」「变身」这类通用词。
- 名字带冒号的标签（`re:zero_...`、`sin:_nanatsu_no_taizai`）正常收录，翻译校验也不会因此判格式错误。

角色与版权**分开返回**：角色进 `char_captions` 逐个锚定身份，作品留在 base 提示词里。混在一起时多角色会只锚定到其中一个。索引补不出来的词条（角色缩写「五宿」、事件「新宿大战」）走人工表 [supplement.py](plugins/novelai_image/supplement.py)。

上限：16 MiB→**192 MiB**、100000 行→**800000 行**、候选 24→**30**（同一中文名最多带出 3 个标签）。解析结果按（路径, 分类, mtime, 大小）在模块级共享，全量索引实测加载 2.9 秒、查询 1–3 毫秒、常驻约 275 MB。

如需主动恢复本地翻译，设 `backend="ollama"`。原 `model`、`fallback_model`、`base_url`、`num_gpu`、`repeat_penalty`、`keep_alive` 仅用于此模式，地址固定本机 `http://127.0.0.1:11434`。默认 `keep_alive=0` 按需加载并卸载，配置中的备用模型仅在本地模式使用。

提示词按原文直接送往 NovelAI，不做内容筛选，也不追加固定的 `sfw` 正面词或安全负面词；如需规避某类内容，请通过 `--负面` 自行指定。翻译只做中文到英文标签转换，不改变内容范围。外部模型可能生成任何内容，管理员应通过 `permission.mode` 与群权限控制使用范围。

## API 与安全

实现时核对 [官方 Swagger](https://image.novelai.net/docs/doc.json)、[图像 API](https://image.novelai.net/docs/index.html)、[官方模型页](https://docs.novelai.net/en/image/models/)。请求为官方 `POST /ai/generate-image`，`Accept: application/json`，兼容 HTTP 201，解析 `images[{image,index,seed}]`。V4/V5 使用 `v4_prompt/v4_negative_prompt` 的 `caption.base_caption/char_captions`、`use_coords`、`use_order` 结构。

Swagger 没有给 `params_version` 的模型对应值或必填约束，本版本不发送这个可选字段；未执行真实付费请求，实际 V5 请求仍须在安全配置新令牌且另行授权后验证。未来模型变化应检查 `client.build_payload`、`parser.MODEL_ALIASES/SIZE_PRESETS` 和插件配置校验，不能仅改名字假设兼容。

HTTP 401 提示令牌失效，402 提示订阅或 Anlas，403 提示权限，429 提示限频，400/422 提示参数，5xx 提示服务异常并可显示经过格式检查的 correlation ID。不自动重试付费请求。网络、DNS、TLS、超时、JSON、空响应、图片发送故障均有中文反馈。

图片严格解码 Base64，移除受支持的数据前缀，限制单图 30 MiB、总响应 84 MiB、像素数约 1600 万，校验 PNG/JPEG/WebP 签名并使用 Pillow 验证损坏。令牌优先从进程环境变量读取，Windows 下也可从当前用户的已保存环境变量读取；异常不包含上游响应或请求头，日志只记录安全的异常类型，不记录提示词、令牌或 Base64。

## 离线验证

在 MaiBot 目录运行 `.venv/Scripts/python.exe -m pytest plugins/novelai_image/tests -q`。测试使用 `httpx.MockTransport` 和明显虚构的测试凭证，涵盖解析、参数边界、模型路由、高清权限、HTTP/网络错误、图片校验、冷却、并发名额及图片发送失败。没有调用真实 NovelAI 服务。

NanoGPT 接入未执行真实翻译请求或付费绘图请求；没有新增或运行测试。

插件使用 `create_plugin()` 入口和 SDK 配置模型。加载验证可以实例化、注入配置后调用 `on_load`；真实聊天发送、账户额度与官方实时生成尚未实机验证。插件目录为独立 Git 仓库，`config.toml`、缓存均被忽略。


### 角色知识库与 Precise Reference

默认读取 MaiBot 数据根下 `plugins/github.QlzqQlzq.character-knowledge-plugin/characters.json`，并仅对 QQ 请求读取发送者自己的 `personal_characters/sha256(user_id)前24位/characters.json`。个人同名角色在名称/别名匹配前覆盖共享条目，不读取其他人的私人库。`characters.catalog_path` 可指定共享库路径；缺失的目录跳过；已存在但损坏、不可读或超过大小限制的目录明确报错。

按本轮提示中的名称或别名匹配。命中后走**确定性两步**，知识库内容不进入任何模型：先从外观卡和 visual_reference_note 里按固定词表 `characters.IDENTITY_TERMS` 抽出稳定身份标签（发色、发型发长、瞳色、耳、尾、物种特征），再把角色名与别名从本轮描述里剔除，剩下的中文仍交给原先的所选后端翻译。最后按「身份标签在前、本轮标签在后」的顺序确定性拼装并去重。服装、姿势、道具、画风不在词表内，因此不会被继承；本轮描述已提到的特征（如用户自己写了发色）不会重复追加。最多匹配四个角色；若角色资料里一个身份特征都抽不到，明确报错而不是把中文原样发给 NovelAI。

`include_image_reference = true` 时，仅 4.5 Full 的单角色使用第一张有效本地参考图，限定在该目录的 references 子目录内，源文件不超过 8 MiB / 16777216 像素；缩放并黑色补边为官方要求的尺寸。使用官方 Precise Reference，默认独立参考角色与画风；自动规划角色 strength 0.8、fidelity 0.55（关闭自动时使用配置值），明确指定或排除画风时只保留 character。可在 characters 中调节。默认 `separate_style_reference=true` 会把同一张图分别作为 character 和 style 参考：角色参数 0.8 / 0.55，画风参数 0.85 / 0.7。服装、姿势及身份覆盖只降低角色参数，画风参数保持不变，且不追加默认文字画风。两项参考每张生成图额外 10 Anlas，比原单图参考多 5 Anlas；发送请求前会提示。显式 `--参考模式 角色画风` 保留一项 combined 参考（额外 5 Anlas），或关闭该配置恢复单项自动参考。参数仍是经验初始值，未通过付费出图校准。参考图仍可能影响服装与姿势；服装、姿势或发型要求只把 fidelity 限制为 outfit_fidelity（默认 0.35），保留角色 strength；明确覆盖发色、瞳色或部分物种特征时才使用 override_strength / override_fidelity 上限。参数为可调初始值，尚未通过付费出图校准。多角色为避免身份混合仅使用文字；5 Full 当前官方明确不支持 Precise Reference，仅使用文字并明确提示。没有有效参考图也会提示文字模式。

字段依据：[官方 Precise Reference 文档](https://docs.novelai.net/en/image/precisereference/) 与 [官方 API schema](https://image.novelai.net/docs/doc.json)。本次未执行收费生成验证。


### 自动角色与画风计划

日常直接发送 `/画图 角色名 描述`。程序先匹配当前用户可见的角色别名、选图、抽取稳定身份标签，再剔除角色名，只把剩余中文描述交给所选翻译后端；角色知识库和参考图不进入翻译模型。只写角色名且身份标签齐全也可生成，不必调用翻译。

4.5 Full 单角色默认用同一张知识库参考图的 `character&style`，同时参考角色和画风。正面或负面描述明确提到水彩、写实、像素等画风时自动使用 `character`，由文字决定画风；指定服装、姿势或发型时只降低保真度，保留角色强度；覆盖发色、瞳色或部分物种特征时才同时降低强度和保真度。这是固定词表启发式策略，不保证所有表达都能识别，也不保证生成结果严格满足姿势。5 Full 不发送参考图，多角色仅使用文字身份资料，避免图像混合身份。每张参考图每次生成图片额外消耗 5 Anlas。

`/画图检查 角色名 描述`（同义 `/画图预览`）显示最终正负标签、模型、尺寸、参考模式和参数。它复用正式生成的计划函数，会按需调用所选翻译后端；NanoGPT 翻译按账户用量计费，但不请求 NovelAI 生成、不需要 API Token，也不消耗 Anlas，但 NanoGPT 翻译可能产生费用。检查遵守插件开关、允许名单，并与生成共用并发名额及用户冷却；检查与随后生成之间角色库或配置发生变更时，计划可能改变。

示例：`/画图检查 小雪 坐在窗边，水彩风格`；`/画图 小雪 running in a uniform`。

`[characters]` 配置：`default_reference_type="character&style"`（可选 `character`）、`auto_reference_policy=true`、`reference_strength=0.75`、`reference_fidelity=0.6`、`override_strength=0.35`、`override_fidelity=0.15`。覆盖参数作为上限，不会把管理员已设置更低的值提升。关闭自动策略时按默认参考类型和基础参数使用图像；默认类型为 character&style 且 separate_style_reference=true 时，仍拆为独立角色和画风参考。

参考类型及费用依据：[NovelAI Precise Reference 文档](https://docs.novelai.net/en/image/precisereference/)；请求字段依据：[官方 API 定义](https://image.novelai.net/docs/doc.json)。


### 上传画风参考图

发送 `/画图画风 <提示词>`，随后在五分钟内由同一用户在同一平台、同一聊天单独发送一张图片。支持原有参数，但仅允许 4.5 Full；选择 5 Full 会明确拒绝，避免忽略参考图继续收费生成。同一用户在该聊天再次发送命令会替换等待请求。无图片的普通聊天不会触发绘图，其他用户或聊天的图片不会被用于该请求。等待过期后需重新发送命令。

上传图默认使用独立的 `style` 参考，知识库角色图使用 `character` 参考，两张图通过相同长度的 `director_reference_*` 数组发送，分别控制参数。不将上传图放入图生图底图，也不提取其人物、服装、动作、构图或背景作为文字标签。用户明确写出“参考图人物/角色”时上传图改为 `character&style`；如果同时存在知识库角色图会报错，避免混合身份。Precise Reference 不能可靠复制场景、构图或背景，因此此类请求会提示用户用文字具体描述。风格模式仍可能产生内容影响，不能保证严格隔离或与参考图完全一致。

`[style]` 默认启用，自动规划 strength 0.85、fidelity 0.7（关闭自动时使用配置值）；允许管理员调整。上传图最多 8 MiB、16777216 像素，只接受 SDK 提供的 PNG/JPEG/WebP Base64 数据，拒绝 URL 与文件路径，验证格式后缩放并黑色补边到官方参考画布。图片仅在内存中用于本次请求，不保存或跨请求复用；它会上传到 NovelAI，每张参考图每张生成图片额外 5 Anlas。两张参考图、两张生成图片即额外 20 Anlas。最多同时等待 100 个请求，生成沿用原有并发和冷却限制；无收费请求自动重试。

命令登记、图片接收和实际生成都会检查开关与允许名单；图片元数据必须具有完整平台、聊天、发送者和有效时间。关闭绘图清除等待请求，卸载插件清理后台任务。图片接收使用 `chat.receive.before_process` Hook，未新增读取历史消息能力，manifest 沿用 `send.text`、`send.image`、`config.get`。后台任务仍走同一个 `draw` 和 `_plan_request` 路径。

参考依据为 [官方 Precise Reference 文档](https://docs.novelai.net/en/image/precisereference/) 的三种模式及组合规则，参数数组依据 [官方 API schema](https://image.novelai.net/docs/doc.json)。API schema 的旧文字说明仍只列 character / character&style，模式字段本身是普通字符串；本实现依据更新的用户文档使用 style，尚未执行真实付费 API 验证。高强度和保真度参数并不保证角色或画风完全一致；官方建议使用清晰的人物全身及脸部特写参考。

角色外观标签保留复合颜色，例如“银白偏淡紫”表示 silver hair + subtle lavender hair tint，避免拼出两个互相竞争的主发色；白色猫耳、蓬松猫尾等稳定特征保留为独立身份标签。


## 自动参数与高清文字画风（1.2）

`/高清画图` 和 `--模型 高清` 对所有符合当前绘图允许名单的用户开放。`/打开画图`、`/关闭画图` 仍仅管理员可用，冷却、最大张数和并发限制保持生效。

参数优先级为本次手动指定、自动规划、配置默认值。自动引导按固定规则选择：标准 5.5、柔光细线 5.3、多人或复杂动作 6.0；宽幅/全景优先横图，全身/portrait 优先竖图，其余沿用配置比例。采样器和步数沿用配置，显式引导、比例、步数、种子和张数保持原值，自动规划不会增加张数或步数。这是确定性启发式规划，不会根据生成结果反馈自动优化。4.5 的自动角色和上传画风参考强度/保真度为 0.85/0.65；服装或姿势只降低角色参考保真度，身份覆盖减弱角色参考，本次手动强度/保真度可覆盖自动结果。`--自动参数 off` 使用配置默认值。

| 中文选项 | 英文选项 | 有效值 |
| --- | --- | --- |
| --引导 | --scale | 0～10 |
| --采样器 | --sampler | k_euler、k_euler_ancestral、k_dpmpp_2s_ancestral、k_dpmpp_2m、k_dpmpp_sde、ddim_v3 |
| --角色强度 | --character-strength | 0～1，4.5 图片参考 |
| --角色保真度 | --character-fidelity | 0～1，4.5 图片参考 |
| --画风强度 | --style-strength | 0～1，上传图片或普通单角色独立画风参考 |
| --画风保真度 | --style-fidelity | 0～1，上传图片或普通单角色独立画风参考 |
| --参考模式 | --reference-mode | 自动/auto、角色/character、角色画风/character&style、关闭/off |
| --自动参数 | --auto | on/off、开启/关闭 |
| --画风权重 | --style-weight | 0～2，自动继承的文字画风权重，0 不追加画风标签 |

高清仍使用 5 Full。官方 V5 当前不支持 Precise Reference 或 Vibe Transfer，因此此插件不会向 V5 发送或编码图片参考；V5 指定图片强度/保真度或角色图片参考模式会明确报错。`/高清画图画风` 暂不提供，`/画图画风 --模型 高清` 会拒绝；不会自动降级模型。

自动规划且未指定其他画风时应用默认画风。普通 4.5 与高清 5 分别读取 `[style].default_tags`、`hd_default_tags`。高清默认完整恢复原图的 44 项柔光配方，默认排除词仅 `dramatic lighting`、`harsh shadows`，`hd_default_only_negative_tags=[]`，不删除原请求的赛璐璐等排除词，保留历史场景过曝门控，不追加新的反写实排除词。风格标签放在提示词前段；本轮手动负面、画风权重、种子、比例、步数与采样参数仍优先。两种模型的默认标签可分别编辑，置空列表可关闭；匹配角色时，知识库中可识别的画风标签优先覆盖默认标签。

### 特色画风预设

提示词里点名某档画风时**整档替换**默认预设，而不是把默认预设丢掉只留模型自由发挥。现在共 45 档：

- 媒介类 7 档：厚涂 / 赛璐璐 / 水彩 / 素描 / 像素 / 写实 / **锐利线稿**
- 画师类 38 档：ciloranko、rella、wlop、mika pikazo、hiten、fuzichoco、wanke、ke-ta、ask、yoneyama mai、krenz、kantoku、shimada fumikane、as109、lack、misaki kurehito、modare、atdan、sakimichan、kuroboshi kouhaku、torino aqua、so-bin、iren lovel、aroa (aroamoyasi)、mocha (cotton)、hitsukuya、cola (gotouryouta)、tamaki、momoko (momopoco)、konya karasue、achiki、tokkyu、rurudo、mifuru、miv4t、fajyobore、emyo、loundraw

中英文关键词都能命中（「厚涂」「赛璐璐上色」「pixel art」「锐利线稿」「ciloranko」）。**命中多个时取关键词最长的那个**，不是表内靠前的——「锐利线稿」同时含「锐利线稿」和素描的「线稿」，按顺序会被素描截走。

**画师名支持模糊匹配**：分隔符互换（`misaki kurehito` / `misaki_kurehito`）、连写（`misakikurehito`）、去掉 `(xxx)` 后缀（`mocha` 对 `mocha (cotton)`）都认，中文与日文写法（`画师momoko`、`ももこ`、`紺屋鴉江`）也认。但**边界没有放宽**——英文关键词仍按整词匹配，`hiten` 不会落在 `whiten` 里、`rella` 不会落在 `corella` 里；中文按键位包含匹配。

每档自带标签、要从基础负面里摘除的冲突项（赛璐璐和锐利线稿要摘掉 `cel shading`、`flat colors`、`crisp lineart` 等十项，否则正面要平涂锐线、负面在挡，模型会解成高反差杂色）、以及一张内置风格参考图。

画师档使用逐档数值权重和专属画法词；as109、tamaki 保留原档不单独给画师名加权的约定。各权重块独立输出，禁止把已有 `数字::标签::` 再包进另一层；手动 `--画风权重` 同时缩放独立画师块和画法块。

`/查看画风`（别名 `/画风列表`）用合并转发列出全部画风，每条先发画风名，再按模型分别给出参考图；转发不可用时逐条发送。缩略图会先压到 400px JPEG，否则每档两个模型、近百张一起塞进一次 IPC 会超载荷上限。

每档备有两套参考图，文件名一一对应：`assets/styles/<name>.png` 是 4.5 那套（生成时真正使用的），`assets/styles/v5/<name>.png` 是 5 那套（只供并排比对，因为 5 Full 不支持参考图）。`/画风预览`（别名 `/画风对照表`）把两套各拼成一张对照表，用一个合并转发发出，两张分别标明模型。对照表每格尺寸由 `[style].sheet_cell_width`（默认 480，旧版 300）、`sheet_columns`（默认 6）、`sheet_quality`（默认 88）控制；调大更清楚，但单张 base64 会同比上涨。

`/查看画师 <画师名>` 单独看一位画师的两套参考图，发的是**原图**（对照表是缩过的），同样走合并转发。画师名支持模糊写法，写一半也认：`torino`、`mika`、`sakimi`、`kurehito` 都能命中；少于 3 个字不给匹配，避免一个字命中一大片。

没有点名任何一档、也没有上传画风参考图时走默认柔光档。点名了表内没有的画风（例如「插画风」）时交给翻译模型按本轮措辞处理。`[style].preset_enabled` 可整档关闭预设，`preset_reference_enabled` 可只关参考图。

内置参考图只在 4.5 Full 上生效（5 Full 官方不支持参考图），也是 `style` 类型、每张每图额外 5 Anlas；角色参考已占满两张槽位、或模型是 5 Full 时自动退化为纯标签，并在提示信息里说明原因。

38 位画师均按不可变的原始参考图逐张检查。37 档使用各自的线条、上色、纹理标签及静态 `GenerationTuning`；v5 模型已有偏差的预览图不再倒过来定义目标。torino aqua 的标签和专属参数保留。请求阶段不读取 PNG 元数据。手动负面、引导、步数、采样器和文字权重优先于自动参数；`--自动参数 off` 关闭自动参数调整。不会从样图继承人物、衣服、道具或构图。普通模型的内置参考图支持 `--画风强度`、`--画风保真度`；V5 仍只支持文字画风，效果接近程度需要实际出图判断。

赛璐璐档正面明确要求 `cel shading`、`flat colors` 和 `two-tone shading`，并去除冲突负面；素描档使用独立的单色铅笔权重块，v5 的错误彩色样图已重绘。

### 按群号折叠为合并转发

`[generation].collapse_groups` 列出的群号，里面的出图一律折成一条合并转发，不做内容判定；其余群和私聊逐张直发。默认空列表，即哪里都不折叠。

转发节点的图片先统一转成有体积限制的 JPEG，再以 base64 上传。机器人账号从 `bot.qq_account` / `bot.nickname` 取。明确上传失败时，仅用更小的 JPEG 重试一次；超时等结果不明的情况不重试，避免重复投递。名单内群始终不降级直发，失败原图及索引保存在插件数据目录 `pending_deliveries/<任务号>/`，路径记入管理员日志，聊天返回任务号。当前本机名单只有 `100000000`，其余群和私聊出图直接发送。

画师标签可通过 `[style].artist_tags` 和 `hd_artist_tags` 按模型分别配置，默认留空。一个可试的低权重候选为 `0.35::artist:ainiwaffles::`；NovelAI 官方风格文档曾用该标签展示插画例子，但它不是“小虚”原图作者的确认信息，也不能保证匹配两代模型。建议先只在一项配置里试，并用同一提示词、种子和其余参数与默认风格对照，再决定是否保留。官方建议把整体画风标签放在提示词靠前位置，本插件的自动风格块会放在角色与场景标签前。

`/画图检查 小絮站在海边 --模型 高清 --引导 5.5 --画风权重 1.3` 可查看最终模型、提示词、实际引导、采样器、文字画风来源及手动覆盖。检查只构造计划，不调用 NovelAI 生成或图片编码接口，不消耗 Anlas；中文翻译可能产生 NanoGPT token 费用。图片参考仅存于当前请求内存，不新增私人图片持久化。

官方依据：[V5 功能说明](https://novelai.net/v5)、[提示词加强与减弱](https://docs.novelai.net/en/image/strengthening-weakening/)、[步数与引导](https://docs.novelai.net/en/image/stepsguidance/)。

自动参考参数可通过 `[characters].auto_reference_strength / auto_reference_fidelity` 与 `[style].auto_reference_strength / auto_reference_fidelity` 配置；关闭自动参数时使用各节原有 `reference_strength / reference_fidelity`。手动命令参数始终优先。明确指定新画风时仅保留知识库角色参考；上传画风图时不再添加知识库画风参考。多角色和高清仍沿用文字资料策略。


中文显式内容优先：打伞/撑伞及常见衣装经词表转换后参与内容保留判断，避免把用户要求的伞、衬衫、裙装误当作模型新增内容删除。否定命中不会强制加入正向标签；「不要伞」「不打伞」「不穿裙子」会阻止对应正向内容。未要求的衣装和道具仍受过滤，明确指定的英文画师标签在独立画风上下文中保留。
