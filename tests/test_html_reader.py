import json
import base64
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from PIL import Image
from pypdf import PdfWriter
import pytest

from mathpix_pipeline import html_reader


def write_pdf(path: Path, pages: int = 1, sizes: list[tuple[int, int]] | None = None) -> None:
    writer = PdfWriter()
    for width, height in sizes or [(600, 800)] * pages:
        writer.add_blank_page(width=width, height=height)
    with path.open("wb") as stream:
        writer.write(stream)


def write_lines(path: Path, pages: int = 1) -> None:
    value = {
        "pages": [
            {
                "page": page,
                "page_width": 1200,
                "page_height": 1600,
                "lines": [
                    {
                        "id": f"text-{page}",
                        "type": "text",
                        "text": "Sökbar text <säker>",
                        "region": {"top_left_x": 120, "top_left_y": 200, "width": 500, "height": 55},
                        "confidence": 0.99,
                    },
                    {
                        "id": f"formula-{page}",
                        "type": "math",
                        "text": "\\[x^2 + y^2 = 1\\]",
                        "region": {"top_left_x": 300, "top_left_y": 400, "width": 420, "height": 80},
                        "confidence": 1,
                    },
                ],
            }
            for page in range(1, pages + 1)
        ]
    }
    path.write_text(json.dumps(value), encoding="utf-8")


def fake_render(source: Path, pages_dir: Path, dpi: int, quality: int) -> list[Path]:
    pages_dir.mkdir(parents=True, exist_ok=True)
    rendered = []
    count = len(html_reader.PdfReader(source).pages)
    for page in range(1, count + 1):
        target = pages_dir / f"page-{page:04d}.jpg"
        Image.new("RGB", (900, 1200), "white").save(target, quality=quality)
        rendered.append(target)
    return rendered


def test_prepare_data_normalizes_regions_and_formula_copy(tmp_path):
    lines = tmp_path / "lines.json"
    write_lines(lines)
    data, metrics = html_reader._prepare_data(lines, "Test")
    assert data["pages"][0]["blocks"][0]["bbox"]["left"] == 0.1
    assert data["pages"][0]["blocks"][1]["type"] == "formula"
    assert data["pages"][0]["blocks"][1]["copy"] == "x^2 + y^2 = 1"
    assert metrics["selectable_characters"] > 20


def test_build_html_reader_is_file_url_safe_and_shareable(tmp_path, monkeypatch):
    source = tmp_path / "course.pdf"
    lines = tmp_path / "lines.json"
    output = tmp_path / "reader"
    write_pdf(source)
    write_lines(lines)
    monkeypatch.setattr(html_reader, "_render_jpegs", fake_render)

    result = html_reader.build_html_reader(source, lines, output, title="Kurs & analys")

    index = (output / "index.html").read_text(encoding="utf-8")
    assert "Kurs &amp; analys" in index
    assert "\\u003csäker\\u003e" in index
    assert "fetch(" not in index
    assert (output / "pages" / "page-0001.jpg").exists()
    assert result["qa"]["status"] == "passed"
    with zipfile.ZipFile(result["zip"]) as archive:
        assert "reader/index.html" in archive.namelist()
        assert "reader/pages/page-0001.jpg" in archive.namelist()


