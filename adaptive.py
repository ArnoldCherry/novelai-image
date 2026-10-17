"""确定性画风规划；不向翻译模型发送知识库，也不调用图片编码接口。"""
from typing import List, Sequence
import re

from .characters import Character

# 只在"用户明确在讲画风"时命中。刻意不含裸 3d / style / sketch / lineart / realistic ——
# 实测「in a 3d room」「old style dress」「holding a sketch book」这类场景与道具描述会误命中，
# 而命中会把整套默认预设丢掉。收录过的具体画风由 presets.py 接管。
STYLE_REQUEST = re.compile(
    r"画风|风格|插画风|漫画风|二次元|涂鸦|"
    r"水彩|油画|素描|速写|写实|像素|厚涂|赛璐璐|平涂|"
    r"art style|illustration style|painterly|cel shading|oil painting|watercolou?r|pixel art",
    re.I,
)
# 参考图确立的默认画风：室内柔光漫射、低对比、浅肤色，身体要走插画质感而不是油光写实。
# 刻意不含 glossy 之外的 oiled / shiny / luminous skin —— 那一组会把身体推成半写实油光，
# 细节改由 painterly、detailed texture 与加权的 detailed skin 拿。
# 另：1.3 重压去色会连同暖色一起压掉，皮肤变成灰粉病态色，所以只保留档位内的去色。
# 不加 rim lighting / backlighting / volumetric lighting —— 那类舞台光会让光影失真。
DEFAULT_STYLE = (
    "soft shading",
    "smooth shading",
    "gradient shading",
    "airbrushed shading",
    "detailed skin",
    "glossy skin",
    "specular highlights",
    "porcelain skin",
    "white skin",
    "detailed face",
    "detailed eyes",
    "beautiful detailed eyes",
    "detailed hair",
    "hair highlights",
    "glossy hair",
    "0.7::colored lineart, thin lines::",
    "soft lineart",
    "soft edges",
    "blurred lineart",
    "gentle shading",
    "soft gradient",
    "smooth gradient",
    "blended shading",
    "soft blending",
    "subtle shading",
    "smooth transition",
    "depth of field",
    "blurry background",
    "soft focus",
    "soft lighting",
    "diffused lighting",
    "ambient light",
    "even lighting",
    "soft shadows",
    "low contrast",
    "gentle light",
    "muted colors",
    "dull colors",
    "desaturated",
    "low saturation",
    "faded colors",
    "subdued colors",
    "soft bloom",
    "hazy",
    "atmospheric haze",
    "dreamy",
    "warm grey",
    "beige",
    "warm ambient light",
    "pale skin",
    "light skin",
    "pastel colors",
    "warm tone",
    "anime style",
    "2d",
    "clean lineart",
    "1.2::detailed skin, detailed shading, intricate details::",
    "painterly",
    "detailed texture",
    "fine details",
    "1.3::detailed skin, detailed shading, intricate details::",
    "delicate features",
    # 重复一次是有意的：定稿配方里这一条出现了两次，眼睛的刻画强度依赖这次重复。
    "beautiful detailed eyes",
    "detailed iris",
    "detailed eyelashes",
    "1.2::detailed face, detailed eyes::",
)
DEFAULT_NEGATIVE_STYLE = (
    "dramatic lighting",
    "high contrast",
    "harsh shadows",
    "vivid colors",
    "saturated",
    # 反写实：摘掉油光标签后仍有涂抹回照片质感的趋势，这组是补上的闸。
    "realistic skin",
    "photorealistic",
    "hyperrealistic",
    "3d",
    "3d render",
    "skin pores",
    "oily skin",
    "wax figure",
    "doll",
)

# 恢复目标原图 PNG 中的默认柔光块；不把翻译附带的赛璐璐词写入默认档。
# 18:41、19:54 请求与 19:19 缓存目标原图的画风块一致。
HD_DEFAULT_STYLE = (
    'soft shading',
    'smooth shading',
    'gradient shading',
    'airbrushed shading',
    'detailed skin',
    'detailed face',
    'detailed eyes',
    'beautiful detailed eyes',
    'detailed hair',
    'hair highlights',
    'glossy hair',
    'soft lineart',
    'soft edges',
    'blurred lineart',
    'gentle shading',
    'soft gradient',
    'smooth gradient',
    'blended shading',
    'soft blending',
    'subtle shading',
    'smooth transition',
    'depth of field',
    'blurry background',
    'soft focus',
    'soft lighting',
    'diffused lighting',
    'ambient light',
    'even lighting',
    'soft shadows',
    'gentle light',
    'warm grey',
    'beige',
    'warm ambient light',
    'pale skin',
    'light skin',
    'pastel colors',
    'warm tone',
    'anime style',
    '2d',
    'clean lineart',
    'contrast',
    'saturated colors',
    'vivid colors',
    '0.7::colored lineart, thin lines::',
)
HD_DEFAULT_NEGATIVE_STYLE = ('dramatic lighting', 'harsh shadows')

