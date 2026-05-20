from pathlib import Path

import fitz
import pytest

from core.output import build_document_text, _extract_first_balanced_table
from core.pdf_extractor import (
    extract_pdf,
    _html_column_count,
    _is_table_crop_covered_by_native_tables,
    _table_cells_to_html,
)


def _require_pdf(relative_path: str) -> Path:
    pdf_path = Path(__file__).resolve().parents[1] / relative_path
    if not pdf_path.exists():
        pytest.skip(f"missing PDF fixture: {relative_path}")
    return pdf_path


def _write_single_column_container_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    outer_x0, outer_x1 = 82, 566
    outer_y = [96, 273, 564]
    for y in outer_y:
        page.draw_line((outer_x0, y), (outer_x1, y), width=0.8)
    page.draw_line((outer_x0, outer_y[0]), (outer_x0, outer_y[-1]), width=0.8)
    page.draw_line((outer_x1, outer_y[0]), (outer_x1, outer_y[-1]), width=0.8)

    page.insert_text((110, 130), "Disaster notice", fontsize=11)
    page.insert_text((110, 155), "Traffic is restricted for resident safety.", fontsize=10)
    page.insert_text((110, 310), "Bus route changes", fontsize=10)

    inner_x = [128, 275, 410, 520]
    inner_y = [365, 381, 394, 409, 424, 439, 454]
    for x in inner_x:
        page.draw_line((x, inner_y[0]), (x, inner_y[-1]), width=0.8)
    for y in inner_y:
        page.draw_line((inner_x[0], y), (inner_x[-1], y), width=0.8)
    page.insert_text((155, 377), "Route No.", fontsize=8)
    page.insert_text((310, 377), "Current route", fontsize=8)
    page.insert_text((435, 377), "Changed route", fontsize=8)

    page.insert_text((110, 505), "Please check the changed service.", fontsize=10)
    doc.save(path)
    doc.close()


def test_extract_pdf_restores_single_column_container_table_without_vlm(tmp_path: Path):
    pdf_path = tmp_path / "container.pdf"
    _write_single_column_container_pdf(pdf_path)

    parsed = extract_pdf(pdf_path)
    content = build_document_text(
        source_path=pdf_path,
        page_count=parsed["page_count"],
        pages=parsed["pages"],
        image_results={},
    )

    assert sum(len(page.get("images", [])) for page in parsed["pages"]) == 0
    assert content.count("[[TABLE]]") == 1
    assert content.lower().count("<table") == 2
    assert "Disaster notice" in content
    assert "Bus route changes" in content
    assert "Route No." in content
    assert "Please check the changed service." in content

    table_elements = [
        element
        for page in parsed["pages"]
        for element in page.get("elements", [])
        if element.get("type") == "native_table"
    ]
    assert len(table_elements) == 1
    assert table_elements[0].get("is_container_table") is True


def test_balanced_table_extraction_keeps_nested_table_closed():
    vlm_text = """
    분석 결과:
    <table>
      <tr>
        <td>놀이터A<table><tr><td>내부 복잡표</td></tr></table></td>
      </tr>
      <tr><td>놀이터B</td></tr>
    </table>
    뒤쪽 텍스트
    """

    table_html = _extract_first_balanced_table(vlm_text)

    assert "놀이터A" in table_html
    assert "놀이터B" in table_html
    assert table_html.lower().count("<table") == table_html.lower().count("</table>")


def test_vlm_nested_table_output_does_not_leak_into_next_content():
    nested_table = """
    <table>
      <tr>
        <td>놀이터A<table><tr><td>내부 복잡표</td></tr></table></td>
      </tr>
      <tr><td>놀이터B</td></tr>
    </table>
    """
    content = build_document_text(
        source_path=Path("made7.pdf"),
        page_count=1,
        pages=[{
            "page": 1,
            "text": "다음 문단",
            "images": [],
            "elements": [
                {"type": "image", "image_id": "page-1-img-1", "bbox": [0, 0, 100, 100]},
                {"type": "text", "content": "다음 문단", "bbox": [0, 110, 100, 130]},
            ],
        }],
        image_results={"page-1-img-1": {"text": nested_table}},
    )

    table_block = content.split("[[TABLE]]", 1)[1].split("[[/TABLE]]", 1)[0]
    assert "놀이터B" in table_block
    assert table_block.lower().count("<table") == table_block.lower().count("</table>")
    assert content.index("[[/TABLE]]") < content.index("다음 문단")


