"""特色画风预设。

点名某档画风时整档替换默认预设，而不是把默认预设整个丢掉：每档自带标签、
要从基础负面里摘掉的冲突项、以及一张风格参考图。没有点名时走默认柔光档。

参考图只在 4.5 Full 上生效（官方限制），而且每图最多两张参考，
所以角色参考已经占满时不再追加，改为纯标签。
"""
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
import json
import re

from PIL import Image

ASSET_DIR = Path(__file__).with_name("assets") / "styles"
# 5 Full 那套参考图放在子目录：文件名与 4.5 那套一一对应，便于并排比对。
V5_DIR = "v5"
# 基础负面里与"硬边/平涂/锐线"有关的这几项，赛璐璐那档必须摘掉，否则正负打架，
# 实测会解成高反差平涂甚至像素画。
HARD_EDGE_NEGATIVES = (
    "sharp lines",
    "sharp focus",
    "hard edges",
    "crisp lineart",
    "cel shading",
    "hard shading",
    "banded shading",
    "posterized",
    "flat colors",
    "two-tone shading",
)


@dataclass(frozen=True)
class GenerationTuning:
    """逐档验证的生成参数；用户在指令中明确填写的参数始终优先。"""

    negative_tags: Optional[Tuple[str, ...]] = None
    scale: Optional[float] = None
    steps: Optional[int] = None
    sampler: Optional[str] = None
    style_weight: Optional[float] = None


@dataclass(frozen=True)
class StylePreset:
    key: str
    label: str
    keywords: Tuple[str, ...]
    tags: Tuple[str, ...]
    extra_negatives: Tuple[str, ...] = ()
    drop_negatives: Tuple[str, ...] = ()
    reference: str = ""
    reference_strength: float = 0.7
    reference_fidelity: float = 0.2
    generation: Optional[GenerationTuning] = None
    generation_v5: Optional[GenerationTuning] = None
    tags_v5: Optional[Tuple[str, ...]] = None
    extra_negatives_v5: Optional[Tuple[str, ...]] = None
    drop_negatives_v5: Optional[Tuple[str, ...]] = None

    @property
    def reference_path(self) -> Path:
        return ASSET_DIR / self.reference

    @property
    def reference_v5_path(self) -> Path:
        """5 Full 那套参考图，只供查看画风并排比对。

        5 Full 当前不支持参考图，生成时不会发送任何一套图片。
        """
        return ASSET_DIR / V5_DIR / self.reference


def generation_tuning(preset: StylePreset, v5: bool = False) -> Optional[GenerationTuning]:
    return preset.generation_v5 if v5 else preset.generation


@lru_cache(maxsize=128)
def preview_recipe(preset: StylePreset, v5: bool = False) -> Tuple[Tuple[str, ...], float, str]:
    """读取对应模型预览图的生成配方；仅取负面和采样参数，不继承示例人物、服装和场景。"""
    path = preset.reference_v5_path if v5 else preset.reference_path
    with Image.open(path) as image:
        metadata = json.loads(image.info["Comment"])
    negative = tuple(tag.strip() for tag in metadata["uc"].split(",") if tag.strip())
    return negative, float(metadata["scale"]), metadata["sampler"]


