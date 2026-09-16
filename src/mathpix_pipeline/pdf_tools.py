from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter


def parse_pages(spec: str, total: int) -> list[int]:
    """Parse 1-based page specs such as ``1,3-5,last``."""
    if total < 1:
        raise ValueError("PDF has no pages")
    pages: list[int] = []
    for token in spec.split(","):
        token = token.strip().lower()
        if not token:
            continue
        if token == "last":
            values = [total]
        elif re.fullmatch(r"\d+", token):
            values = [int(token)]
        elif re.fullmatch(r"(?:\d+|last)-(?:\d+|last)", token):
            start_s, end_s = token.split("-", 1)
            start = total if start_s == "last" else int(start_s)
            end = total if end_s == "last" else int(end_s)
            if start > end:
                raise ValueError(f"Descending page range is not allowed: {token}")
            values = list(range(start, end + 1))
        else:
            raise ValueError(f"Invalid page token: {token!r}")
        for page in values:
            if not 1 <= page <= total:
                raise ValueError(f"Page {page} is outside 1..{total}")
            if page not in pages:
                pages.append(page)
    if not pages:
        raise ValueError("Page selection is empty")
    return pages


def page_count(path: Path) -> int:
    return len(PdfReader(path).pages)


def select_pages(source: Path, destination: Path, pages: list[int]) -> None:
    reader = PdfReader(source)
    writer = PdfWriter()
    for page in pages:
        writer.add_page(reader.pages[page - 1])
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as stream:
        writer.write(stream)


def extract_text_pages(path: Path) -> list[str]:
    return [(page.extract_text() or "") for page in PdfReader(path).pages]


def page_sizes(path: Path) -> list[tuple[float, float]]:
    sizes = []
    for page in PdfReader(path).pages:
        box = page.mediabox
        sizes.append((round(float(box.width), 3), round(float(box.height), 3)))
    return sizes


def require_renderer() -> str:
    executable = shutil.which("pdftoppm")
    if not executable:
        raise RuntimeError("pdftoppm is required for visual QA (install poppler-utils)")
    return executable


def render_pdf(path: Path, destination: Path, dpi: int = 120) -> list[Path]:
    executable = require_renderer()
    destination.mkdir(parents=True, exist_ok=True)
    prefix = destination / "page"
    subprocess.run(
        [executable, "-r", str(dpi), "-png", str(path), str(prefix)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    return sorted(destination.glob("page-*.png"))


def ink_fraction(image_path: Path, white_threshold: int = 245) -> float:
    gray = Image.open(image_path).convert("L")
    histogram = gray.histogram()
    ink = sum(histogram[:white_threshold])
    return ink / float(gray.width * gray.height)


def content_bbox_fraction(image_path: Path, white_threshold: int = 245) -> dict[str, float] | None:
    gray = Image.open(image_path).convert("L")
    # Ignore isolated scanner dust and compression speckles. A row/column must
    # contain a small but non-trivial amount of ink to count as page content.
    pixels = gray.load()
    row_minimum = max(3, round(gray.width * 0.0015))
    column_minimum = max(3, round(gray.height * 0.0015))
    rows = [
        y
        for y in range(gray.height)
        if sum(pixels[x, y] < white_threshold for x in range(gray.width)) >= row_minimum
    ]
    columns = [
        x
        for x in range(gray.width)
        if sum(pixels[x, y] < white_threshold for y in range(gray.height)) >= column_minimum
    ]
    if not rows or not columns:
        return None
    left, top, right, bottom = min(columns), min(rows), max(columns) + 1, max(rows) + 1
    return {
        "left": left / gray.width,
        "top": top / gray.height,
        "right": right / gray.width,
        "bottom": bottom / gray.height,
        "width": (right - left) / gray.width,
        "height": (bottom - top) / gray.height,
    }


def make_contact_sheet(
    source_images: list[Path],
    output_images: list[Path],
    destination: Path,
    source_label: str,
    output_label: str,
) -> None:
    pairs = max(len(source_images), len(output_images))
    if not pairs:
        return
    thumb_width = 620
    gutter = 18
    header = 42
    rows: list[Image.Image] = []
    font = ImageFont.load_default(size=18)
    for idx in range(pairs):
        panels = []
        for label, images in (
            (source_label, source_images),
            (output_label, output_images),
        ):
            if idx < len(images):
                image = Image.open(images[idx]).convert("RGB")
                image.thumbnail((thumb_width, 900))
            else:
                image = Image.new("RGB", (thumb_width, 860), "#f4f4f4")
                ImageDraw.Draw(image).text((20, 20), "NO CORRESPONDING PAGE", fill="#a00000", font=font)
            panel = Image.new("RGB", (thumb_width, image.height + header), "white")
            panel.paste(image, ((thumb_width - image.width) // 2, header))
            suffix = f"page {idx + 1}" if idx < len(images) else "missing"
            ImageDraw.Draw(panel).text((8, 10), f"{label} - {suffix}", fill="black", font=font)
            panels.append(panel)
        row_height = max(panel.height for panel in panels)
        row = Image.new("RGB", (thumb_width * 2 + gutter, row_height), "#d8d8d8")
        row.paste(panels[0], (0, 0))
        row.paste(panels[1], (thumb_width + gutter, 0))
        rows.append(row)
    sheet = Image.new(
        "RGB",
        (rows[0].width, sum(row.height for row in rows) + gutter * (len(rows) - 1)),
        "#b8b8b8",
    )
    y = 0
    for row in rows:
        sheet.paste(row, (0, y))
        y += row.height + gutter
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, optimize=True)