def test_standalone_reader_embeds_every_asset(tmp_path, monkeypatch):
    source = tmp_path / "course.pdf"
    lines = tmp_path / "lines.json"
    output = tmp_path / "course-reader.html"
    write_pdf(source)
    write_lines(lines)
    monkeypatch.setattr(html_reader, "_render_jpegs", fake_render)

    result = html_reader.build_html_reader(
        source, lines, output, standalone=True, make_zip=False
    )

    assert result["standalone"] is True
    assert result["index"] == str(output)
    assert output.is_file()
    assert not (tmp_path / "course-reader").exists()
    content = output.read_text(encoding="utf-8")
    assert content.startswith("<!--\nPDF++ standalone reader/wrapper")
    assert "Author: Isac Carlsson" in content
    assert "Copyright © 2026 Isac Carlsson" in content
    assert "This file generated:" in content
    assert "DO WHAT THE FUCK YOU WANT TO PUBLIC LICENSE" in content
    assert "This license DOES NOT apply to the embedded document" in content
    assert "pages/page-0001.jpg" not in content
    match = re.search(r"data:image/jpeg;base64,([A-Za-z0-9+/=]+)", content)
    assert match
    assert base64.b64decode(match.group(1)).startswith(b"\xff\xd8")
    assert '"build":{"manifest":' in content
    assert "https://github.com/issssse/pdf-plus-reader" in content
    payload = re.search(
        r'<script id="reader-data" type="application/json">(.*?)</script>',
        content,
        re.DOTALL,
    )
    assert payload
    reader_data = json.loads(payload.group(1))
    assert "blocks" not in reader_data
    assert reader_data["pages"][0]["search"] == (
        "sokbar text <saker> text [x 2 + y 2 = 1] formula"
    )
    assert reader_data["build"]["manifest"]["reader_performance"] == {
        "virtualized": True,
        "maximum_mounted_pages": 7,
        "search_index": "normalized_page_text+structured_exercises+structured_answers",
    }


def test_exercise_index_uses_chapter_and_group_context(tmp_path, monkeypatch):
    source = tmp_path / "course.pdf"
    lines = tmp_path / "lines.json"
    output = tmp_path / "reader"
    write_pdf(source, pages=3)
    write_lines(lines, pages=3)
    raw = json.loads(lines.read_text())

    def line(identifier: str, text: str, kind: str, y: int) -> dict:
        return {
            "id": identifier,
            "type": kind,
            "text": text,
            "region": {"top_left_x": 120, "top_left_y": y, "width": 700, "height": 55},
            "confidence": 0.99,
        }

    raw["pages"][0]["lines"] = [
        line("chapter-3", "Kapitel 3", "section_header", 100),
        line("testproblem-3", "Testproblem", "section_header", 180),
        line("instructions-3", "Bestäm lösningar till följande problem", "text", 230),
        line("task-14", "14. Bestäm transformen", "text", 280),
    ]
    raw["pages"][1]["lines"] = [
        line("running-3", "Kapitel 3", "text", 80),
        line("task-15", "15. Lös ekvationen", "text", 220),
    ]
    raw["pages"][2]["lines"] = [
        line("chapter-4", "Kapitel 4", "section_header", 100),
        line("exercises-4", "Övningar", "section_header", 180),
        line("task-7", "4.7.", "text", 280),
    ]
    lines.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(html_reader, "_render_jpegs", fake_render)

    result = html_reader.build_html_reader(source, lines, output, make_zip=False)
    index = (output / "index.html").read_text(encoding="utf-8")
    payload = re.search(
        r'<script id="reader-data" type="application/json">(.*?)</script>',
        index,
        re.DOTALL,
    )
    assert payload
    reader_data = json.loads(payload.group(1))
    assert reader_data["exercises"] == [
        {"kind": "testproblem", "label": "Testproblem", "chapter": 3, "number": 14, "page": 1, "block": "task-14"},
        {"kind": "testproblem", "label": "Testproblem", "chapter": 3, "number": 15, "page": 2, "block": "task-15"},
        {"kind": "exercise", "label": "Övningar", "chapter": 4, "number": 7, "page": 3, "block": "task-7"},
    ]
    assert result["manifest"]["exercise_entries"] == 3
    assert "function parseExerciseQuery(value)" in index
    assert "function exerciseMatches(query)" in index
    assert "tp kap 3 14ac" in index
    assert "requested.get(record.number)" in index

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the embedded search-parser test")
    norm_start = index.index("const norm =")
    norm_end = index.index("\n", norm_start)
    parser_start = index.index("function parseExerciseQuery(value)")
    parser_end = index.index("function exerciseMatches(query)")
    script = (
        index[norm_start:norm_end]
        + "\n"
        + index[parser_start:parser_end]
        + "\nconsole.log(JSON.stringify(["
        + "parseExerciseQuery('Testproblem kap. 3: 1abc, 2abc, 3abc 6, 8'),"
        + "parseExerciseQuery('öv 5 8 10 11'),"
        + "parseExerciseQuery('laplacetransform'),"
        + "parseExerciseQuery('svar tp kap 3 14ac')"
        + "]));"
    )
    completed = subprocess.run(
        [node, "-e", script], check=True, capture_output=True, text=True
    )
    parsed = json.loads(completed.stdout)
    assert parsed[0] == {
        "kind": "testproblem",
        "label": "Testproblem",
        "chapter": 3,
        "requests": [
            {"number": 1, "parts": "abc"},
            {"number": 2, "parts": "abc"},
            {"number": 3, "parts": "abc"},
            {"number": 6, "parts": ""},
            {"number": 8, "parts": ""},
        ],
        "answer": False,
    }
    assert parsed[1]["chapter"] == 5
    assert [item["number"] for item in parsed[1]["requests"]] == [8, 10, 11]
    assert parsed[2] is None
    assert parsed[3] == {
        "kind": "testproblem",
        "label": "Testproblem",
        "chapter": 3,
        "requests": [{"number": 14, "parts": "ac"}],
        "answer": True,
    }