# 逐张查看不可变的原始参考图后记录的画法，而非追随已经偏离原图的 v5 样图。
# 不包含样图人物、衣服、道具或构图。torino 保持原有已确认配方。
V5_REFERENCE_RENDER: Dict[str, Tuple[str, ...]] = {
    "achiki": ("clean lineart", "colored lineart", "cel shading", "gradient shading", "soft highlights"),
    "aroa": ("thin lineart", "colored lineart", "pastel colors", "low contrast", "flat colors", "0.7::halftone, paper texture::"),
    "as109": ("clean lineart", "high contrast", "warm lighting", "deep shadows", "smooth shading", "glossy highlights"),
    "ask": ("thin lineart", "colored lineart", "muted colors", "cel shading", "soft lighting", "cool shadows", "0.7::paper texture::"),
    "atdan": ("delicate lineart", "gradient shading", "soft shading", "glossy highlights", "0.3::iridescent highlights::", "0.7::paper texture::"),
    "ciloranko": ("colored lineart", "pale colors", "low saturation", "soft lighting", "textured shading", "soft edges", "0.8::halftone::"),
    "cola": ("clean lineart", "cel shading", "gradient shading", "cool shadows", "warm highlights", "0.7::paper texture::"),
    "emyo": ("thin lineart", "pale colors", "low contrast", "soft shading", "subtle highlights", "0.7::paper texture::"),
    "fajyobore": ("colored lineart", "muted colors", "hard shading", "backlighting", "rim lighting", "0.6::chromatic aberration, film grain::"),
    "fuzichoco": ("colored lineart", "vibrant colors", "painterly", "hard shading", "iridescent highlights", "textured shading"),
    "hiten": ("thin lineart", "pastel colors", "cel shading", "gradient shading", "soft lighting", "0.7::paper texture::"),
    "hitsukuya": ("thin lineart", "muted colors", "soft shading", "0.7::halftone, paper texture::"),
    "iren_lovel": ("thin lineart", "pale colors", "low contrast", "cel shading", "bloom", "0.7::paper texture::"),
    "kantoku": ("clean lineart", "thin lines", "cel shading", "pale colors", "simple shading", "0.7::paper texture::"),
    "ke-ta": ("delicate lineart", "pastel colors", "glossy highlights", "gradient shading", "soft shading", "0.7::paper texture::"),
    "konya_karasue": ("thin lineart", "muted colors", "cel shading", "simple shading", "neutral lighting", "diffused lighting", "0.8::paper texture::"),
    "krenz": ("painterly", "soft edges", "blended shading", "muted colors", "neutral lighting", "0.7::rim lighting, halftone::"),
    "kuroboshi_kouhaku": ("clean lineart", "pale colors", "simple shading", "cel shading", "diffused lighting", "0.7::paper texture::"),
    "lack": ("clean lineart", "cel shading", "gradient shading", "glossy highlights", "warm lighting", "0.7::paper texture::"),
    "loundraw": ("colored lineart", "flat colors", "hard shading", "diffused lighting", "high key", "0.8::limited palette::"),
    "mifuru": ("thin lineart", "muted colors", "cel shading", "rim lighting", "textured shading"),
    "mika_pikazo": ("delicate lineart", "gradient shading", "soft shading", "pastel colors", "0.7::colored lineart, paper texture::"),
    "misaki_kurehito": ("delicate lineart", "gradient shading", "glossy highlights", "bloom", "vibrant colors", "soft lighting"),
    "miv4t": ("clean lineart", "muted colors", "gradient shading", "cel shading", "paper texture"),
    "mocha": ("colored lineart", "gradient shading", "cool lighting", "bloom", "glossy highlights", "0.7::paper texture::"),
    "modare": ("thin lineart", "matte colors", "hard shading", "cool shadows", "diffused lighting", "0.7::paper texture::"),
    "momoko": ("delicate lineart", "pastel colors", "low contrast", "soft shading", "subtle highlights", "paper texture"),
    "rella": ("thin lineart", "muted colors", "pale colors", "cel shading", "matte colors", "front lighting", "diffused lighting", "neutral lighting", "1.1::flat colors::", "0.5::chromatic aberration, paper texture::"),
    "rurudo": ("delicate lineart", "colored lineart", "pale colors", "cel shading", "soft lighting", "0.7::paper texture::"),
    "sakimichan": ("semi-realistic", "smooth shading", "airbrush", "soft edges", "glossy highlights", "rim lighting"),
    "shimada_fumikane": ("clean lineart", "thin lines", "cel shading", "pastel colors", "gradient shading", "0.7::paper texture::"),
    "so-bin": ("painterly", "high contrast", "deep shadows", "hard shading", "textured shading", "rim lighting"),
    "tamaki": ("clean lineart", "flat colors", "cel shading", "simple shading", "0.7::paper texture::"),
    "tokkyu": ("clean lineart", "smooth shading", "glossy highlights", "high contrast", "reflected light", "0.7::paper texture::"),
    "wanke": ("thin lineart", "muted colors", "cel shading", "matte colors", "cool shadows", "diffused lighting", "1.1::flat colors::", "0.7::paper texture::"),
    "wlop": ("semi-realistic", "painterly", "soft edges", "blended shading", "muted colors", "deep shadows"),
    "yoneyama_mai": ("anime style", "2d", "clean lineart", "defined outlines", "cel shading", "flat colors", "sharp highlights", "detailed eyes", "0.6::paper texture::", "depth of field", "blurry background"),
}


def preset_render_tags(preset: StylePreset, v5: bool = False) -> Tuple[str, ...]:
    """模型专用配方明确隔离，未单独校准的档沿用已有目标标签。"""
    return preset.tags_v5 if v5 and preset.tags_v5 is not None else preset.tags


# 画师档共用的一小段底：够撑住完成度，又不会盖掉画师本身的手感。
_ARTIST_RENDER = (
    "detailed skin", "detailed face", "detailed eyes", "detailed hair",
    "soft lighting", "depth of field",
)

# 柔光系画师只放行「细线」，其余反差项继续压制。
# 整组 HARD_EDGE_NEGATIVES 摘掉会把 cel shading / flat colors / posterized 一起放行，
# 实测会把柔光厚涂拉回通用平涂——torino aqua 这类画师恰好死在这一步。
LINES_OK_NEGATIVES_DROP = ("sharp lines", "crisp lineart")


