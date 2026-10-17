"""命令与 NovelAI 实际结果同板块排版的工作流图文帮助。"""
from base64 import b64encode
from io import BytesIO
from pathlib import Path
import re

from PIL import Image, ImageDraw, ImageFont, ImageOps


ASSETS = Path(__file__).parent / "assets" / "help" / "workflow"

# 普通帮助与工作流示例共用同一套卡片：先格式（大字），再示例（小字）。
BASIC_GUIDE = [
    ("开始出图", "中文描述直接用；不带参数时由插件自动规划比例、引导与参考。", [
        ("生成一张图", ["/画图 中文描述 [参数]"], ["/画图 伊蕾娜在城堡前回头，半身"],
         ["/画图 默认 4.5 Full；每张图都有任务号，后续修改、局部重绘都靠它。"], []),
        ("其余命令默认 5 Full", ["/高清画图 中文描述", "/画小图 中文描述", "任意命令 --模型 4.5"],
         ["/高清画图 伊蕾娜在雨中撑伞，全身 --种子 121823221", "/画小图 猫在窗台上打盹", "/画图对比 赛璐璐-水彩 伊蕾娜读书 --模型 4.5"],
         ["除 /画图 与 /画图检查 外，其余命令默认 5 Full；需要 4.5 时写 --模型 4.5（或 v4.5）。", "高清走正常画布，按尺寸扣 Anlas；小图档走订阅无限额度，不扣 Anlas。"], []),
        ("先检查再画", ["/画图检查 中文描述", "/高清画图检查 中文描述"], ["/画图检查 伊蕾娜在花园里读书", "/高清画图预览 伊蕾娜在雨中撑伞"],
         ["“预览”与“检查”等价：只展示最终请求、不生成图片；云端翻译仍可能计费。"], []),
    ]),
    ("画风", "不点名时使用默认柔光；画风只由文字配方决定，不继承人物或场景。", [
        ("点名画风", ["/画图 <画风名> 中文描述"], ["/画图 水彩 伊蕾娜在窗边喝茶"],
         ["画风名写在提示词里即可整档换风格，共 45 档：厚涂、赛璐璐、水彩、素描、像素、写实、锐利线稿，外加 38 款画师档。", "写一半也认：torino、mika、sakimi 都行；中日文画师名同样支持。"], []),
        ("参考一张图的画风", ["/画图画风 中文描述"], ["/画图画风 伊蕾娜在花园里读书"],
         ["发出命令后 5 分钟内在原聊天发一张图作为画风参考。"], []),
        ("查看画风与画师", ["/查看画风", "/查看画师 画师名", "/画风预览"], ["/查看画师 torino", "/画风预览"],
         ["列表走合并转发，每条先发名字再按模型给参考图；对照表每格尺寸可用 [style] sheet_cell_width / sheet_columns / sheet_quality 调整。"], []),
    ]),
    ("参数", "提示词写在参数前；负面词保留空格到下一个参数，也可用引号。", [
        ("常用参数", ["--比例 方图|竖图|横图  --种子 数字", "--数量 1～2  --负面 文本  --模型 普通|高清"],
         ["/画图 城堡 --比例 横图 --种子 123456", "/高清画图 少女 --数量 2 --负面 模糊, 低质量"], [], []),
        ("采样参数", ["--步数 1～50  --引导 0～10  --采样器 官方名称", "--自动参数 on|off"],
         ["/画图 少女 --步数 28 --引导 5 --采样器 k_euler_ancestral"],
         ["默认自动规划；手动参数优先，免费检查能看到实际计划。"], []),
        ("参考与权重（图片参考仅 4.5）", ["--角色强度 / --角色保真度 0～1", "--画风强度 / --画风保真度 0～1",
                                  "--参考模式 自动|角色|角色画风|关闭  --画风权重 0～2"],
         ["/画图 少女 --角色强度 0.8 --画风强度 0.6"],
         ["4.5 单角色默认同图独立参考角色与画风，合计每图额外 10 Anlas；--参考模式 角色画风 使用联合参考，每图额外 5 Anlas。5 只走文字资料。"], []),
    ]),
    ("写法与开关", "", [
        ("原样标签", ["/画图 原样 我的标签：<标签>", "/画图 不要看这个提示词：<标签>"],
         ["/画图 原样 我的标签：1girl, umbrella, rain"],
         ["跳过翻译、词库、知识库和自动画风；支持 | 分栏和手动参数。"], []),
        ("多人自动拆栏", ["/画图 全局：场景与互动-角色1：外貌与位置-角色2：外貌与位置"],
         ["/高清画图 全局：两人站在花园里牵手-角色1：左侧伊蕾娜-角色2：右侧甘雨"],
         ["最多 6 人；不写角色栏时也会按人数自动拆分。"], []),
        ("管理员开关", ["/打开画图", "/关闭画图"], ["/关闭画图"], ["仅管理员可用；Ollama 模式会同时启停翻译模型。"], []),
    ]),
    ("计费与后续", "生成按账户规则扣 Anlas；中文描述会自动翻译为 Danbooru 标签。", [
        ("计费与英文别名", [], [],
         ["生成按账户规则扣 Anlas；小图档走订阅无限额度。", "中文描述自动翻译；NanoGPT 翻译产生其账户用量。4.5 参考图每图额外计费，5 不支持参考图。",
          "英文别名与中文参数等价：--ratio --seed --steps --count --negative --model --scale --sampler --auto --character-strength --character-fidelity --style-strength --style-fidelity --reference-mode --style-weight。"], []),
        ("继续操作", ["/画图修改 任务号 修改内容", "/画图局部 任务号 修改要求", "/画图记录 [页码]"],
         ["/画图修改 回复 加一条红围巾", "/画图记录"], ["从下一页开始是完整工作流示例。"], []),
    ]),
]
# 每张卡片先给命令格式（大字），再给一条具体示例（小字），最后是说明和对应图片。
GUIDE = [
    ("从伊蕾娜开始", "基础示例统一使用当前默认柔光；先生成，再回复图片或任务号消息继续操作。", [
        ("生成与原参数复刻", ["/高清画图 中文描述 [--种子 数字]", "/画图复刻 任务号"],
         ["/高清画图 伊蕾娜在雨中撑伞，全身 --种子 121823221", "/画图复刻 回复"],
         ["复刻复用原任务的最终参数和种子，不重新翻译。每张新结果都有自己的任务号。", "“回复”可替换为 /画图记录 中属于你在当前聊天的32位任务号。"], [("base", "原图 · 伊蕾娜雨中撑伞"), ("replay", "复刻 · 相同请求与种子")]),
    ]),
    ("修改与替换", "中文负责描述需求，翻译器转为标签；原任务画风继续沿用。", [
        ("在原图方案中增加围巾", ["/画图修改 任务号 中文修改要求"], ["/画图修改 回复 给伊蕾娜加一条红围巾"],
         ["适度补充本次内容，不扩写画风或脸部渲染细节。同种子重生成，构图仍可能变化。"], [("base", "修改前"), ("modified", "修改后 · 红围巾")]),
        ("精确替换一个标签", ["/画图修改 任务号 旧中文-新中文"], ["/画图修改 回复 红围巾-蓝围巾"],
         ["旧项-新项：两侧中文分别作精确翻译，不扩写。旧标签必须命中原任务；不命中会提示。", "英文也可写 red scarf-blue scarf；t-shirt 等单词中的连字符不会被盲目拆开。"], []),
    ]),
    ("对比两种画风", "同一份请求、角色栏和种子，分别套用两套画风配方。", [
        ("赛璐璐与水彩", ["/画图对比 画风A-画风B 中文场景 [--模型 4.5]"], ["/画图对比 赛璐璐-水彩 伊蕾娜坐在花园里读书 --种子 20261018"],
         ["画风A-画风B 后接空格，再写中文场景。两图均为 NovelAI 5 Full 实际生成。", "比较的是画风配方；不同配方仍可能影响构图和姿态。"], [("cel", "画风 A · 赛璐璐"), ("watercolor", "画风 B · 水彩")]),
    ]),
    ("收藏、版本与套用", "先回复满意的画风结果收藏，再用同一个收藏名画新场景。", [
        ("把水彩配方用到窗边喝茶", ["/画图收藏 收藏名 任务号", "/画图套用 收藏名 中文场景 [--模型 4.5]"],
         ["/画图收藏 伊蕾娜水彩 回复", "/画图套用 伊蕾娜水彩 伊蕾娜在窗边喝茶 --种子 20261018"],
         ["只收藏文字画风，不收藏人物或场景。收藏名含空格时，套用命令请给名字加双引号。"], [("watercolor", "收藏来源 · 水彩"), ("favorite", "套用结果 · 窗边喝茶")]),
        ("查看与恢复版本", ["/画图收藏 列表", "/画图版本 收藏名", "/画图回滚 收藏名 版本号"],
         ["/画图收藏 列表", "/画图版本 伊蕾娜水彩", "/画图回滚 伊蕾娜水彩 1"],
         ["同名收藏自动增加版本；回滚也创建新版本。收藏只对本人当前聊天有效。"], []),
    ]),
    ("同角色场景系列", "场景1-场景2-场景3，最多三幕，场景允许直接写中文。", [
        ("伊蕾娜：花园到海边", ["/画图系列 任务号 场景1-场景2[-场景3]"], ["/画图系列 回复 花园长椅上读书-海边看夕阳"],
         ["固定角色身份、原种子与已记录画风，再翻译和适度扩充各幕场景。", "源图只有一个角色时会自动识别身份，无需预先分栏；多人源图需独立角色栏。", "此工作流示例固定使用 NovelAI 5 Full，系列沿用文字身份；身份一致性受模型能力影响。"], [("series_garden", "第一幕 · 花园读书"), ("series_seaside", "第二幕 · 海边夕阳")]),
    ]),
    ("中文局部重绘", "描述位置或元素即可；系统内部自动定位，不需要上传遮罩。", [
        ("只把脖子上的红围巾改成蓝色", ["/画图局部 任务号 中文修改要求"], ["/画图局部 回复 把伊蕾娜脖子上的红围巾改成蓝色"],
         ["先通过视觉模型识别围巾，再把目标当作选区交给 NovelAI 重绘。", "明确的“画面右上角”等方位也可直接识别。定位不可靠会停止，请把目标说具体。", "保留原任务画风和其他标签；生成后把选区外的原像素合回，限制无关改变。"], [("modified", "修改前 · 红围巾"), ("local_blue", "修改后 · 蓝围巾")]),
    ]),
    ("导演工具：表情与背景", "回复任务图，输入工具名；需要描述时直接写中文。", [
        ("改变表情", ["/画图导演 任务号 表情 中文表情"], ["/画图导演 回复 表情 开心微笑"], ["中文表情先转为视觉标签；结果作为新任务保存。"], [("base", "原图"), ("director_emotion", "表情结果")]),
        ("移除背景", ["/画图导演 任务号 去背景"], ["/画图导演 回复 去背景"], ["透明区域在帮助图中用棋盘格显示。导演内部会缩图处理并恢复原画布。"], [("base", "原图"), ("director_removebg", "去背景结果")]),
    ]),
    ("导演工具：线稿与上色", "线稿和上色各是一次操作；上色时回复刚生成的线稿结果。", [
        ("从伊蕾娜图提取线稿", ["/画图导演 任务号 线稿"], ["/画图导演 回复 线稿"], ["线稿只保留轮廓，上色时可重新指定发色、瞳色和服装配色。"], [("director_witch", "原图"), ("director_lineart", "NovelAI 线稿")]),
        ("给线稿重新上色", ["/画图导演 任务号 上色 中文颜色"], ["/画图导演 回复 上色 浅灰发、蓝色眼睛、黑色魔女袍"], ["此处“回复”指上一步线稿任务。导演任务的复刻仍回到来源生成参数。"], [("director_lineart", "待上色线稿"), ("director_colorize", "NovelAI 上色结果")]),
    ]),
    ("记录、反馈与任务管理", "每张图片有独立任务号；记录和收藏均按平台、聊天与本人隔离。", [
        ("找到自己的伊蕾娜图片", ["/画图记录 [页码]"], ["/画图记录", "/画图记录 2"], ["合并转发每页5条，任务号和对应图片放在同一个节点；从节点复制真实任务号即可继续操作。"], [("base", "每条记录包含对应的实际图片")]),
        ("评价收藏画风", ["/画图反馈 任务号 喜欢/不喜欢", "/画图推荐"], ["/画图反馈 回复 喜欢", "/画图反馈 回复 不喜欢", "/画图推荐"], ["先收藏画风，再评价使用它的任务；推荐只依据本人的明确反馈，不会自动改默认画风。"], []),
        ("查看、取消与清除", ["/画图状态", "/画图取消", "/画图清除"], [], ["取消停止本地等待和后续步骤，已提交请求可能仍计费。清除会删除当前聊天本人的任务、图片、收藏与反馈。", "参数默认保留24小时；图片默认仅内存保留。重启或回收后图片可能不可用，管理员可开启 workflow.persist_images。"], []),
    ]),
    ("多人显式分栏", "伊蕾娜与甘雨：全局描述互动，每个角色单独写外貌、位置和动作。", [
        ("全局 + 左侧伊蕾娜 + 右侧甘雨", ["/高清画图 全局：场景与互动-角色1：外貌与位置-角色2：外貌与位置 [--比例 横图]"],
         ["/高清画图 全局：两人站在花园里牵手，全身-角色1：左侧伊蕾娜，浅灰长发，蓝色眼睛，黑色魔女袍-角色2：右侧甘雨，蓝发紫瞳，头上有角 --比例 横图 --种子 20261018"],
         ["“全局：”写人数、场景和互动；“-角色1：”“-角色2：”明确分栏，最多6人。", "中文分栏会翻译为 NovelAI 的独立角色提示词；位置和各自属性留在相应人物栏。", "所有工作流生成示例均使用 NovelAI 5 Full。英文单词内的 - 不作分栏；只有命名角色标记会拆栏。"], [("multichar", "NovelAI 实出图 · 左：伊蕾娜／右：甘雨")]),
    ]),
]