def test_exercise_index_excludes_answer_sections(tmp_path):
    lines = tmp_path / "lines.json"
    write_lines(lines)
    raw = json.loads(lines.read_text())
    raw["pages"][0]["lines"] = [
        {
            "id": "chapter",
            "type": "section_header",
            "text": "Kapitel 3",
            "region": {"top_left_x": 100, "top_left_y": 100, "width": 500, "height": 50},
        },
        {
            "id": "answers",
            "type": "section_header",
            "text": "Svar till övningarna i kapitel 3",
            "region": {"top_left_x": 100, "top_left_y": 180, "width": 700, "height": 50},
        },
        {
            "id": "answer-14",
            "type": "list_item",
            "text": "3.14. Svarstext",
            "region": {"top_left_x": 100, "top_left_y": 260, "width": 700, "height": 50},
        },
    ]
    lines.write_text(json.dumps(raw), encoding="utf-8")
    data, _ = html_reader._prepare_data(lines, "Test")
    assert html_reader._add_exercise_metadata(data) == []
    assert html_reader._add_answer_metadata(data) == [
        {
            "kind": "exercise",
            "label": "Svar",
            "chapter": 3,
            "number": 14,
            "page": 1,
            "block": "answer-14",
        }
    ]


def test_toc_links_use_titles_and_inferred_printed_page_offset(tmp_path):
    lines = tmp_path / "lines.json"
    write_lines(lines, pages=5)
    raw = json.loads(lines.read_text())

    def line(identifier: str, text: str, kind: str, y: int) -> dict:
        return {
            "id": identifier,
            "type": kind,
            "text": text,
            "region": {"top_left_x": 100, "top_left_y": y, "width": 700, "height": 50},
        }

    raw["pages"][0]["lines"] = [
        line("contents", "Contents", "section_header", 80),
        line("toc-one", "1.1 First topic", "table_of_contents_item", 160),
        line("toc-one-page", "1", "table_of_contents_number", 160),
        line("toc-two", "1.2 Second topic", "table_of_contents_item", 240),
        line("toc-two-page", "3", "table_of_contents_number", 240),
    ]
    raw["pages"][2]["lines"] = [line("first-topic", "1.1. First topic", "section_header", 100)]
    raw["pages"][4]["lines"] = [line("second-topic", "1.2. Second topic", "section_header", 100)]
    lines.write_text(json.dumps(raw), encoding="utf-8")
    data, _ = html_reader._prepare_data(lines, "Test")
    links = html_reader._add_toc_metadata(data)
    assert data["toc_page_offset"] == 2
    assert [(item["source_block"], item["page"]) for item in links] == [
        ("toc-one", 3),
        ("toc-two", 5),
    ]
    assert data["pages"][0]["blocks"][1]["toc"]["block"] == "first-topic"


