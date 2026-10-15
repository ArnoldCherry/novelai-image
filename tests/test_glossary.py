from novelai_image.glossary import apply_glossary, merge_tags


def test_substitutes_in_source_order():
    text, hits = apply_glossary("裸体少女，露出乳头")
    assert hits == ["nude", "1girl", "nipples"]
    assert "裸体" not in text and "少女" not in text and "乳头" not in text


def test_longest_term_wins():
    text, hits = apply_glossary("全裸")
    assert hits == ["completely nude"]
    assert "裸体" not in text


def test_unknown_text_untouched():
    text, hits = apply_glossary("低质量，模糊，多手多脚")
    assert text == "低质量，模糊，多手多脚"
    assert hits == []


def test_repeated_terms_deduplicated():
    _, hits = apply_glossary("裸体裸体")
    assert hits == ["nude"]


def test_ambiguous_term_maps_to_adult_tag():
    _, hits = apply_glossary("无修正露点")
    assert hits == ["uncensored", "exposed nipples"]


def test_merge_tags_deduplicates_case_insensitively():
    assert merge_tags(["nude", "Nipples"], ["1girl", "nude", "  "]) == ["nude", "Nipples", "1girl"]