# 19:17 默认配方不追加新的反写实排除词。
HD_DEFAULT_ONLY_NEGATIVE_STYLE = ()
HD_DEFAULT_DROP_NEGATIVES = frozenset()

# 提亮并加雾的这一组：暗室内景里有暖光源和暗背景垫着，柔化出来就是参考图的观感；
# 换到白天、黄昏、夜景、雪、海边，它就把整张冲成灰白——夜变成淡彩白天，夕阳没有夕阳色。
# 去色本身是要保留的（用户认可的那个柔色），过亮才是要压的。
HAZE_STYLE = frozenset({"soft bloom", "hazy", "atmospheric haze", "dreamy", "faded colors"})
# 亮场景额外压住过曝，这是灰白的直接来源。
# 去色档（vivid colors / saturated / high contrast）不在放开之列：放开会让暖调场景整体偏橘。
BRIGHT_NEGATIVE = ("overexposed", "blown out", "washed out", "bright", "high key")
# 出现这些词说明场景自身依靠色彩或光线，需要按亮场景处理。
# 刻意不含「夜」本身——夜里的室内暖灯场景正是默认画风的主场。
COLOR_SCENE = re.compile(
    r"白天|日间|晴天|正午|阳光|日照|"
    r"黄昏|夕阳|日落|日出|傍晚|暮色|"
    r"霓虹|夜景|灯火|"
    r"雨|雪|雷|雾|"
    r"户外|室外|野外|"
    r"沙滩|海边|大海|海洋|海岸|湖|河|水面|"
    r"天空|云海|星空|极光|"
    r"森林|树林|草原|花田|花海|原野|山|"
    r"街道|街头|马路|城市|都市|小镇|"
    r"学校|教室|操场|校园|"
    r"窗|窗外|阳台|天台|"
    r"daytime|sunset|sunrise|dusk|golden hour|neon|nightscape|"
    r"rain|snow|storm|outdoor|beach|ocean|sea|coast|lake|river|"
    r"sky|cloud|starry|aurora|forest|field|meadow|mountain|"
    r"street|city|town|school|classroom|window|balcony|rooftop",
    re.I,
)


def scene_keeps_haze(text: str) -> bool:
    """暗室内景保留雾化组；依赖色彩或光线的场景返回 False。"""
    return not COLOR_SCENE.search(text or "")


def drop_haze(tags: Sequence[str]) -> List[str]:
    return [tag for tag in tags if tag not in HAZE_STYLE]


def default_negative_style(hd: bool) -> List[str]:
    return list(HD_DEFAULT_NEGATIVE_STYLE if hd else DEFAULT_NEGATIVE_STYLE)


def scene_negatives(text: str, excluded: Sequence[str]) -> List[str]:
    """按场景调整排除词：亮场景追加过曝压制，去色档保持不变。"""
    if scene_keeps_haze(text):
        return list(excluded)
    return list(excluded) + [tag for tag in BRIGHT_NEGATIVE if tag not in excluded]


