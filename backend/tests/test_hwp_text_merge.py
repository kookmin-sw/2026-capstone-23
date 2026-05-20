import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from core.pipeline import (
    merge_hwp_source_text_with_structured_blocks,
    split_hwp_source_text_into_pages,
)


def test_split_hwp_source_text_into_pages_uses_form_feed_boundaries() -> None:
    source_text = "첫째 페이지\n\f둘째 페이지\n\f셋째 페이지"

    pages = split_hwp_source_text_into_pages(source_text, 3)

    assert pages == ["첫째 페이지", "둘째 페이지", "셋째 페이지"]


def test_merge_hwp_source_text_prefers_source_text_when_generated_text_is_garbled() -> None:
    source_text = "폐기물 관리 절차서 개정대비표\n\n<표>"
    generated_text = "■■■ ■■■ (■■■■■■■■■■) ■■■"

    merged = merge_hwp_source_text_with_structured_blocks(source_text, generated_text)

    assert merged == source_text


def test_merge_hwp_source_text_reinserts_structured_blocks_into_markers() -> None:
    source_text = "개요\n\n<그림>\n\n설명\n\n<표>\n\n마무리"
    generated_text = """
[[IMAGE]]
이미지 설명
[[/IMAGE]]

[[TABLE]]
<table><tr><td>A</td></tr></table>
[[/TABLE]]
""".strip()

    merged = merge_hwp_source_text_with_structured_blocks(source_text, generated_text)

    assert "<그림>" not in merged
    assert "<표>" not in merged
    assert merged.index("[[IMAGE]]") < merged.index("설명")
    assert merged.index("[[TABLE]]") > merged.index("설명")
    assert "개요" in merged
    assert "마무리" in merged


def test_merge_hwp_source_text_appends_extra_blocks_without_matching_markers() -> None:
    source_text = "본문만 있는 페이지"
    generated_text = """
[[TABLE]]
<table><tr><td>A</td></tr></table>
[[/TABLE]]
""".strip()

    merged = merge_hwp_source_text_with_structured_blocks(source_text, generated_text)

    assert merged.startswith("본문만 있는 페이지")
    assert "[[TABLE]]" in merged