def workflow_help_text() -> str:
    lines = ["个人绘图工作流 · 伊蕾娜示例", "任务号前后用空格，替换/场景序列用 -。回复图片或任务号消息时可使用“回复”。"]
    for title, intro, cards in GUIDE:
        lines.extend(["", title, intro])
        for heading, syntax, example, notes, _ in cards:
            lines.extend([heading, *syntax])
            if example:
                lines.extend(["示例：" + example[0], *example[1:]])
            lines.extend(notes)
    lines.append("图中结果为 NovelAI 实际生成。固定种子示例仅用于展示操作，实际出图可能不同；生成及导演按账户规则计费。")
    return "\n".join(lines)


def _font(size: int):
    for path in (Path("C:/Windows/Fonts/msyh.ttc"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")):
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _wrap(text: str, font, width: int, draw) -> list[str]:
    # 按字符折行会把 --种子 20261018 拆成 "2026101 / 8"、workflow.persist_images 拆成
    # "workflow.p / ersist_images"；英文单词、数字和路径当作整体，只在空格或汉字处断开。
    tokens = re.findall(r"[A-Za-z0-9_.:/+=-]+| +|.", text)
    lines, current = [], ""
    for token in tokens:
        if current and draw.textlength(current + token, font=font) > width:
            lines.append(current.rstrip())
            current = token.lstrip()
        else:
            current += token
    if current:
        lines.append(current)
    return lines


def _render_pages(guide, start: int = 1, total: int | None = None) -> list[str]:
    """整张卡片先测量后绘制，不把命令、说明与对应的前后图拆页。"""
    total = total or len(guide)
    width, padding, gap = 1080, 38, 22
    title_font, heading_font, body_font, small_font = _font(38), _font(29), _font(25), _font(21)
    syntax_font = _font(28)
    ruler = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    pages = []
    for offset, (title, intro, cards) in enumerate(guide):
        page_number = start + offset
        layouts = []
        for heading, syntax, example, notes, images in cards:
            rows = [(heading, heading_font, "#f5f0ff", 44, 0)]
            for command in syntax:
                rows.extend((line, syntax_font, "#9ce2ff", 40, 0) for line in _wrap(command, syntax_font, width - 2 * padding - 44, ruler))
            rows.append(("", small_font, "#bcc7dc", 9, 0))
            if example:
                rows.append(("示例", small_font, "#7f8dab", 27, 0))
                for command in example:
                    rows.extend((line, small_font, "#a9bdd9", 31, 16) for line in _wrap(command, small_font, width - 2 * padding - 60, ruler))
                rows.append(("", small_font, "#bcc7dc", 9, 0))
            for note in notes:
                rows.extend((line, small_font, "#bdc8dd", 31, 0) for line in _wrap(note, small_font, width - 2 * padding - 44, ruler))
            picture_height = (420 if len(cards) > 1 else 640) if images else 0
            if len(images) == 1 and images[0][0] == "multichar":
                picture_height = 595
            height = 38 + sum(row[3] for row in rows) + (picture_height + 48 if images else 0)
            layouts.append((rows, images, picture_height, height))
        intro_lines = _wrap(intro, body_font, width - 2 * padding, ruler)
        top = 100 + len(intro_lines) * 36 + 25
        height = top + sum(layout[3] for layout in layouts) + gap * (len(layouts) - 1) + 85
        sheet = Image.new("RGB", (width, height), "#101623")
        draw = ImageDraw.Draw(sheet)
        draw.text((padding, 24), f"{page_number:02d}  {title}", font=title_font, fill="#e4ceff")
        draw.text((width - 142, 35), f"{page_number}/{total}", font=small_font, fill="#8e9dbb")
        y = 95
        for line in intro_lines:
            draw.text((padding, y), line, font=body_font, fill="#d4def0")
            y += 36
        y = top
        for rows, pictures, picture_height, card_height in layouts:
            draw.rounded_rectangle((padding, y, width - padding, y + card_height), radius=18, fill="#1c2639", outline="#364660", width=2)
            row_y = y + 18
            for line, font, color, advance, indent in rows:
                if line:
                    draw.text((padding + 22 + indent, row_y), line, font=font, fill=color)
                row_y += advance
            if pictures:
                inner_width = width - 2 * padding - 44
                cell_width = (inner_width - 18 * (len(pictures) - 1)) // len(pictures)
                for index, (name, label) in enumerate(pictures):
                    x = padding + 22 + index * (cell_width + 18)
                    draw.text((x, row_y + 4), label, font=small_font, fill="#ffdc9a")
                    with Image.open(ASSETS / f"{name}.png") as source:
                        picture = source.convert("RGBA")
                    # 只有真的带透明区域才铺棋盘格；普通人像的 alpha 常是 254（取整误差），
                    # 按 <255 判定会让人像两侧凭空多出格子边。
                    alpha = picture.getchannel("A")
                    translucent = alpha.getextrema()[0] < 128
                    slot = Image.new("RGB", (cell_width, picture_height), "#121b2b")
                    if translucent:
                        shade = ImageDraw.Draw(slot)
                        for yy in range(0, picture_height, 24):
                            for xx in range(0, cell_width, 24):
                                if (xx // 24 + yy // 24) % 2:
                                    shade.rectangle((xx, yy, xx + 23, yy + 23), fill="#2a3445")
                    # 原图等比容纳，避免裁掉人物/道具。
                    fitted = ImageOps.contain(picture, (cell_width, picture_height), Image.Resampling.LANCZOS)
                    slot.paste(fitted, ((cell_width - fitted.width) // 2, (picture_height - fitted.height) // 2), fitted)
                    sheet.paste(slot, (x, row_y + 38))
            y += card_height + gap
        footer = ("NovelAI 5 Full 实际生成示例 · 每个任务使用命令所示种子 · 费用以实际账户为准"
                  if any(card[4] for card in cards) else "NovelAI 绘图 · 帮助")
        draw.text((padding, height - 54), footer, font=small_font, fill="#8e9dbb")
        buffer = BytesIO()
        sheet.save(buffer, "JPEG", quality=90, optimize=True)
        pages.append(b64encode(buffer.getvalue()).decode("ascii"))
    return pages


def workflow_help_images(start: int = 1, total: int | None = None) -> list[str]:
    return _render_pages(GUIDE, start, total)


def basic_help_images(start: int = 1, total: int | None = None) -> list[str]:
    return _render_pages(BASIC_GUIDE, start, total)


def help_images() -> list[str]:
    """普通帮助在前、工作流示例在后，编号连续，合并成一套发出去。"""
    total = len(BASIC_GUIDE) + len(GUIDE)
    return _render_pages(BASIC_GUIDE, 1, total) + _render_pages(GUIDE, len(BASIC_GUIDE) + 1, total)