def test_repeated_toc_titles_resolve_from_their_printed_pages(tmp_path):
    lines = tmp_path / "lines.json"
    write_lines(lines, pages=8)
    raw = json.loads(lines.read_text())

    def line(identifier: str, text: str, kind: str, y: int) -> dict:
        return {
            "id": identifier,
            "type": kind,
            "text": text,
            "region": {"top_left_x": 100, "top_left_y": y, "width": 700, "height": 50},
        }

    raw["pages"][0]["lines"] = [
        line("toc-first", "1.1 First topic", "table_of_contents_item", 100),
        line("toc-first-page", "1", "table_of_contents_number", 100),
        line("toc-problems-one", "Problems", "table_of_contents_item", 180),
        line("toc-problems-one-page", "3", "table_of_contents_number", 180),
        line("toc-second", "2.1 Second topic", "table_of_contents_item", 260),
        line("toc-second-page", "4", "table_of_contents_number", 260),
        line("toc-problems-two", "Problems", "table_of_contents_item", 340),
        line("toc-problems-two-page", "6", "table_of_contents_number", 340),
    ]
    raw["pages"][2]["lines"] = [line("first", "1.1 First topic", "section_header", 100)]
    raw["pages"][4]["lines"] = [line("problems-one", "Problems", "section_header", 100)]
    raw["pages"][5]["lines"] = [line("second", "2.1 Second topic", "section_header", 100)]
    raw["pages"][7]["lines"] = [line("problems-two", "Problems", "section_header", 100)]
    lines.write_text(json.dumps(raw), encoding="utf-8")

    data, _ = html_reader._prepare_data(lines, "Test")
    links = html_reader._add_toc_metadata(data)

    assert data["toc_page_offset"] == 2
    repeated = [item for item in links if item["title"] == "Problems"]
    assert [(item["page"], item["block"]) for item in repeated] == [
        (5, "problems-one"),
        (8, "problems-two"),
    ]


def test_page_count_mismatch_fails_before_render(tmp_path):
    source = tmp_path / "two.pdf"
    lines = tmp_path / "one.json"
    write_pdf(source, pages=2)
    write_lines(lines, pages=1)
    try:
        html_reader.build_html_reader(source, lines, tmp_path / "reader")
    except ValueError as exc:
        assert "Page count mismatch" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected page count mismatch")


def test_nonempty_output_requires_force(tmp_path, monkeypatch):
    source = tmp_path / "course.pdf"
    lines = tmp_path / "lines.json"
    output = tmp_path / "reader"
    write_pdf(source)
    write_lines(lines)
    output.mkdir()
    (output / "keep.txt").write_text("mine")
    monkeypatch.setattr(html_reader, "_render_jpegs", fake_render)
    try:
        html_reader.build_html_reader(source, lines, output)
    except ValueError as exc:
        assert "--force" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected overwrite guard")


def test_corrections_are_line_scoped_and_checks_are_whitespace_tolerant(tmp_path):
    lines = tmp_path / "lines.json"
    write_lines(lines)
    data, _ = html_reader._prepare_data(lines, "Test")
    corrections = tmp_path / "corrections.json"
    corrections.write_text(
        json.dumps(
            {
                "replacements": [
                    {
                        "id": "text-1",
                        "old": "Sökbar text",
                        "new": "Verifierad text",
                        "expected_count": 1,
                    }
                ]
            }
        )
    )
    checks = tmp_path / "checks.json"
    checks.write_text(
        json.dumps(
            {
                "checks": [
                    {"page": 1, "contains": ["Verifierad   text"]},
                    {"page": 1, "not_contains": ["Sökbar text"]},
                ]
            }
        )
    )
    assert len(html_reader._apply_corrections(data, corrections)) == 1
    assert all(item["passed"] for item in html_reader._run_semantic_checks(data, checks))


def test_duplicate_ocr_ids_are_rejected(tmp_path):
    lines = tmp_path / "lines.json"
    write_lines(lines, pages=2)
    value = json.loads(lines.read_text())
    value["pages"][1]["lines"][0]["id"] = "text-1"
    lines.write_text(json.dumps(value))
    try:
        html_reader._prepare_data(lines, "Test")
    except ValueError as exc:
        assert "Duplicate OCR line id" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected duplicate ID failure")


def test_render_output_uses_numeric_page_order(tmp_path, monkeypatch):
    pages = tmp_path / "pages"

    def fake_run(*args, **kwargs):
        pages.mkdir(exist_ok=True)
        for number in (1, 10, 2):
            (pages / f"raw-{number}.jpg").write_bytes(str(number).encode())

    monkeypatch.setattr(html_reader, "require_renderer", lambda: "pdftoppm")
    monkeypatch.setattr(html_reader.subprocess, "run", fake_run)
    rendered = html_reader._render_jpegs(tmp_path / "book.pdf", pages, 144, 88)
    assert [path.read_text() for path in rendered] == ["1", "2", "10"]


