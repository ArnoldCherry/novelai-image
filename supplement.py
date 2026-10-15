"""索引补不出来的词条：角色缩写与著名事件。

全量抓取解决的是"标签库里有没有这个标签"，但这两类它解决不了：

- **角色缩写**（「五宿」）：粉丝造词，Danbooru 上没有，抓全量也不会有。
- **著名事件**（「新宿大战」）：Danbooru 只有单个标签，没有"事件"这个维度的标签。
  实测 `shinjuku_showdown`、`shibuya_incident`、`collapsed_building` 都不存在，
  能用的只有 `ruins` / `rubble` / `battle` 这类通用场景标签。

所以这张表是人工维护的，与索引无关。新增词条直接往两个字典里加即可。
"""

from typing import Dict, List, Tuple

# 每条映射写两栏：（角色, 场景）。
# 角色进 char_captions 逐个锚定身份，场景留在 base 提示词里——
# 把 ruins / battle 这类塞进 char_caption 会污染角色描述。
ABBREVIATIONS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    # 一个缩写对应多个角色：写全名是「五条悟和宿傩」，缩写是「五宿」。
    "五宿": (("gojo_satoru", "ryoumen_sukuna_(jujutsu_kaisen)"), ()),
}

# 事件展开成角色 + 作品 + 场景。Danbooru 没有事件标签，只能这样拼。
EVENTS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "新宿大战": (
        ("gojo_satoru", "ryoumen_sukuna_(jujutsu_kaisen)"),
        ("jujutsu_kaisen", "ruins", "rubble", "battle", "night"),
    ),
}


def supplement_tags(text: str) -> Tuple[List[str], List[str], List[str]]:
    """按表展开本轮描述里的人工词条。

    Returns:
        Tuple[List[str], List[str], List[str]]: 角色标签、场景标签，以及命中的词
        （调用方从文本里剔除，免得再被翻译模型当成普通中文翻一遍）。
    """
    if not text:
        return [], [], []
    characters: List[str] = []
    scene: List[str] = []
    hits: List[str] = []
    for table in (ABBREVIATIONS, EVENTS):
        for key, (mapped_characters, mapped_scene) in table.items():
            if key in text and key not in hits:
                hits.append(key)
                characters.extend(mapped_characters)
                scene.extend(mapped_scene)
    return list(dict.fromkeys(characters)), list(dict.fromkeys(scene)), hits