# 参考图确立的露骨画法：私处按解剖结构分面（小阴唇、阴蒂各自成形），柔粉渐变而非硬线，
# 表面湿润并带一道细亮高光；无马赛克。不给这些标签，模型只会糊一块平粉。
EXPLICIT_SCENE = re.compile(
    r"裸体|裸露|全裸|一丝不挂|脱光|乳头|巨乳|阴部|小穴|肉穴|阴道|阴蒂|阴茎|肉棒|龟头|"
    r"自慰|手淫|插入|抽插|后入|口交|手交|足交|射精|中出|精液|爱液|淫水|潮吹|做爱|性交|骑乘|"
    r"素股|股间|深喉|舔阴|肛交|精液|"
    r"nude|naked|pussy|penis|cock|nipple|clitoris|vagina|areola|"
    r"masturbat|fingering|penetrat|doggystyle|missionary|blowjob|handjob|footjob|"
    r"deepthroat|irrumatio|cunnilingus|thigh sex|anal sex|"
    r"cum|semen|creampie|orgasm|spread pussy|topless|bottomless",
    re.I,
)
# 仅在场上有男性或插入行为时才追加男根标签，否则给单人场景加阴茎是反向破坏。
PENIS_SCENE = re.compile(
    r"阴茎|肉棒|龟头|后入|插入|抽插|口交|中出|做爱|性交|骑乘|射精|精液|"
    r"penis|cock|doggystyle|missionary|penetrat|blowjob|handjob|creampie|cum|semen|vaginal|1boy|male",
    re.I,
)
# 分四层：通用质感、女性解剖、男根、具体行为。行为那层的标签互不通用——
# 自慰要的是张开腿与私处焦点，口交要的是口腔与唾液，中出要的是溢出与事后，
# 一套通吃等于什么都没说。
# soft pink shading 不列：它会把整片腹股沟染成粉红，颜色扩散出私处范围，
# 不摘掉的话身体是白的、只有这一块发红。湿滑组则要留着，它撑的是表面的湿润高光。
# 只留不做就会出马赛克或糊成平粉的项。wet / wet skin / glistening / moist skin 原本在这里，
# 但它们作用于整张图，会把全身画成油亮写实，与普通图的柔雾画风拉开明显差距；
# 现在下沉到 R18_FEMALE 的局部加权块，湿润只落在私处，其余部分与普通图同一套质感。
R18_BASE = (
    "uncensored",
    "detailed anatomy",
)
# 露骨场景把模型的注意力拉向解剖细节，脸容易被挤小、拉长、眼睛被发丝挡住。
# 单独一层在露骨时无条件补回面部细节；与 R18_BASE 同级，不随性别分支跳过。
R18_FACE = (
    "1.2::detailed face::",
    "1.5::detailed eyes, detailed iris, detailed eyelashes::",
)
# 露骨内容补一块柔光强调。实测同一份请求体、同种子，只把画布从 832×1216 换到
# 512×768（Small 档），画面就明显滑向写实：皮肤出照片质感、高光变硬。
# 加权细节在小画布上喂不饱，模型先丢的就是柔光画风，所以这里把它顶回来。
R18_SOFT_STYLE = (
    "1.15::soft shading, airbrushed shading, gentle shading, blended shading, "
    "soft edges, blurred lineart, soft focus::",
)
# 分面权重原为 1.3 / 1.2：画风里的柔化标签会把结构磨平，不加权就糊成一块平粉。
# 但小画布像素只有大画布的 39%，同样的权重喂不饱，反而把柔光画风挤掉，
# 故降到 1.15 / 1.05，与像素预算对齐。
R18_FEMALE = (
    "1.15::detailed pussy, visible labia, inner labia, detailed clitoris::",
    "1.05::glistening pussy, wet pussy, vaginal lubrication::",
    "detailed vulva",
    "vaginal opening",
)
# 胸部细节与私处解剖分开：裸体、口交、乳交这些场景需要胸，但不需要把镜头怼到私处。
R18_BREAST = ("detailed nipples", "areola")
# 只有描述确实涉及私处或用到私处的行为时才追加 R18_FEMALE。
# 裸体、topless、口交、手交、足交、乳交都不在这个集合里——无条件追加会让每张 R18
# 都凭空长出一个阴部分镜，哪怕提示词里根本没提。
GENITAL_SCENE = re.compile(
    r"阴部|私处|小穴|肉穴|阴道|阴唇|阴蒂|胯下|下体|私部|"
    r"自慰|手淫|抠|插入|抽插|后入|做爱|性交|骑乘|正常位|传教士|中出|舔阴|肛交|潮吹|爱液|淫水|"
    r"pussy|vulva|vagina|clitoris|labia|"
    r"masturbat|fingering|penetrat|doggystyle|missionary|cowgirl|creampie|"
    r"cunnilingus|anal sex|spread pussy|vulval",
    re.I,
)
R18_PENIS = ("detailed penis", "glans detail", "veined penis", "precum")
R18_ACTS = (
    (re.compile(r"自慰|手淫|抠|揉|masturbat|fingering", re.I),
     ("spread legs", "spread pussy", "fingering", "solo")),
    (re.compile(r"后入|背后位|从后面|趴|doggystyle|from behind", re.I),
     ("from behind", "doggystyle", "ass focus", "top-down bottom-up")),
    (re.compile(r"骑乘|骑乘位|女上|cowgirl", re.I),
     ("cowgirl position", "straddling", "girl on top")),
    (re.compile(r"正常位|传教士|missionary", re.I),
     ("missionary", "on back", "legs up")),
    (re.compile(r"口交|吹箫|舔.*(棒|茎)|含.*(棒|茎)|blowjob|fellatio", re.I),
     ("fellatio", "penis in mouth", "saliva", "oral")),
    (re.compile(r"手交|撸|手活|handjob", re.I),
     ("handjob", "hand on penis", "lubricant")),
    (re.compile(r"足交|footjob", re.I),
     ("footjob", "foot on penis", "soles", "toes")),
    (re.compile(r"乳交|胸交|paizuri", re.I),
     ("paizuri", "breasts around penis")),
    (re.compile(r"中出|内射|射在里|creampie", re.I),
     ("cum in pussy", "cum", "semen", "overflow")),
    (re.compile(r"颜射|facial", re.I),
     ("facial", "cum on face", "cum on hair")),
    (re.compile(r"射精|精液|潮吹|squirt|semen|cumshot", re.I),
     ("cum", "semen")),
    (re.compile(r"事后|做完|after sex", re.I),
     ("after sex", "cum dripping", "used")),
    (re.compile(r"露出|野外.*做|exhibitionism", re.I),
     ("exhibitionism", "public indecency")),
    (re.compile(r"素股|大腿夹|股间|thigh sex|intercrural", re.I),
     ("thigh sex", "penis between thighs", "thighs focus")),
    (re.compile(r"深喉|咽喉|deepthroat|irrumatio", re.I),
     ("deepthroat", "irrumatio", "saliva", "tears")),
    (re.compile(r"舔阴|舔穴|cunnilingus", re.I),
     ("cunnilingus", "tongue out", "licking pussy", "oral")),
    (re.compile(r"肛交|anal", re.I),
     ("anal", "anal sex")),
    (re.compile(r"站立|墙上|立位|standing sex|against wall", re.I),
     ("standing sex", "standing", "against wall")),
    (re.compile(r"浴室|淋浴|泡澡|shower|bath", re.I),
     ("shower", "bathroom", "wet hair", "water", "steam")),
    (re.compile(r"内衣|泳装|蕾丝|lingerie|underwear|panties|bikini", re.I),
     ("lingerie", "underwear", "lace trim")),
    (re.compile(r"三人|双飞|多p|群p|threesome|3some|group sex", re.I),
     ("threesome", "3some", "group sex")),
    (re.compile(r"束缚|捆绑|绳|bondage|shibari", re.I),
     ("bondage", "shibari", "rope", "bound wrists")),
    (re.compile(r"揉胸|乳首|乳房焦点|breast focus", re.I),
     ("breasts focus", "grabbing own breasts", "cleavage")),
)
# 取景层：同一行为在不同取景下要补的东西不同。特写要压局部细节，全身要交代姿势，
# 第一人称必须立住摄像机的立场，否则模型会把镜头拉回第三人称。
R18_FRAMING = (
    # 只收紧镜头，不追加 detail focus：那个标签会让模型另开一块局部放大插图，
    # 于是每张都凭空多出一个阴部分镜。
    (re.compile(r"特写|近景|局部|close-?up|zoom", re.I),
     ("close-up",)),
    (re.compile(r"全身|全身像|full body|wide shot", re.I),
     ("full body", "wide shot")),
    (re.compile(r"俯视|从上往下|from above|bird'?s eye", re.I),
     ("from above",)),
    (re.compile(r"仰视|从下往上|from below", re.I),
     ("from below",)),
    (re.compile(r"第一人称|男主视角|男性视角|pov", re.I),
     ("pov", "male pov")),
)
# 只在女性在场时追加女性解剖标签；纯男性场景加这些是反向破坏。
MALE_ONLY = re.compile(r"1boy|1男人|男性", re.I)
FEMALE_MARK = re.compile(r"1girl|1女孩|girl|female|女性|少女|女方", re.I)
# 露骨场景手部离私处很近，手指粘连概率高；同时明确挡住马赛克。
# 注意别用负面做角色矫正：实测官方角色标签带来的特征（如兽耳轮廓）压不掉——v4.5 的
# v4_negative_prompt 只管 base caption，对角色标签条件化的内容无效，加权到 1.3 也一样。
R18_NEGATIVE_STYLE = (
    "censored",
    "mosaic censoring",
    "bar censor",
    "fused fingers",
    "extra fingers",
    "missing fingers",
    "malformed hands",
    "bad proportions",
    "extra limbs",
)


