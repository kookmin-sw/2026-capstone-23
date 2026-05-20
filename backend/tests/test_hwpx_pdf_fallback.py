import base64
import zipfile

import fitz

from core.converters import _extract_hwpx_pdf_elements, convert_hwpx_to_pdf_via_elements, parse_hwpx_document


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def test_convert_hwpx_to_pdf_via_elements_keeps_images(tmp_path):
    hwpx_path = tmp_path / "sample.hwpx"
    with zipfile.ZipFile(hwpx_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Contents/content.hpf",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<opf:package xmlns:opf="http://www.idpf.org/2007/opf">'
                "<opf:manifest>"
                '<opf:item id="image1" href="BinData/image1.png" media-type="image/png" isEmbeded="1"/>'
                "</opf:manifest>"
                "</opf:package>"
            ),
        )
        archive.writestr(
            "Contents/section0.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
                'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" '
                'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core">'
                '<hp:p><hp:run><hp:t>sample text</hp:t></hp:run></hp:p>'
                '<hp:p><hp:run><hp:pic><hc:img binaryItemIDRef="image1"/></hp:pic></hp:run></hp:p>'
                "</hs:sec>"
            ),
        )
        archive.writestr("BinData/image1.png", PNG_1X1)

    pdf_path = convert_hwpx_to_pdf_via_elements(hwpx_path, tmp_path)

    doc = fitz.open(pdf_path)
    assert "sample text" in "\n".join(page.get_text() for page in doc)
    assert sum(len(page.get_images(full=True)) for page in doc) == 1


def test_convert_hwpx_to_pdf_via_elements_uses_numeric_section_order(tmp_path):
    hwpx_path = tmp_path / "sample.hwpx"
    with zipfile.ZipFile(hwpx_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Contents/section10.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
                'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
                '<hp:p><hp:run><hp:t>section ten</hp:t></hp:run></hp:p>'
                "</hs:sec>"
            ),
        )
        archive.writestr(
            "Contents/section2.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
                'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
                '<hp:p><hp:run><hp:t>section two</hp:t></hp:run></hp:p>'
                "</hs:sec>"
            ),
        )

    pdf_path = convert_hwpx_to_pdf_via_elements(hwpx_path, tmp_path)

    doc = fitz.open(pdf_path)
    text = "\n".join(page.get_text() for page in doc)
    assert text.index("section two") < text.index("section ten")


def test_extract_hwpx_pdf_elements_preserves_inline_picture_order(tmp_path):
    hwpx_path = tmp_path / "sample.hwpx"
    with zipfile.ZipFile(hwpx_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Contents/content.hpf",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<opf:package xmlns:opf="http://www.idpf.org/2007/opf">'
                "<opf:manifest>"
                '<opf:item id="image1" href="BinData/image1.png" media-type="image/png" isEmbeded="1"/>'
                "</opf:manifest>"
                "</opf:package>"
            ),
        )
        archive.writestr(
            "Contents/section0.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
                'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" '
                'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core">'
                "<hp:p><hp:run>"
                "<hp:t>before image</hp:t>"
                '<hp:pic><hc:img binaryItemIDRef="image1"/></hp:pic>'
                "<hp:t>after image</hp:t>"
                "</hp:run></hp:p>"
                "</hs:sec>"
            ),
        )
        archive.writestr("BinData/image1.png", PNG_1X1)

    elements = _extract_hwpx_pdf_elements(hwpx_path)

    assert [element["type"] for element in elements] == ["text", "image", "text"]
    assert elements[0]["text"] == "before image"
    assert elements[2]["text"] == "after image"


