import base64
import zipfile

from core.documents.hwpx_preview import render_hwpx_preview_html


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def test_hwpx_preview_restores_basic_layout_styles_and_images(tmp_path):
    hwpx_path = tmp_path / "styled.hwpx"
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
            "Contents/header.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<hh:head xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head" '
                'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core">'
                '<hh:borderFill id="1"><hh:leftBorder type="SOLID" width="0.4 mm" color="#000000"/>'
                '<hh:rightBorder type="SOLID" width="0.4 mm" color="#000000"/>'
                '<hh:topBorder type="SOLID" width="0.4 mm" color="#000000"/>'
                '<hh:bottomBorder type="SOLID" width="0.4 mm" color="#000000"/>'
                '<hc:fillBrush><hc:winBrush faceColor="#99CCFF"/></hc:fillBrush></hh:borderFill>'
                '<hh:charPr id="1" height="1200" textColor="#112233"><hh:bold/></hh:charPr>'
                '<hh:paraPr id="1"><hh:align horizontal="CENTER"/><hh:lineSpacing value="120"/></hh:paraPr>'
                "</hh:head>"
            ),
        )
        archive.writestr(
            "Contents/section0.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
                'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" '
                'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core">'
                '<hp:p paraPrIDRef="1"><hp:run charPrIDRef="1"><hp:t>가운데 문단</hp:t></hp:run></hp:p>'
                '<hp:tbl><hp:sz width="14400" height="7200"/>'
                '<hp:tr><hp:tc borderFillIDRef="1" header="1">'
                '<hp:subList vertAlign="CENTER"><hp:p paraPrIDRef="1"><hp:run charPrIDRef="1"><hp:t>헤더</hp:t></hp:run></hp:p></hp:subList>'
                '<hp:cellAddr colAddr="0" rowAddr="0"/><hp:cellSpan colSpan="2" rowSpan="1"/>'
                '<hp:cellSz width="14400" height="1200"/><hp:cellMargin left="100" right="100" top="100" bottom="100"/></hp:tc></hp:tr>'
                "</hp:tbl>"
                '<hp:p><hp:run><hp:pic><hc:img binaryItemIDRef="image1"/></hp:pic></hp:run></hp:p>'
                "</hs:sec>"
            ),
        )
        archive.writestr("BinData/image1.png", PNG_1X1)

    html = render_hwpx_preview_html(hwpx_path.read_bytes(), filename=hwpx_path.name)

    assert 'colspan="2"' in html
    assert "background-color:#99CCFF" in html
    assert "text-align:center" in html
    assert "font-size:12.00pt" in html
    assert "font-weight:700" in html
    assert "data:image/png;base64" in html


def test_hwpx_preview_can_render_embedded_image_vlm_result(tmp_path):
    hwpx_path = tmp_path / "image.hwpx"
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
                '<hp:p><hp:run><hp:pic><hc:img binaryItemIDRef="image1"/></hp:pic></hp:run></hp:p>'
                "</hs:sec>"
            ),
        )
        archive.writestr("BinData/image1.png", PNG_1X1)

    calls = []

    def describe_image(image_bytes, mime_type, image_ref):
        calls.append((image_bytes, mime_type, image_ref))
        return "<table><tr><td>이미지 표</td></tr></table>\n설명 텍스트"

    html = render_hwpx_preview_html(
        hwpx_path.read_bytes(),
        filename=hwpx_path.name,
        describe_image=describe_image,
    )

    assert calls == [(PNG_1X1, "image/png", "image1")]
    assert "data:image/png;base64" not in html
    assert '<div class="image-vlm">' in html
    assert "<td>이미지 표</td>" in html
    assert "설명 텍스트" in html


def test_hwpx_preview_preserves_nested_tables_inside_parent_cell(tmp_path):
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

    html = render_hwpx_preview_html(hwpx_path.read_bytes(), filename=hwpx_path.name)

    assert html.count("<table") == 2
    assert "바깥 셀" in html
    assert "안쪽 A" in html
    assert "안쪽 B" in html


def test_hwpx_preview_does_not_duplicate_top_level_table_inside_paragraph(tmp_path):
    hwpx_path = tmp_path / "paragraph-table.hwpx"
    with zipfile.ZipFile(hwpx_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Contents/section0.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
                'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
                '<hp:p><hp:run>'
                '<hp:tbl>'
                '<hp:tr><hp:tc><hp:subList><hp:p><hp:run><hp:t>단일 표</hp:t></hp:run></hp:p></hp:subList></hp:tc></hp:tr>'
                '</hp:tbl>'
                '</hp:run></hp:p>'
                "</hs:sec>"
            ),
        )

    html = render_hwpx_preview_html(hwpx_path.read_bytes(), filename=hwpx_path.name)

    assert html.count("<table") == 1
    assert "단일 표" in html


def test_hwpx_preview_wraps_wide_landscape_tables(tmp_path):
    hwpx_path = tmp_path / "landscape.hwpx"
    with zipfile.ZipFile(hwpx_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Contents/section0.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
                'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
                '<hp:secPr><hp:pagePr landscape="NARROWLY" width="84189" height="119055"/></hp:secPr>'
                '<hp:tbl><hp:sz width="111937" height="7200"/>'
                '<hp:tr><hp:tc><hp:subList><hp:p><hp:run><hp:t>넓은 표</hp:t></hp:run></hp:p></hp:subList></hp:tc></hp:tr>'
                '</hp:tbl>'
                "</hs:sec>"
            ),
        )

    html = render_hwpx_preview_html(hwpx_path.read_bytes(), filename=hwpx_path.name)

    assert "width: 1683px" in html
    assert "max-width: none" in html
    assert '<div class="table-wrap"><table' in html
    assert "overflow-x: auto" in html