def explicit_tags(text: str) -> List[str]:
    """按在场性别与具体行为，分层追加露骨细节标签。"""
    text = text or ""
    if not EXPLICIT_SCENE.search(text):
        return []
    tags = list(R18_BASE) + list(R18_FACE) + list(R18_SOFT_STYLE)
    if not (MALE_ONLY.search(text) and not FEMALE_MARK.search(text)):
        tags.extend(R18_BREAST)
        # 私处解剖只在描述涉及私处时追加，否则裸体/口交类的图会被强行拉出阴部分镜。
        if GENITAL_SCENE.search(text):
            tags.extend(R18_FEMALE)
    if PENIS_SCENE.search(text):
        tags.extend(R18_PENIS)
    for pattern, extra in R18_ACTS + R18_FRAMING:
        if pattern.search(text):
            tags.extend(extra)
    return tags


# 只在"画面里只有一个主体"时成立的标签，多人全局栏里必须剔除。
SINGLE_SUBJECT_TAGS = frozenset({
    "solo", "pussy focus", "ass focus", "detail focus", "pov", "male pov",
})


def explicit_global_tags(text: str) -> List[str]:
    """多人合用的露骨层：全场成立的部分，去掉只在单个角色身上成立的那几组。

    base 提示词在 V5 里同时作用于每个角色，私处解剖标签放进去会漂到不该有的位置；
    但反马赛克、面部细节回填、柔光强调和具体行为（插入、后入、自慰…）都是全场共用的事实，
    多人一样必须有——此前整层被跳过，导致多人 R18 请求的动作标签一个都进不了提示词。

    另有一批标签只在"画面里只有一个主体"时成立：``solo`` 直接声明单主体，``pussy focus``
    / ``ass focus`` / ``detail focus`` 和 pov 系列是单主体视角的镜头指令。放进多人全局栏，
    模型会把三个人挤成一个主体并怼近镜头，构图直接崩掉。
    """
    return [tag for tag in explicit_tags(text)
            if tag not in R18_FEMALE and tag not in SINGLE_SUBJECT_TAGS]