def test_table_crop_covered_by_multiple_native_tables_is_duplicate():
    crop = [40.9, 45.9, 553.6, 440.6]
    native_bboxes = [
        [46.0, 51.7, 549.2, 156.0],
        [46.0, 170.2, 549.2, 295.3],
        [46.0, 309.5, 549.2, 434.7],
    ]

    assert _is_table_crop_covered_by_native_tables(crop, native_bboxes)


def test_native_table_html_escapes_cell_text():
    class FakeTab:
        cells = [(0, 0, 100, 30)]

        def extract(self):
            return [["A & B < C"]]

    html = _table_cells_to_html(FakeTab())

    assert "<p>A &amp; B &lt; C</p>" in html
    assert "A & B < C" not in html


def test_made3_page1_keeps_three_native_tables_without_duplicate_crop():
    pdf_path = _require_pdf("eval/data/input/made_complex/made3.pdf")
    parsed = extract_pdf(pdf_path)
    page1_elements = parsed["pages"][0]["elements"]

    native_tables = [e for e in page1_elements if e.get("type") == "native_table"]
    table_crops = [
        e
        for e in page1_elements
        if e.get("type") == "image" and e.get("is_text_table")
    ]

    assert len(native_tables) == 3
    assert table_crops == []
    assert [table["html"].count("<tr>") for table in native_tables] == [5, 6, 6]

    first_table = native_tables[0]["html"]
    second_table = native_tables[1]["html"]
    assert '<th rowspan="2"><p>비고</p></th>' in first_table
    assert "<td><p>2분기</p></td>" in first_table
    assert '<th rowspan="2"><p>승인</p></th>' in second_table
    assert "<td><p>보고</p></td>" in second_table
    assert "<td><p>월간</p></td>" in second_table


def test_made3_last_multipage_table_keeps_continuation_rows():
    pdf_path = _require_pdf("eval/data/input/made_complex/made3.pdf")
    parsed = extract_pdf(pdf_path)

    page6_tables = [
        e for e in parsed["pages"][5]["elements"] if e.get("type") == "native_table"
    ]
    page7_tables = [
        e for e in parsed["pages"][6]["elements"] if e.get("type") == "native_table"
    ]

    assert len(page6_tables) == 1
    assert page7_tables == []

    html = page6_tables[0]["html"]
    assert html.count("<tr>") == 17
    assert '<td rowspan="5"><p>운영</p></td>' in html
    assert "개선조치 11." in html
    assert "개선조치 12." in html
    assert "개선조치 13." in html
    assert "개선조치 14." in html
    assert "개선조치 15." in html


def test_made4_multipage_native_table_is_merged():
    pdf_path = _require_pdf("eval/data/input/made_complex/made4.pdf")
    parsed = extract_pdf(pdf_path)

    page1_tables = [
        e for e in parsed["pages"][0]["elements"] if e.get("type") == "native_table"
    ]
    page2_tables = [
        e for e in parsed["pages"][1]["elements"] if e.get("type") == "native_table"
    ]

    assert len(page1_tables) == 1
    assert page1_tables[0]["html"].count("<tr>") >= 24
    diagnostics = page1_tables[0].get("_grid_diagnostics", {})
    assert diagnostics.get("cols") == 5
    assert diagnostics.get("incomplete_rows") == []
    assert diagnostics.get("overflowing_rowspans") == []
    assert _html_column_count(page1_tables[0]["html"]) == 5
    assert '<th colspan="2"><p>실 무 반</p></th>' in page1_tables[0]["html"]
    assert '<td colspan="2"><p>전문자문단</p></td>' in page1_tables[0]["html"]
    assert '<td colspan="2"><p>구조구급</p>\n    <p>지원반</p></td>' in page1_tables[0]["html"]
    assert "<p>수색·구조·구급</p>" in page1_tables[0]["html"]
    assert page2_tables == []