def test_reader_has_cross_browser_and_device_fallbacks(tmp_path, monkeypatch):
    source = tmp_path / "course.pdf"
    lines = tmp_path / "lines.json"
    output = tmp_path / "reader"
    write_pdf(source)
    write_lines(lines)
    monkeypatch.setattr(html_reader, "_render_jpegs", fake_render)
    html_reader.build_html_reader(source, lines, output, make_zip=False)
    index = (output / "index.html").read_text(encoding="utf-8")
    assert "viewport-fit=cover" in index
    assert "safe-area-inset" in index
    assert "prefers-reduced-motion" in index
    assert "forced-colors:active" in index
    assert "touch-action:pan-x pan-y pinch-zoom" in index
    assert "orientationchange" in index
    assert "image.width = page.image_width" in index
    assert "CSS.escape" not in index
    assert "ResizeObserver" not in index
    assert "IntersectionObserver" not in index


def test_reader_uses_native_scroll_selection_and_minimal_floating_controls(tmp_path, monkeypatch):
    source = tmp_path / "course.pdf"
    lines = tmp_path / "lines.json"
    output = tmp_path / "reader"
    write_pdf(source, pages=8)
    write_lines(lines, pages=8)
    monkeypatch.setattr(html_reader, "_render_jpegs", fake_render)
    html_reader.build_html_reader(source, lines, output, make_zip=False)
    index = (output / "index.html").read_text(encoding="utf-8")

    assert "window.addEventListener('scroll'" in index
    assert "{passive:true}" in index
    assert "overflow-anchor:none" in index
    assert ".ocr-line {" in index and "user-select:text" in index
    assert "dblclick" not in index
    assert 'id="detail"' not in index
    assert "Originalsidor med sökbart OCR-lager" not in index
    assert 'id="prev"' not in index
    assert 'id="next"' not in index
    assert 'id="zoom"' not in index
    assert 'id="sidePanel"' not in index
    assert 'id="menuButton"' not in index
    assert 'id="backdrop"' not in index
    assert 'class="floating-controls"' in index
    assert 'id="pageNumber"' in index
    assert 'id="searchUI" class="search-ui"' in index
    assert 'id="info" class="info-toggle"' in index
    assert 'id="infoPanel" class="info-panel"' in index
    assert "Text, formler, rubriker och länkar är maskinlästa" in index
    assert '<span class="ocr-icon" aria-hidden="true">OCR</span>' in index
    assert "event.key.toLowerCase()==='f'" in index
    assert "contextmenu" not in index
    assert "bottom:calc(12px + env(safe-area-inset-bottom,0px))" in index
    assert "#pageNumber:focus { border-bottom-color:#626d76; outline:none; }" in index
    assert "#search:focus,#pageNumber:focus" not in index
    assert "top:calc(6px + env(safe-area-inset-top,0px))" in index
    assert ".page-control.mobile-visible,.page-control:focus-within" in index
    assert "opacity:var(--mobile-pager-opacity,1)" in index
    assert "height:30px" in index
    assert "pagerScrollDistance-32" in index
    assert "setTimeout(hideMobilePager,1050)" in index
    assert "trackMobilePagerScroll();" in index


def test_reader_has_pointer_anchored_wheel_zoom_and_fitted_text_layer(tmp_path, monkeypatch):
    source = tmp_path / "course.pdf"
    lines = tmp_path / "lines.json"
    output = tmp_path / "reader"
    write_pdf(source)
    write_lines(lines)
    monkeypatch.setattr(html_reader, "_render_jpegs", fake_render)
    html_reader.build_html_reader(source, lines, output, make_zip=False)
    index = (output / "index.html").read_text(encoding="utf-8")

    assert "window.addEventListener('wheel',queueWheelZoom,{passive:false})" in index
    assert "if (!event.ctrlKey&&!event.metaKey) return" in index
    assert "function applyZoomAt(clientX,clientY,factor)" in index
    assert "const across=oldRect" in index
    assert "span.textContent = block.copy" in index
    assert "className = 'ocr-text'" not in index
    assert "function fitTextLayer(shell,immediate=false)" in index
    assert "target/natural" in index
    assert "span.style.width = pct(block.bbox.width)" not in index
    assert "line.dataset.width" in index
    assert "function resetSelectionLayer(layer)" in index
    assert "document.addEventListener('selectionchange'" in index
    assert "end-of-content" in index