def test_parse_hwpx_document_extracts_text_and_tables_without_vlm(tmp_path):
    hwpx_path = tmp_path / "sample.hwpx"
    with zipfile.ZipFile(hwpx_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Contents/section0.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
                'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
                '<hp:p><hp:run><hp:t>표 앞 문단</hp:t></hp:run></hp:p>'
                '<hp:p><hp:tbl>'
                '<hp:tr>'
                '<hp:tc><hp:subList><hp:p><hp:run><hp:t>모델</hp:t></hp:run></hp:p></hp:subList></hp:tc>'
                '<hp:tc><hp:subList><hp:p><hp:run><hp:t>값</hp:t></hp:run></hp:p></hp:subList></hp:tc>'
                "</hp:tr>"
                '<hp:tr>'
                '<hp:tc><hp:subList><hp:p><hp:run><hp:t>GPT</hp:t></hp:run></hp:p></hp:subList></hp:tc>'
                '<hp:tc><hp:subList><hp:p><hp:run><hp:t>1</hp:t></hp:run></hp:p></hp:subList></hp:tc>'
                "</hp:tr>"
                "</hp:tbl></hp:p>"
                '<hp:p><hp:run><hp:t>표 뒤 문단</hp:t></hp:run></hp:p>'
                "</hs:sec>"
            ),
        )

    parsed = parse_hwpx_document(hwpx_path)
    elements = parsed["pages"][0]["elements"]

    assert parsed["page_count"] == 1
    assert [element["type"] for element in elements] == ["text", "table", "text"]
    assert elements[0]["content"] == "표 앞 문단"
    assert "<table>" in elements[1]["content"]
    assert "<td><p>모델</p></td>" in elements[1]["content"]
    assert "| 모델 | 값 |" in elements[1]["markdown"]
    assert elements[2]["content"] == "표 뒤 문단"


def test_parse_hwpx_document_extracts_embedded_images_in_order(tmp_path):
    hwpx_path = tmp_path / "sample.hwpx"
    with zipfile.ZipFile(hwpx_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Contents/content.hpf",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<opf:package xmlns:opf="http://www.idpf.org/2007/opf">'
                "<opf:manifest>"
                '<opf:item id="image1" href="BinData/image1.png" media-type="image/png" isEmbeded="1"/>'
                "</opf:manifest>"
                "</opf:package>"
            ),
        )
        archive.writestr(
            "Contents/section0.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
                'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" '
                'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core">'
                "<hp:p>"
                "<hp:run><hp:t>before image</hp:t></hp:run>"
                '<hp:pic><hc:img binaryItemIDRef="image1"/></hp:pic>'
                "<hp:run><hp:t>after image</hp:t></hp:run>"
                "</hp:p>"
                "</hs:sec>"
            ),
        )
        archive.writestr("BinData/image1.png", PNG_1X1)

    parsed = parse_hwpx_document(hwpx_path)
    page = parsed["pages"][0]

    assert [element["type"] for element in page["elements"]] == ["text", "image", "text"]
    assert page["elements"][1]["image_id"] == "hwpx-img-1"
    assert page["images"][0]["image_id"] == "hwpx-img-1"
    assert page["images"][0]["bytes"] == PNG_1X1


def test_parse_hwpx_document_preserves_nested_tables_inside_parent_cell(tmp_path):
    hwpx_path = tmp_path / "nested.hwpx"
    with zipfile.ZipFile(hwpx_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Contents/section0.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
                'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
                '<hp:tbl>'
                '<hp:tr><hp:tc><hp:subList>'
                '<hp:p><hp:run><hp:t>바깥 셀</hp:t>'
                '<hp:tbl>'
                '<hp:tr><hp:tc><hp:subList><hp:p><hp:run><hp:t>안쪽 A</hp:t></hp:run></hp:p></hp:subList></hp:tc>'
                '<hp:tc><hp:subList><hp:p><hp:run><hp:t>안쪽 B</hp:t></hp:run></hp:p></hp:subList></hp:tc></hp:tr>'
                '</hp:tbl>'
                '</hp:run>'
                '</hp:p>'
                '</hp:subList></hp:tc></hp:tr>'
                '</hp:tbl>'
                "</hs:sec>"
            ),
        )

    parsed = parse_hwpx_document(hwpx_path)
    elements = parsed["pages"][0]["elements"]
    table_html = elements[0]["content"]

    assert [element["type"] for element in elements] == ["table"]
    assert table_html.count("<table>") == 2
    assert "<p>바깥 셀</p>" in table_html
    assert "<td><p>안쪽 A</p></td>" in table_html
    assert "<td><p>안쪽 B</p></td>" in table_html