# 顺序即优先级：更具体的关键词排前面，避免「半写实」被「写实」先截走。
PRESETS: Tuple[StylePreset, ...] = (
    StylePreset(
        key="painterly",
        label="厚涂",
        keywords=("厚涂", "油画", "笔触", "半写实", "painterly", "oil painting", "impasto", "semi-realistic"),
        tags=(
            "painterly", "oil painting", "thick brushstrokes", "textured brushwork",
            "detailed skin", "detailed shading", "soft lighting", "muted colors", "warm tone",
            "digital painting", "concept art", "detailed face", "detailed eyes",
            "delicate features", "fine details", "depth of field",
        ),
        extra_negatives=("anime style", "cel shading", "flat colors", "clean lineart", "cartoon", "chibi"),
        reference="painterly.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="cel",
        label="赛璐璐",
        keywords=("赛璐璐", "平涂", "动画风", "动画涂", "cel shading", "cel", "anime screencap"),
        tags=(
            "anime style", "official art", "anime screencap", "clean lineart", "2d", "anime coloring",
            "cel shading", "flat colors", "two-tone shading", "detailed eyes", "detailed face", "sharp lines",
        ),
        extra_negatives=("painterly", "semi-realistic", "realistic skin", "blurred lineart",
                         "pixel art", "dithering", "retro game art", "limited palette"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="cel.png",
        reference_strength=0.65,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="watercolor",
        label="水彩",
        keywords=("水彩", "水彩风", "watercolor", "watercolour"),
        tags=(
            "watercolor", "watercolor (medium)", "wet on wet", "soft washes", "paper texture",
            "bleeding colors", "light colors", "transparent", "ink outline", "traditional media",
            "pale colors", "delicate lines",
        ),
        extra_negatives=("cel shading", "oil painting", "thick brushstrokes", "heavy shadows", "photorealistic"),
        reference="watercolor.png",
        reference_strength=0.7,
        reference_fidelity=0.2,
    ),
    StylePreset(
        key="sketch",
        label="素描",
        # 不收裸 sketch / pencil：实测「holding a sketch book」这类道具描述会被误命中。
        keywords=("素描", "铅笔素描", "速写", "线稿", "sketch style", "pencil sketch",
                  "sketch drawing", "rough sketch", "graphite", "lineart"),
        tags=(
            "1.3::sketch, monochrome, greyscale, pencil::", "graphite (medium)", "lineart", "hatching",
            "rough sketch", "paper texture", "traditional media", "cross-hatching",
        ),
        extra_negatives=("colorful", "full color", "painting", "cel shading", "photorealistic", "glossy skin", "airbrushed shading"),
        drop_negatives=LINES_OK_NEGATIVES_DROP,
        reference="sketch.png",
        reference_strength=0.7,
        reference_fidelity=0.2,
        generation_v5=GenerationTuning(negative_tags=(), scale=5.5, steps=28, sampler="k_euler_ancestral", style_weight=1.0),
    ),
    StylePreset(
        key="pixel",
        label="像素",
        keywords=("像素风", "像素画", "像素", "pixel art", "pixelated", "8-bit", "16-bit"),
        tags=(
            "pixel art", "8-bit", "limited palette", "dithering", "retro game art",
            "pixelated", "sprite",
        ),
        extra_negatives=("smooth shading", "soft shading", "blur", "photorealistic", "painting", "painterly"),
        reference="pixel.png",
        reference_strength=0.7,
        reference_fidelity=0.2,
    ),
    StylePreset(
        key="photoreal",
        label="写实",
        keywords=("写实", "照片", "真人", "photorealistic", "realistic", "photograph"),
        tags=(
            "photorealistic", "realistic", "photograph", "detailed skin texture",
            "subsurface scattering", "natural lighting", "depth of field", "50mm",
            "film grain", "photographic", "soft lighting",
        ),
        extra_negatives=("anime", "cartoon", "cel shading", "illustration", "flat colors",
                         "painting", "drawing", "2d", "lineart"),
        drop_negatives=("photorealistic",),
        reference="photoreal.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="sharp",
        label="锐利线稿",
        keywords=("锐利线稿", "锐利线条", "清晰线条", "精线", "硬朗线条",
                  "sharp lineart", "crisp lineart", "precise lineart"),
        tags=(
            "anime style", "2d", "clean lineart", "precise lineart", "sharp lines", "defined lines",
            "high contrast", "vivid colors", "saturated colors", "detailed eyes", "detailed face",
            "detailed hair", "sharp focus", "crisp edges",
        ),
        extra_negatives=("painterly", "semi-realistic", "realistic skin", "soft shading",
                         "blurred lineart", "soft focus", "hazy", "faded colors", "muted colors"),
        # 基础负面里挡"硬边/锐线"的那几项必须摘掉，否则正面要锐线、负面在挡锐线。
        drop_negatives=(*HARD_EDGE_NEGATIVES, "high contrast", "vivid colors"),
        reference="sharp.png",
        reference_strength=0.65,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="weiguang",
        label="微光",
        keywords=("微光", "微光画风", "weiguang", "soft glow"),
        tags=(
            "1.05::artist:machi::", "semi-realistic", "painterly",
            "detailed skin", "detailed face", "detailed eyes", "detailed hair",
            "soft lighting", "depth of field", "glossy hair", "intricate details",
        ),
        # 这一档要的是半写实厚涂，基础负面里没有压它，只需挡平涂与赛璐璐。
        extra_negatives=("anime style", "cel shading", "flat colors", "chibi"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="weiguang.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_ciloranko",
        label="画师·ciloranko",
        keywords=("ciloranko", "画师ciloranko"),
        tags=("1.05::artist:ciloranko::", *_ARTIST_RENDER),
        extra_negatives=("painterly", "semi-realistic", "realistic skin", "soft shading", "blurred lineart"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_ciloranko.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_rella",
        label="画师·rella",
        keywords=("rella", "画师rella"),
        tags=("1.05::artist:rella::", *_ARTIST_RENDER),
        extra_negatives=("painterly", "semi-realistic", "realistic skin", "sharp lines", "high contrast"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_rella.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_wlop",
        label="画师·wlop",
        keywords=("wlop", "画师wlop"),
        tags=("1.05::artist:wlop::", *_ARTIST_RENDER),
        extra_negatives=("anime style", "cel shading", "flat colors", "clean lineart", "chibi"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_wlop.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_mika_pikazo",
        label="画师·mika pikazo",
        keywords=("mika pikazo", "mikapikazo", "画师mikapikazo"),
        tags=("1.05::artist:mika pikazo::", *_ARTIST_RENDER),
        extra_negatives=("painterly", "semi-realistic", "muted colors", "desaturated", "dull colors"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_mika_pikazo.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_hiten",
        label="画师·hiten",
        keywords=("hiten", "画师hiten"),
        tags=("1.05::artist:hiten::", *_ARTIST_RENDER),
        extra_negatives=("painterly", "semi-realistic", "realistic skin", "blurred lineart", "hazy"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_hiten.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_fuzichoco",
        label="画师·fuzichoco",
        keywords=("fuzichoco", "画师fuzichoco"),
        tags=("1.05::artist:fuzichoco::", *_ARTIST_RENDER),
        extra_negatives=("photorealistic", "realistic skin", "muted colors", "desaturated"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_fuzichoco.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_wanke",
        label="画师·wanke",
        keywords=("wanke", "画师wanke"),
        tags=("1.05::artist:wanke::", *_ARTIST_RENDER),
        extra_negatives=("painterly", "semi-realistic", "realistic skin", "high contrast"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_wanke.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_ke-ta",
        label="画师·ke-ta",
        keywords=("ke-ta", "画师ke-ta"),
        tags=("1.05::artist:ke-ta::", *_ARTIST_RENDER),
        extra_negatives=("painterly", "semi-realistic", "high contrast", "sharp lines"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_ke-ta.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_ask",
        label="画师·ask",
        keywords=("ask_(askzy)", "ask (askzy)", "画师ask"),
        tags=("1.05::artist:ask_(askzy)::", *_ARTIST_RENDER),
        extra_negatives=("painterly", "semi-realistic", "muted colors", "desaturated"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_ask.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_yoneyama_mai",
        label="画师·yoneyama mai",
        keywords=("yoneyama mai", "yoneyamai", "画师yoneyama"),
        tags=("1.05::artist:yoneyama mai::", *_ARTIST_RENDER),
        extra_negatives=("painterly", "semi-realistic", "flat colors", "muted colors"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_yoneyama_mai.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_krenz",
        label="画师·krenz",
        keywords=("krenz", "画师krenz"),
        tags=("1.05::artist:krenz::", *_ARTIST_RENDER),
        extra_negatives=("anime style", "cel shading", "flat colors", "chibi"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_krenz.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_kantoku",
        label="画师·kantoku",
        keywords=("kantoku", "画师kantoku"),
        tags=("1.05::artist:kantoku::", *_ARTIST_RENDER),
        extra_negatives=("painterly", "semi-realistic", "realistic skin", "sharp lines"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_kantoku.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_shimada_fumikane",
        label="画师·shimada fumikane",
        keywords=("shimada fumikane", "shimada_fumikane", "画师shimada"),
        tags=("1.05::artist:shimada fumikane::", *_ARTIST_RENDER),
        extra_negatives=("painterly", "semi-realistic", "soft shading", "blurred lineart"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_shimada_fumikane.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_as109",
        label="画师·as109",
        keywords=("as109", "画师as109"),
        # 不加 1.05 权重：实测带权重的 1.05::artist:as109:: 会直接崩成噪点图，去掉就正常。
        tags=("artist:as109", *_ARTIST_RENDER),
        extra_negatives=("anime style", "cel shading", "flat colors", "chibi"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_as109.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_lack",
        label="画师·lack",
        keywords=("lack", "画师lack"),
        tags=("1.05::artist:lack::", *_ARTIST_RENDER),
        extra_negatives=("painterly", "semi-realistic", "realistic skin", "blurred lineart"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_lack.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_misaki_kurehito",
        label="画师·misaki kurehito",
        keywords=("misaki kurehito", "misaki_kurehito", "画师misaki"),
        tags=("1.05::artist:misaki kurehito::", *_ARTIST_RENDER),
        extra_negatives=("painterly", "semi-realistic", "realistic skin", "sharp lines"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_misaki_kurehito.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_modare",
        label="画师·modare",
        keywords=("modare", "画师modare"),
        tags=("1.05::artist:modare::", *_ARTIST_RENDER),
        extra_negatives=("painterly", "semi-realistic", "high contrast"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_modare.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_atdan",
        label="画师·atdan",
        keywords=("atdan", "画师atdan"),
        tags=("1.05::artist:atdan::", *_ARTIST_RENDER),
        extra_negatives=("painterly", "semi-realistic", "muted colors", "desaturated"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_atdan.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_sakimichan",
        label="画师·sakimichan",
        keywords=("sakimichan", "画师sakimichan"),
        tags=("1.05::artist:sakimichan::", *_ARTIST_RENDER),
        extra_negatives=("anime style", "cel shading", "flat colors", "chibi"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_sakimichan.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_kuroboshi_kouhaku",
        label="画师·kuroboshi kouhaku",
        keywords=("kuroboshi kouhaku", "kuroboshi_kouhaku", "画师kuroboshi"),
        tags=("1.05::artist:kuroboshi kouhaku::", *_ARTIST_RENDER),
        extra_negatives=("painterly", "semi-realistic", "high contrast", "sharp lines"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_kuroboshi_kouhaku.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_torino_aqua",
        label="画师·torino aqua",
        keywords=("torino aqua", "torino_aqua", "画师torino"),
        # torino 的手感全在「虹彩发丝 + 发光肌肤 + 通透渐变瞳」这三件事上。
        # 光给 artist 标签加通用完成度词，出来的是通用柔光图，认不出是他。
        tags=(
            "1.05::artist:torino aqua::",
            "iridescent hair", "prismatic highlights", "rainbow reflections",
            "glowing skin", "soft skin", "blush",
            "detailed eyes", "gradient eyes", "sparkling eyes",
            "delicate lineart", "soft shading",
            "luminous", "bloom", "airy atmosphere", "highly detailed",
            "soft lighting", "depth of field",
        ),
        # 压反差，但保留暗部：整幅提亮 + 参考图的紫调会把角色的黑衣服染掉，
        # 「luminous / bloom / pastel colors」和过强的参考图叠在一起就是那个下场。
        extra_negatives=("painterly", "semi-realistic", "high contrast", "harsh shadows", "washed out", "overexposed"),
        drop_negatives=LINES_OK_NEGATIVES_DROP,
        reference="artist_torino_aqua.png",
        reference_strength=0.5,
        reference_fidelity=0.25,
    ),
    StylePreset(
        key="artist_so-bin",
        label="画师·so-bin",
        keywords=("so-bin", "sobin", "画师so-bin"),
        tags=("1.05::artist:so-bin::", *_ARTIST_RENDER),
        extra_negatives=("anime style", "cel shading", "flat colors", "chibi"),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference="artist_so-bin.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    ),
)

# 往后新增的画师档走这张表：字段一致，没必要一段段重复 StylePreset(...)。
# 字段：slug、画师名（同时用作标签和显示名）、触发词、追加负面、是否加 1.05 权重。
# 权重那一栏不是摆设：`1.05::artist:xxx::` 会让个别标签直接崩成空白图——
# as109（带数字）与 tamaki.（带点）实测两版都出白图，去掉权重就恢复正常。
_ARTIST_TABLE: Tuple[Tuple[str, str, Tuple[str, ...], str, bool], ...] = (
    ("iren_lovel", "iren lovel", ("iren lovel", "iren_lovel", "画师iren"),
     "painterly, semi-realistic, high contrast", True),
    ("aroa", "aroa (aroamoyasi)", ("aroa (aroamoyasi)", "aroa_aroamoyasi", "画师aroa"),
     "painterly, semi-realistic, realistic skin, sharp lines", True),
    ("mocha", "mocha (cotton)", ("mocha (cotton)", "mocha_cotton", "画师mocha"),
     "painterly, semi-realistic, high contrast", True),
    ("hitsukuya", "hitsukuya", ("hitsukuya", "画师hitsu"),
     "painterly, semi-realistic, realistic skin, blurred lineart", True),
    ("cola", "cola (gotouryouta)", ("cola (gotouryouta)", "画师cola", "コーラ"),
     "anime style, cel shading, flat colors, chibi", True),
    ("tamaki", "tamaki", ("tamaki", "画师tamaki"),
     "painterly, semi-realistic, high contrast", False),
    ("momoko", "momoko (momopoco)", ("momoko (momopoco)", "momoko_momopoco", "画师momoko", "ももこ"),
     "anime style, cel shading, flat colors, chibi", True),
    ("konya_karasue", "konya karasue", ("konya karasue", "konya_karasue", "画师konya", "紺屋鴉江"),
     "painterly, semi-realistic, sharp lines, high contrast", True),
    ("achiki", "achiki", ("achiki", "画师achiki", "Aちき"),
     "painterly, semi-realistic, muted colors, desaturated", True),
    ("tokkyu", "tokkyu", ("tokkyu", "画师tokkyu"),
     "anime style, cel shading, flat colors, chibi", True),
    ("rurudo", "rurudo", ("rurudo", "画师rurudo"),
     "painterly, semi-realistic, muted colors, desaturated", True),
    ("mifuru", "mifuru", ("mifuru", "画师mifuru", "みふる"),
     "painterly, semi-realistic, high contrast", True),
    ("miv4t", "miv4t", ("miv4t", "画师miv4t"),
     "anime style, cel shading, flat colors, chibi", True),
    ("fajyobore", "fajyobore", ("fajyobore", "画师fajyobore", "ファジョボレ"),
     "painterly, semi-realistic, high contrast", True),
    ("emyo", "emyo", ("emyo", "画师emyo", "えみょ"),
     "painterly, semi-realistic, muted colors, desaturated", True),
    ("loundraw", "loundraw", ("loundraw", "画师loundraw"),
     "anime style, cel shading, flat colors, chibi", True),
    # 半写实、细节重的画法：负面不能再压 painterly / semi-realistic，
    # 否则正好把这一档的手感挡掉，只剩平涂。
    ("machi", "machi", ("machi", "画师machi"),
     "anime style, cel shading, flat colors, chibi", True),
)

PRESETS = PRESETS + tuple(
    StylePreset(
        key=f"artist_{slug}",
        label=f"画师·{artist}",
        keywords=keywords,
        tags=((f"1.05::artist:{artist}::" if weighted else f"artist:{artist}"), *_ARTIST_RENDER),
        extra_negatives=tuple(tag.strip() for tag in negative.split(",")),
        drop_negatives=HARD_EDGE_NEGATIVES,
        reference=f"artist_{slug}.png",
        reference_strength=0.7,
        reference_fidelity=0.25,
    )
    for slug, artist, keywords, negative, weighted in _ARTIST_TABLE
)

# 原图逐档审计后的候选参数；实际插件同种子出图记录见调校报告。
# 分组只复用相容的负面底，各档仍有自己的视觉词、CFG 和画师权重。
_RENDER_FAMILY_NEGATIVES: Dict[str, Tuple[str, ...]] = {
    "cel": ("photorealistic", "3d", "oil painting", "thick brushstrokes", "airbrushed shading", "overexposed", "washed out"),
    "soft": ("photorealistic", "3d", "oil painting", "thick brushstrokes", "overexposed", "washed out"),
    "paint": ("photorealistic", "3d", "plastic skin", "overexposed", "washed out"),
    "volume": ("3d", "flat colors", "cel shading", "chibi", "overexposed", "washed out"),
}
_ARTIST_CALIBRATION: Dict[str, Tuple[str, float, float]] = {
    "achiki": ("cel", 5.5, 1.15), "aroa": ("cel", 4.5, 1.10),
    "as109": ("soft", 5.5, 1.0), "ask": ("cel", 5.2, 1.05),
    "atdan": ("soft", 5.2, 0.85), "ciloranko": ("soft", 5.2, 1.05),
    "cola": ("cel", 5.0, 1.20), "emyo": ("soft", 4.8, 1.15),
    "fajyobore": ("cel", 5.2, 1.20), "fuzichoco": ("paint", 5.5, 1.20),
    "hiten": ("cel", 5.0, 1.15), "hitsukuya": ("soft", 4.5, 1.10),
    "iren_lovel": ("cel", 4.5, 1.10), "kantoku": ("cel", 5.2, 1.15),
    "ke-ta": ("soft", 5.0, 1.15), "konya_karasue": ("cel", 5.3, 1.05),
    "krenz": ("paint", 5.2, 1.05), "kuroboshi_kouhaku": ("cel", 5.2, 1.05),
    "lack": ("cel", 5.5, 1.20), "loundraw": ("cel", 5.2, 1.10),
    "mifuru": ("cel", 4.8, 1.20), "mika_pikazo": ("soft", 4.8, 0.55),
    "misaki_kurehito": ("soft", 5.2, 1.20), "miv4t": ("cel", 5.0, 1.15),
    "mocha": ("soft", 5.0, 1.15), "modare": ("cel", 5.2, 1.10),
    "momoko": ("soft", 4.5, 1.10), "rella": ("cel", 5.2, 1.05),
    "rurudo": ("cel", 5.0, 1.15), "sakimichan": ("volume", 5.0, 1.20),
    "shimada_fumikane": ("cel", 5.2, 1.15), "so-bin": ("paint", 5.5, 1.20),
    "tamaki": ("cel", 5.2, 1.0), "tokkyu": ("soft", 5.2, 1.15),
    "wanke": ("cel", 5.2, 1.10), "wlop": ("volume", 4.8, 1.20),
    "yoneyama_mai": ("cel", 4.8, 1.0),
}
_REFERENCE_EXTRA_NEGATIVES: Dict[str, Tuple[str, ...]] = {
    key: ("sepia", "orange theme") for key in (
        "rella", "wanke", "ask", "krenz", "modare", "ciloranko", "konya_karasue", "kuroboshi_kouhaku", "loundraw"
    )
}
_REFERENCE_EXTRA_NEGATIVES["yoneyama_mai"] = ("oily skin", "plastic skin", "metallic skin", "oversaturated")
_REFERENCE_EXTRA_NEGATIVES["mika_pikazo"] = ("bold outlines", "high contrast", "harsh shadows", "oversaturated")


def _calibrated_artist(preset: StylePreset) -> StylePreset:
    key = preset.key.removeprefix("artist_")
    if key not in _ARTIST_CALIBRATION:
        return preset  # 特别保留 torino 已确认的整套配方。
    family, cfg, artist_weight = _ARTIST_CALIBRATION[key]
    artist = preset.tags[0]
    if "::artist:" in artist:
        artist = re.sub(r"^\d+(?:\.\d+)?::", f"{artist_weight:g}::", artist)
    # as109、tamaki 的原档明确避免给名称套数字权重，保留该约束。
    return replace(
        preset, tags=(artist, *V5_REFERENCE_RENDER[key]),
        extra_negatives=(*_RENDER_FAMILY_NEGATIVES[family], *_REFERENCE_EXTRA_NEGATIVES.get(key, ())), drop_negatives=HARD_EDGE_NEGATIVES,
        generation=GenerationTuning(negative_tags=(), scale=5.5, steps=28, sampler="k_euler_ancestral", style_weight=1.0),
        generation_v5=GenerationTuning(negative_tags=(), scale=cfg, steps=28, sampler="k_euler_ancestral", style_weight=1.0),
    )


PRESETS = tuple(_calibrated_artist(preset) for preset in PRESETS)
# V5 按自己的参考图锁定静态配方，不能套用后来为 4.5 校准的另一套画法。
# 快照只包含画风、负面与采样参数，排除了样图人物/服装/构图/seed；生成阶段不读 PNG。
_V5_RECIPES = json.loads((ASSET_DIR.parent / "style_recipes_v5.json").read_text(encoding="utf-8"))["recipes"]
PRESETS = tuple(replace(preset,
    tags_v5=tuple(_V5_RECIPES[preset.key]["tags"]),
    extra_negatives_v5=(), drop_negatives_v5=tuple(_V5_RECIPES[preset.key]["drop_negatives"]),
    generation_v5=GenerationTuning(
        negative_tags=tuple(_V5_RECIPES[preset.key]["negative_tags"]),
        scale=_V5_RECIPES[preset.key]["scale"], steps=_V5_RECIPES[preset.key]["steps"],
        sampler=_V5_RECIPES[preset.key]["sampler"], style_weight=_V5_RECIPES[preset.key]["style_weight"]))
    if preset.key in _V5_RECIPES else preset for preset in PRESETS)
# 默认档（柔光）不出现在这张表里：它走 [style] default_tags，不需要参考图。
# 但「查看画风」要把它一起列出来，所以单独留一张预览图。
DEFAULT_LABEL = "默认柔光"
DEFAULT_PREVIEW = "default.png"


def preview_entries(v5: bool = False) -> List[Tuple[str, Path]]:
    """查看画风用的清单：默认档在前，其余按表内顺序，各带一张参考图。

    ``v5=True`` 取 5 Full 那套（v5/ 子目录），用来并排看两个模型的观感差异。
    """
    root = ASSET_DIR / V5_DIR if v5 else ASSET_DIR
    entries = [(DEFAULT_LABEL, root / DEFAULT_PREVIEW)]
    entries.extend((preset.label, preset.reference_v5_path if v5 else preset.reference_path) for preset in PRESETS)
    return entries


def preview_groups(v5: bool = False) -> Tuple[List[Tuple[str, Path]], List[Tuple[str, Path]]]:
    """拆成（媒介档, 画师档）。

    画师档有 39 款，全平铺会把一条转发撑得很长，所以查看画风把它们收进嵌套转发。
    """
    entries = preview_entries(v5)
    media = [item for item in entries if not item[1].stem.startswith("artist_")]
    artists = [item for item in entries if item[1].stem.startswith("artist_")]
    return media, artists


def _keyword_hits(text: str, keyword: str) -> bool:
    """画师名与英文风格词按整词匹配，中文按键位包含匹配。

    画师名很多是常见英文片段（hiten 落在 whiten 里、rella 落在 corella 里），
    纯子串匹配会把它们误当成点名；加词边界后只在独立出现时才算。
    边界只按 ASCII 字母数字算：Python 的 ``\\w`` 把汉字也算作单词字符，
    于是 ``rella画风`` 这种紧贴中文的写法匹配不上，整档画风被静默丢掉。
    """
    if keyword.isascii():
        return re.search(r"(?<![A-Za-z0-9_])" + re.escape(keyword) + r"(?![A-Za-z0-9_])", text, re.I) is not None
    return keyword.casefold() in text.casefold()


def _keyword_variants(keyword: str) -> Tuple[str, ...]:
    """画师名的模糊写法。用户不会记得 Danbooru 的确切拼法，这几种都认：

    - 原样：``mocha (cotton)``
    - 分隔符互换：``mocha_cotton`` / ``mocha-cotton``
    - 去掉括号后缀：``mocha``（Danbooru 常把画师名写成 ``xxx_(yyy)``）
    - 连写：``misakikurehito``

    变体仍然走整词匹配，所以 ``whiten`` 里的 ``hiten`` 不会被认出来——
    放宽的只是分隔符和后缀，不是子串边界。
    """
    base = keyword.strip()
    forms = [base]
    spaced = re.sub(r"[_\-]+", " ", base)
    forms.append(spaced)
    forms.append(re.sub(r"\s+", "_", spaced))
    stripped = re.sub(r"\s*[（(][^)）]*[)）]", "", spaced).strip()
    if stripped and stripped != spaced:
        forms.append(stripped)
    joined = re.sub(r"[^0-9A-Za-z]", "", base)
    if joined:
        forms.append(joined)
    seen, ordered = set(), []
    for form in forms:
        key = form.casefold()
        # 长度下限只对英文生效：两字中文（「厚涂」「素股」）本身就有意义，
        # 拿它去卡会把中文关键词整条滤掉。
        if (not form.isascii() or len(form) >= 3) and key not in seen:
            seen.add(key)
            ordered.append(form)
    return tuple(ordered)


def match_preset(text: str) -> Optional[StylePreset]:
    """取命中的档次；没有点名任何一档时返回 None。

    命中多个时取**关键词最长**的那个，而不是表内靠前的：「锐利线稿」同时含
    「锐利线稿」和素描的「线稿」，按表内顺序会让素描截走它。同长时按表内顺序，
    保证结果确定。
    """
    lowered = (text or "").strip()
    if not lowered:
        return None
    best: Optional[Tuple[int, int, StylePreset]] = None
    for order, preset in enumerate(PRESETS):
        for keyword in preset.keywords:
            matched = next((form for form in _keyword_variants(keyword) if _keyword_hits(lowered, form)), None)
            if matched is not None:
                candidate = (len(matched), -order, preset)
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
                break
    return best[2] if best else None


def strip_preset_keywords(text: str, preset: StylePreset) -> str:
    """移除已消费的画风关键词，避免角色匹配或翻译把关键词拆成角色别名。"""
    result = text
    for keyword in preset.keywords:
        for form in _keyword_variants(keyword):
            if not _keyword_hits(result, form):
                continue
            if form.isascii():
                # 连带吃掉紧跟的“画风/风格”：否则剩下的通用词会被翻译成 style 标签。
                result = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(form) + r"(?![A-Za-z0-9_])\s*(?:画风|风格)?",
                                " ", result, flags=re.I)
            else:
                result = re.sub(re.escape(form), " ", result, flags=re.I)
    return result


def preset_names() -> List[str]:
    return [preset.key for preset in PRESETS]


def artist_presets() -> List[StylePreset]:
    return [preset for preset in PRESETS if preset.key.startswith("artist_")]


def match_artist(text: str) -> Optional[StylePreset]:
    """按画师名找档。

    先走正常的关键词命中（且必须落在画师档），再退到包含匹配——用户往往只写一半
    （「torino」「mika」），严格整词匹配认不出来。
    """
    lowered = (text or "").strip()
    if not lowered:
        return None
    found = match_preset(lowered)
    if found is not None and found.key.startswith("artist_"):
        return found
    normalized = re.sub(r"[\s_\-]+", "", lowered).casefold()
    if len(normalized) < 3:  # 一两个字会命中一大片，不如让用户写清楚
        return None
    best: Optional[Tuple[Tuple[int, int], StylePreset]] = None
    for preset in artist_presets():
        for keyword in preset.keywords:
            candidate = re.sub(r"[\s_\-]+", "", keyword).casefold()
            if not candidate:
                continue
            if normalized in candidate:
                # 越靠前、越短 = 越贴身。按候选串最长排会让「mika」命中 fumikane
                # （shimada fuMIKAne），所以先看匹配位置再看长度。
                score = (candidate.find(normalized), len(candidate))
            elif candidate in normalized:
                score = (0, 0)
            else:
                continue
            if best is None or score < best[0]:
                best = (score, preset)
    return best[1] if best else None


def apply_preset_negatives(existing: Sequence[str], preset: StylePreset, *, v5: bool = False) -> List[str]:
    """摘掉与这档画风冲突的负面，再追加这档自己的。

    传进来的应当是合并后的整条负面串（含 [generation] default_negative_prompt），
    冲突项在那里也有一份，只清风格那半条等于没清。
    """
    drops = preset.drop_negatives_v5 if v5 and preset.drop_negatives_v5 is not None else preset.drop_negatives
    extras = preset.extra_negatives_v5 if v5 and preset.extra_negatives_v5 is not None else preset.extra_negatives
    dropped = {tag.lower() for tag in drops}
    kept = [tag for tag in existing if tag.lower() not in dropped]
    seen = {tag.lower() for tag in kept}
    return kept + [tag for tag in extras if tag.lower() not in seen]