def test_search_results_and_highlights_persist_after_jump(tmp_path, monkeypatch):
    source = tmp_path / "course.pdf"
    lines = tmp_path / "lines.json"
    output = tmp_path / "reader"
    write_pdf(source)
    write_lines(lines)
    monkeypatch.setattr(html_reader, "_render_jpegs", fake_render)
    html_reader.build_html_reader(source, lines, output, make_zip=False)
    index = (output / "index.html").read_text(encoding="utf-8")

    jump = index[index.index("function jumpToBlock"):index.index("let searchTimer")]
    assert "searchResults.hidden" not in jump
    assert "pendingHits.clear" not in jump
    assert "closePanel" not in index
    assert "pendingHits.has(block.id)" in index


def test_reader_has_collapsible_search_spreads_references_and_print_css(tmp_path, monkeypatch):
    source = tmp_path / "course.pdf"
    lines = tmp_path / "lines.json"
    output = tmp_path / "reader"
    write_pdf(source, pages=2)
    write_lines(lines, pages=2)
    raw = json.loads(lines.read_text())
    raw["pages"][0]["lines"][0]["type"] = "section_header"
    raw["pages"][0]["lines"][0]["text"] = "Kapitel ett"
    lines.write_text(json.dumps(raw))
    monkeypatch.setattr(html_reader, "_render_jpegs", fake_render)
    html_reader.build_html_reader(source, lines, output, make_zip=False)
    index = (output / "index.html").read_text(encoding="utf-8")

    assert 'id="searchToggle" class="search-toggle"' in index
    assert "function expandSearch(focus=true)" in index
    assert 'id="spread" class="spread-toggle"' in index
    assert "body.two-page #pageWindow" in index
    assert "twoPage=!twoPage" in index
    assert "rowStarts" in index and "rowOffsets" in index
    assert "<a class=\"result\" href=\"${pageReference" in index
    assert "new URLSearchParams(location.hash.slice(1))" in index
    assert "let pendingBlockJump = null" in index
    assert "if (page===pendingBlockJump.page)" in index
    assert "clearTimeout(blockJumpTimer)" in index
    assert "const exercise=block?.exercise" in index
    assert "window.addEventListener('beforeprint',preparePrint)" in index
    assert "page-break-after:always" in index
    assert "color:rgba(0,0,0,.012)!important" in index
    assert "var(--page-ratio)" in index
    assert "var(--page-inverse-ratio)" in index
    assert "100vh - 2px" in index
    assert "height:auto!important" not in index
    assert ".search-ui,.ocr-toggle,.spread-toggle { display:none!important; }" in index
    assert '.ocr-toggle[aria-pressed="true"],.spread-toggle[aria-pressed="true"]' in index
    assert ".page-control {" in index and "background:transparent" in index
    assert "appearance:textfield" in index
    assert '"outline":[{"title":"Kapitel ett","page":1,"block":"text-1"}]' in index
    assert '"section":"Kapitel ett"' in index


def test_reader_virtualizes_large_books_without_mount_flashes(tmp_path, monkeypatch):
    source = tmp_path / "course.pdf"
    lines = tmp_path / "lines.json"
    output = tmp_path / "reader"
    write_pdf(source, pages=12)
    write_lines(lines, pages=12)
    monkeypatch.setattr(html_reader, "_render_jpegs", fake_render)
    html_reader.build_html_reader(source, lines, output, make_zip=False)
    index = (output / "index.html").read_text(encoding="utf-8")

    assert "const WINDOW_RADIUS = 3" in index
    assert "function mountWindow(page)" in index
    assert "function pageAtPosition(position)" in index
    assert "const nearby=Math.abs(target-originPage)<=WINDOW_RADIUS" in index
    assert "const nearby=Math.abs(block.page-originPage)<=WINDOW_RADIUS" in index
    assert 'id="topSpacer"' in index
    assert 'id="pageWindow"' in index
    assert 'id="bottomSpacer"' in index
    assert "page.search.includes(q)" in index
    assert "@keyframes ocr-intro" not in index
    assert "text-layer intro" not in index
    assert "layer.classList.remove('intro')" not in index
    assert "document.createElement(block.toc?'a':'span')" in index