def test_made5_repeated_header_continuation_keeps_data_rows_separate():
    pdf_path = _require_pdf("eval/data/input/made_complex/made5.pdf")
    parsed = extract_pdf(pdf_path)

    page1_tables = [
        e for e in parsed["pages"][0]["elements"] if e.get("type") == "native_table"
    ]
    page2_tables = [
        e for e in parsed["pages"][1]["elements"] if e.get("type") == "native_table"
    ]

    assert len(page1_tables) == 1
    assert len(page2_tables) == 1
    assert parsed["pages"][1].get("merged_table_bboxes")

    html = page1_tables[0]["html"]
    assert html.count("<tr>") == 14
    assert "<td><p>10</p></td>" in html
    assert "<td><p>코리아헤럴드</p></td>" in html
    assert "<td><p>11</p></td>" in html
    assert "<td><p>매일신문</p></td>" in html
    assert "<td><p>12</p></td>" in html
    assert "<td><p>영남일보</p></td>" in html
    assert '<td colspan="4"><p>합 계</p></td>' in html
    assert '<td rowspan="4"><p>10</p>' not in html


def test_made9_embedded_table_cell_images_are_appended_to_table_cells():
    pdf_path = _require_pdf("eval/data/input/made_complex/made9.pdf")
    parsed = extract_pdf(pdf_path)
    page1_elements = parsed["pages"][0]["elements"]

    table_elements = [e for e in page1_elements if e.get("type") == "native_table"]
    embedded_images = [
        e
        for e in page1_elements
        if e.get("type") == "image" and e.get("is_embedded_in_table_cell")
    ]

    assert len(table_elements) == 1
    assert len(embedded_images) == 3
    assert {img["embedded_table_cell"]["table_image_id"] for img in embedded_images} == {
        table_elements[0]["image_id"]
    }

    image_results = {
        img["image_id"]: {"text": "## 요약\n셀 내부 차트 설명"}
        for img in embedded_images
    }
    content = build_document_text(
        source_path=pdf_path,
        page_count=parsed["page_count"],
        pages=parsed["pages"],
        image_results=image_results,
    )

    assert content.count("[[TABLE]]") == 1
    assert "[[IMAGE]]" not in content
    assert content.count("[차트 설명]") == 3
    assert content.count("셀 내부 차트 설명") == 3


def test_made7_inner_native_tables_are_not_emitted_standalone():
    pdf_path = _require_pdf("eval/data/input/made_complex/made7.pdf")
    parsed = extract_pdf(pdf_path)

    page1_tables = [
        e for e in parsed["pages"][0]["elements"] if e.get("type") == "native_table"
    ]
    page2_tables = [
        e for e in parsed["pages"][1]["elements"] if e.get("type") == "native_table"
    ]

    assert len(page1_tables) == 1
    assert len(page2_tables) == 1
    assert "A 안전점검" in page1_tables[0]["html"]
    assert "B 세부 점검" in page1_tables[0]["html"]
    assert "M01 안전점검" in page2_tables[0]["html"]
    assert "M10 세부 점검" in page2_tables[0]["html"]


def test_made8_vector_charts_and_flows_are_appended_to_table_cells():
    pdf_path = _require_pdf("eval/data/input/made_complex/made8.pdf")
    parsed = extract_pdf(pdf_path)
    embedded_images = [
        e
        for page in parsed["pages"]
        for e in page["elements"]
        if e.get("type") == "image" and e.get("is_embedded_in_table_cell")
    ]

    assert len(embedded_images) == 12
    assert {
        img["embedded_table_cell"]["table_image_id"]
        for img in embedded_images
    } == {"page-1-native-table-1", "page-2-native-table-1"}

    image_results = {
        img["image_id"]: {"text": "## 요약\n셀 내부 벡터 설명"}
        for img in embedded_images
    }
    content = build_document_text(
        source_path=pdf_path,
        page_count=parsed["page_count"],
        pages=parsed["pages"],
        image_results=image_results,
    )

    assert content.count("[[TABLE]]") == 2
    assert "[[IMAGE]]" not in content
    assert content.count("셀 내부 벡터 설명") == 12


def test_stale_embedded_table_reference_falls_back_to_image_block():
    content = build_document_text(
        source_path=Path("stale.pdf"),
        page_count=1,
        pages=[{
            "page": 1,
            "text": "",
            "images": [],
            "elements": [{
                "type": "image",
                "image_id": "page-1-cell-img",
                "bbox": [10, 10, 100, 100],
                "is_embedded_in_table_cell": True,
                "embedded_table_cell": {
                    "table_image_id": "removed-native-table",
                    "row": 0,
                    "col": 0,
                },
            }],
        }],
        image_results={"page-1-cell-img": {"text": "## 요약\n유실되면 안 되는 셀 이미지 설명"}},
    )

    assert "[[IMAGE]]" in content
    assert "유실되면 안 되는 셀 이미지 설명" in content