def explicit_negatives(text: str) -> List[str]:
    return list(R18_NEGATIVE_STYLE) if EXPLICIT_SCENE.search(text or "") else []
# 只映射视觉表现；不继承人物、衣着、场景或构图。
STYLE_TERMS = (
    (r"日系二次元|日系.*插画|anime illustration", "anime illustration"),
    (r"细腻.*线|精细.*线|细线|线条细致|细致线条|fine lineart|delicate lines", "delicate lines"),
    (r"干净.*线|清晰.*线|clean lineart", "clean fine lineart"),
    (r"柔和.*渐变|柔软.*渐变|明亮柔和|soft gradient", "soft gradient shading"),
    (r"低饱和|浅色调|淡色|配色清透|清透配色|pale.*palette|muted colors", "pale color palette"),
    (r"柔光|柔和.*光|soft lighting", "soft lighting"),
    (r"水彩|watercolou?r", "watercolor"),
    (r"赛璐璐|cel shading", "cel shading"),
    (r"厚涂|painterly", "painterly"),
    (r"素描|sketch", "sketch"),
    (r"油画|oil painting", "oil painting"),
)


def style_tags(roles: Sequence[Character]) -> List[str]:
    """固定词表抽取外观卡的画风信息，保持顺序并去重。"""
    clauses = [clause for role in roles for card in role.descriptions for clause in re.split(r"[,，。;；\n]", card)]
    positive = [clause for clause in clauses if not re.search(r"不要|不使用|不采用|避免|非.{0,4}风格|do not|don't|without|avoid|no ", clause, re.I)]
    return [tag for pattern, tag in STYLE_TERMS if any(re.search(pattern, clause, re.I) for clause in positive)]