def test_mixed_portrait_and_landscape_page_ratios(tmp_path, monkeypatch):
    source = tmp_path / "mixed.pdf"
    lines = tmp_path / "mixed.lines.json"
    output = tmp_path / "reader"
    write_pdf(source, sizes=[(600, 800), (800, 600)])
    write_lines(lines, pages=2)
    raw = json.loads(lines.read_text())
    raw["pages"][1]["page_width"] = 1600
    raw["pages"][1]["page_height"] = 1200
    lines.write_text(json.dumps(raw))

    def mixed_render(source: Path, pages_dir: Path, dpi: int, quality: int) -> list[Path]:
        pages_dir.mkdir(parents=True)
        sizes = [(900, 1200), (1200, 900)]
        rendered = []
        for page, size in enumerate(sizes, start=1):
            target = pages_dir / f"page-{page:04d}.jpg"
            Image.new("RGB", size, "white").save(target)
            rendered.append(target)
        return rendered

    monkeypatch.setattr(html_reader, "_render_jpegs", mixed_render)
    result = html_reader.build_html_reader(source, lines, output, make_zip=False)
    assert [page["image_ocr_aspect_ratio_delta"] for page in result["qa"]["pages"]] == [0.0, 0.0]
    assert [page["image_source_aspect_ratio_delta"] for page in result["qa"]["pages"]] == [0.0, 0.0]
    assert '"image_width":1200,"image_height":900' in (output / "index.html").read_text()


def test_rotated_pdf_page_uses_display_orientation(tmp_path):
    source = tmp_path / "rotated.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    page.rotate(90)
    with source.open("wb") as stream:
        writer.write(stream)
    pdf_page = html_reader.PdfReader(source).pages[0]
    assert html_reader._display_page_size(pdf_page) == (800.0, 600.0)


def test_display_page_size_and_renderer_use_cropbox(tmp_path, monkeypatch):
    source = tmp_path / "cropped.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    page.cropbox.lower_left = (72, 90)
    page.cropbox.upper_right = (540, 714)
    with source.open("wb") as stream:
        writer.write(stream)

    pdf_page = html_reader.PdfReader(source).pages[0]
    assert html_reader._display_page_size(pdf_page) == (468.0, 624.0)

    captured: list[str] = []

    def fake_run(command, **kwargs):
        captured.extend(command)
        pages_dir = Path(command[-1]).parent
        Image.new("RGB", (468, 624), "white").save(pages_dir / "raw-1.jpg")

    monkeypatch.setattr(html_reader, "require_renderer", lambda: "pdftoppm")
    monkeypatch.setattr(html_reader.subprocess, "run", fake_run)
    rendered = html_reader._render_jpegs(source, tmp_path / "pages", 72, 80)

    assert "-cropbox" in captured
    assert Image.open(rendered[0]).size == (468, 624)


def test_geometry_mismatch_is_a_hard_failure(tmp_path, monkeypatch):
    source = tmp_path / "course.pdf"
    lines = tmp_path / "lines.json"
    write_pdf(source)
    write_lines(lines)

    def wrong_ratio(source: Path, pages_dir: Path, dpi: int, quality: int) -> list[Path]:
        pages_dir.mkdir(parents=True)
        target = pages_dir / "page-0001.jpg"
        Image.new("RGB", (1200, 600), "white").save(target)
        return [target]

    monkeypatch.setattr(html_reader, "_render_jpegs", wrong_ratio)
    try:
        html_reader.build_html_reader(source, lines, tmp_path / "reader", make_zip=False)
    except ValueError as exc:
        assert "geometry QA failed" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected geometry QA failure")
