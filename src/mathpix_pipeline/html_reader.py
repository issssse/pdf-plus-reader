from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image
from pypdf import PdfReader
from rapidfuzz import fuzz

from .pdf_tools import require_renderer


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _display_page_size(page: Any) -> tuple[float, float]:
    # PDF viewers display the CropBox when present. Mathpix likewise OCRs the
    # visible page, so using the MediaBox here can shift every text coordinate
    # on PDFs whose scans have been cropped after creation.
    width = float(page.cropbox.width)
    height = float(page.cropbox.height)
    rotation = int(page.get("/Rotate", 0) or 0) % 360
    return (height, width) if rotation in {90, 270} else (width, height)


def _block_type(raw_type: Any) -> str:
    value = str(raw_type or "text").lower()
    if value in {"math", "equation", "formula"}:
        return "formula"
    if value in {"section_header", "title", "heading"}:
        return "heading"
    if "table" in value:
        return "table"
    if value in {"diagram", "figure", "image", "picture"}:
        return "figure"
    return "text"


def _copy_text(text: str, block_type: str) -> str:
    value = text.strip()
    if block_type == "formula":
        for left, right in (("\\[", "\\]"), ("$$", "$$"), ("\\(", "\\)")):
            if value.startswith(left) and value.endswith(right):
                value = value[len(left) : -len(right)].strip()
                break
    return value


def _normalize_search(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.lower())
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.replace("\\", "")
    value = re.sub(r"[{}_\^$]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _add_navigation_metadata(data: dict[str, Any]) -> list[dict[str, Any]]:
    outline: list[dict[str, Any]] = []
    current_section = data["title"]
    previous_title = ""
    for page in data["pages"]:
        for block in page["blocks"]:
            if block["type"] != "heading":
                continue
            heading = re.sub(r"\s+", " ", block["copy"]).strip()
            if not heading or heading == previous_title:
                continue
            outline.append(
                {
                    "title": heading,
                    "page": page["page"],
                    "block": block["id"],
                }
            )
            current_section = heading
            previous_title = heading
        page["section"] = current_section
    data["outline"] = outline
    return outline


_CHAPTER_PATTERN = re.compile(r"\b(?:kap(?:itel)?|chapter|ch)\.?\s*(\d{1,3})\b", re.I)
_SECTION_PATTERN = re.compile(r"^\s*\d{1,3}\.\d+(?:\.\d+)?\.?\s+\S")
_EXERCISE_PATTERN = re.compile(
    r"^\s*(?:(?:uppgift|problem|exercise|ovning|övning)\s+)?"
    r"(\d{1,3})(?:\s*([a-zåäö]{1,8}))?\s*[.)\]:-](?:\s*(\S.*))?$",
    re.I,
)
_CHAPTER_EXERCISE_PATTERN = re.compile(
    r"^\s*(\d{1,3})[.-](\d{1,3})\s*[.)\]:-]?(?:\s*(\S.*))?$",
    re.I,
)


def _is_answer_heading(normalized: str) -> bool:
    return bool(re.match(r"^(?:svar|facit|answers?|solutions?|losningar)(?:\s|$)", normalized))


def _exercise_group(value: str) -> tuple[str, str] | None:
    normalized = _normalize_search(value)
    if len(normalized) > 100:
        return None
    if _is_answer_heading(normalized):
        return None
    if re.search(r"\btest\s*problem\b", normalized):
        return "testproblem", "Testproblem"
    if re.fullmatch(
        r"(?:ovningar|exercises?|problems?|problem sets?|review problems?)"
        r"(?:\s+(?:for|till)\s+(?:chapter|kapitel)\s+\d{1,3})?",
        normalized,
    ):
        label = "Övningar" if "ovning" in normalized else "Problems"
        return "exercise", label
    return None


def _answer_group(value: str) -> tuple[str, str] | None:
    normalized = _normalize_search(value)
    if len(normalized) > 140 or not _is_answer_heading(normalized):
        return None
    swedish = bool(re.match(r"^(?:svar|facit|losningar)", normalized))
    label = "Svar" if swedish else "Solutions"
    if re.search(r"\b(?:testproblem\w*|test\s+problems?)\b", normalized):
        return "testproblem", label
    if re.search(r"\b(?:ovning\w*|exercises?)\b", normalized):
        return "exercise", label
    return "problem", label


def _chapter_from_context(block: dict[str, Any]) -> int | None:
    """Read chapter context only from headings/running heads, never prose references."""
    normalized = _normalize_search(block["copy"])
    raw_type = block.get("raw_type")
    if raw_type == "table_of_contents_item":
        return None
    starts_with_chapter = bool(re.match(r"^(?:kap(?:itel)?|chapter|ch)\.?\s*\d", normalized))
    heading_like = block["type"] == "heading" or raw_type == "page_info" or starts_with_chapter
    if not heading_like:
        return None
    chapter_match = _CHAPTER_PATTERN.search(normalized)
    if chapter_match:
        return int(chapter_match.group(1))
    if raw_type == "page_info":
        running = re.match(r"^(\d{1,3})\s*/", normalized)
        if running:
            return int(running.group(1))
    if block["type"] == "heading":
        numbered = re.match(r"^(\d{1,3})(?:\D|$)", normalized)
        if numbered:
            return int(numbered.group(1))
    return None


def _add_exercise_metadata(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Create conservative exercise anchors from document order and heading context."""
    exercises: list[dict[str, Any]] = []
    chapter: int | None = None
    group: tuple[str, str] | None = None
    group_style: str | None = None
    last_number: int | None = None
    seen: set[tuple[str, int | None, int, str]] = set()
    for page in data["pages"]:
        for block in page["blocks"]:
            text = re.sub(r"\s+", " ", block["copy"]).strip()
            normalized_text = _normalize_search(text)
            context_chapter = _chapter_from_context(block)
            if context_chapter is not None:
                next_chapter = context_chapter
                if chapter is not None and next_chapter != chapter:
                    group = None
                    group_style = None
                    last_number = None
                chapter = next_chapter
            if _is_answer_heading(normalized_text) and (
                block["type"] == "heading" or len(normalized_text) < 100
            ):
                group = None
                continue
            detected_group = _exercise_group(text)
            if detected_group:
                if group != detected_group:
                    group_style = None
                    last_number = None
                group = detected_group
                continue
            qualified = _CHAPTER_EXERCISE_PATTERN.match(text)
            if group and (
                (_SECTION_PATTERN.match(text) and not (group[0] == "exercise" and qualified))
                or (block["type"] == "heading" and len(text) < 120)
            ):
                group = None
                continue
            if not group or block["type"] != "text":
                continue
            if qualified and group[0] == "exercise":
                number_chapter = int(qualified.group(1))
                if chapter is not None and number_chapter != chapter and group_style is not None:
                    continue
                chapter = number_chapter
                number = int(qualified.group(2))
                remainder = qualified.group(3) or ""
                group_style = "qualified"
            else:
                match = _EXERCISE_PATTERN.match(text)
                if not match:
                    continue
                number = int(match.group(1))
                remainder = match.group(3) or ""
                if group_style == "qualified" and chapter is not None and last_number is not None:
                    joined = str(number)
                    prefix = str(chapter)
                    if joined.startswith(prefix) and len(joined) > len(prefix):
                        repaired = int(joined[len(prefix) :])
                        if repaired == last_number + 1:
                            number = repaired
            if number > 300 or re.match(r"^\d+[.)]", remainder):
                continue
            key = (group[0], chapter, number, block["id"])
            if key in seen:
                continue
            seen.add(key)
            exercise = {
                "kind": group[0],
                "label": group[1],
                "chapter": chapter,
                "number": number,
                "page": page["page"],
                "block": block["id"],
            }
            exercises.append(exercise)
            block["exercise"] = exercise
            last_number = number
        page["chapter"] = chapter
        if group:
            page["exercise_group"] = {"kind": group[0], "label": group[1]}
    data["exercises"] = exercises
    return exercises


def _add_answer_metadata(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Index numbered answers/solutions without assuming book-specific page ranges."""
    answers: list[dict[str, Any]] = []
    chapter: int | None = None
    group: tuple[str, str] | None = None
    sparse_group = False
    seen: set[tuple[str, int | None, int, str]] = set()
    for page in data["pages"]:
        for block in page["blocks"]:
            text = re.sub(r"\s+", " ", block["copy"]).strip()
            normalized = _normalize_search(text)
            context_chapter = _chapter_from_context(block)
            detected_group = _answer_group(text)
            raw_type = block.get("raw_type", "")
            section_marker = not raw_type.startswith("table_of_contents") and (
                block["type"] == "heading"
                or raw_type == "page_info"
                or bool(re.match(r"^(?:answers|svar\s+till|facit\s+till)\b", normalized))
                or bool(
                    re.match(r"^solutions\s+to\b", normalized)
                    and re.search(r"\b(?:problems?|exercises?|chapter)\b", normalized)
                )
            )
            if detected_group and section_marker:
                group = detected_group
                sparse_group = bool(re.search(r"\b(?:even|odd|selected|urval|utvalda)\b", normalized))
                explicit_chapter = _CHAPTER_PATTERN.search(normalized)
                if explicit_chapter:
                    chapter = int(explicit_chapter.group(1))
                continue
            if context_chapter is not None and group:
                chapter = context_chapter
                if block["type"] == "heading" or len(normalized) < 50:
                    continue
            if not group:
                continue
            if block["type"] == "heading" and block.get("raw_type") != "page_info":
                group = None
                continue
            qualified = _CHAPTER_EXERCISE_PATTERN.match(text)
            if qualified:
                number_chapter = int(qualified.group(1))
                if chapter is not None and number_chapter != chapter:
                    continue
                chapter = number_chapter
                number = int(qualified.group(2))
                remainder = qualified.group(3) or ""
            else:
                match = _EXERCISE_PATTERN.match(text)
                if not match:
                    continue
                number = int(match.group(1))
                remainder = match.group(3) or ""
            if number > 500 or re.match(r"^\d+[.)]", remainder):
                continue
            key = (group[0], chapter, number, block["id"])
            if key in seen:
                continue
            seen.add(key)
            answer = {
                "kind": group[0],
                "label": group[1],
                "chapter": chapter,
                "number": number,
                "page": page["page"],
                "block": block["id"],
                "_sparse": sparse_group,
            }
            answers.append(answer)
            block["answer"] = answer
        if group:
            page["answer_group"] = {"kind": group[0], "label": group[1]}
    block_order = [block for page in data["pages"] for block in page["blocks"]]
    positions = {block["id"]: index for index, block in enumerate(block_order)}
    completed: list[dict[str, Any]] = []
    for index, answer in enumerate(answers):
        completed.append(answer)
        if index + 1 >= len(answers):
            continue
        following = answers[index + 1]
        if (
            answer["_sparse"]
            or following["_sparse"]
            or answer["kind"] != following["kind"]
            or answer["chapter"] != following["chapter"]
            or answer["page"] != following["page"]
            or following["number"] != answer["number"] + 2
        ):
            continue
        start = positions[answer["block"]] + 1
        end = positions[following["block"]]
        candidates = [block for block in block_order[start:end] if block.get("copy")]
        target = candidates[len(candidates) // 2] if candidates else block_order[positions[answer["block"]]]
        inferred = {
            "kind": answer["kind"],
            "label": answer["label"],
            "chapter": answer["chapter"],
            "number": answer["number"] + 1,
            "page": answer["page"],
            "block": target["id"],
            "inferred": True,
            "_sparse": False,
        }
        target["answer"] = inferred
        completed.append(inferred)
    answers = completed
    for answer in answers:
        answer.pop("_sparse", None)
    data["answers"] = answers
    return answers


def _toc_key(value: str) -> str:
    normalized = _normalize_search(value)
    normalized = re.sub(r"[^a-z0-9åäö]+", " ", normalized)
    normalized = re.sub(r"\b(?:kapitel|chapter)\b", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _section_id(value: str) -> tuple[int, ...] | None:
    match = re.match(r"^\s*(\d{1,3}(?:\s*[.-]\s*\d{1,3})*)\b", value)
    if not match:
        return None
    return tuple(int(part) for part in re.findall(r"\d+", match.group(1)))


def _add_toc_metadata(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Link Mathpix TOC rows to OCR destinations using titles and inferred page offset."""
    records: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    toc_pages: set[int] = set()
    for page in data["pages"]:
        for block in page["blocks"]:
            raw_type = block.get("raw_type")
            if raw_type == "table_of_contents_item":
                pending = {"title": block["copy"], "page": page["page"], "block": block}
                records.append(pending)
                toc_pages.add(page["page"])
            elif raw_type == "table_of_contents_number" and pending and "printed_page" not in pending:
                match = re.search(r"\d+", block["copy"])
                if match:
                    pending["printed_page"] = int(match.group())

    candidates: list[dict[str, Any]] = []
    for page in data["pages"]:
        if page["page"] in toc_pages:
            continue
        heading_blocks = [
            block
            for block in page["blocks"]
            if block["type"] == "heading"
            or _is_answer_heading(_normalize_search(block["copy"]))
            or _exercise_group(block["copy"])
        ]
        for block in heading_blocks:
            candidates.append(
                {"title": block["copy"], "key": _toc_key(block["copy"]), "page": page["page"], "block": block["id"]}
            )
        for start in range(len(heading_blocks)):
            for length in (2, 3):
                group = heading_blocks[start : start + length]
                if len(group) != length:
                    continue
                title = " ".join(block["copy"] for block in group)
                candidates.append(
                    {"title": title, "key": _toc_key(title), "page": page["page"], "block": group[0]["id"]}
                )

    record_key_counts = Counter(_toc_key(record["title"]) for record in records)
    for record in records:
        key = _toc_key(record["title"])
        record["key"] = key
        source_section = _section_id(record["title"])
        # A bare repeated heading such as "Problems" is not globally
        # identifiable. It is resolved from its printed page after the offset
        # has been inferred from distinctive headings.
        if source_section is None and record_key_counts[key] > 1:
            continue
        scored = [
            (fuzz.ratio(key, candidate["key"]), candidate)
            for candidate in candidates
            if key and candidate["key"]
            and (
                source_section is None
                or source_section == _section_id(candidate["title"])
            )
        ]
        if scored:
            score, candidate = max(scored, key=lambda item: (item[0], -item[1]["page"]))
            if score >= 90:
                record["matched"] = candidate
                record["match_score"] = score

    offsets = Counter(
        record["matched"]["page"] - record["printed_page"]
        for record in records
        if record.get("matched") and record.get("printed_page")
    )
    page_offset = offsets.most_common(1)[0][0] if offsets else 0
    for record in records:
        destination = record.get("matched")
        if record.get("printed_page"):
            target_page = record["printed_page"] + page_offset
            if 1 <= target_page <= len(data["pages"]):
                on_page = [candidate for candidate in candidates if candidate["page"] == target_page]
                scored = [
                    (fuzz.ratio(record["key"], candidate["key"]), candidate)
                    for candidate in on_page
                    if record["key"] and candidate["key"]
                ]
                best = max(scored, key=lambda item: item[0], default=None)
                target_blocks = data["pages"][target_page - 1]["blocks"]
                destination = {
                    "page": target_page,
                    "block": (
                        best[1]["block"]
                        if best and best[0] >= 70
                        else target_blocks[0]["id"] if target_blocks else None
                    ),
                }
        record["destination"] = destination
    for index, record in enumerate(records):
        if record.get("destination") or not re.match(
            r"^(?:kapitel|chapter)\b", _normalize_search(record["title"])
        ):
            continue
        following = next(
            (item.get("destination") for item in records[index + 1 :] if item.get("destination")),
            None,
        )
        if following:
            record["destination"] = following

    links: list[dict[str, Any]] = []
    for record in records:
        destination = record.get("destination")
        if not destination:
            continue
        link = {
            "title": record["title"],
            "source_page": record["page"],
            "source_block": record["block"]["id"],
            "page": destination["page"],
            "block": destination.get("block"),
        }
        record["block"]["toc"] = link
        links.append(link)
    data["toc"] = links
    data["toc_page_offset"] = page_offset
    return links


def _prepare_data(lines_path: Path, title: str) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = json.loads(lines_path.read_text(encoding="utf-8"))
    raw_pages = raw.get("pages") if isinstance(raw, dict) else None
    if not isinstance(raw_pages, list):
        raise ValueError("Mathpix lines JSON must contain a pages array")

    pages: list[dict[str, Any]] = []
    all_blocks: list[dict[str, Any]] = []
    block_ids: set[str] = set()
    warnings: list[str] = []
    invalid_regions = 0
    empty_lines = 0
    nontext_regions = 0
    confidences: list[float] = []

    for page_index, raw_page in enumerate(raw_pages, start=1):
        page_width = _number(raw_page.get("page_width"))
        page_height = _number(raw_page.get("page_height"))
        if page_width <= 0 or page_height <= 0:
            raise ValueError(f"Page {page_index} has invalid Mathpix dimensions")
        page_blocks: list[dict[str, Any]] = []
        for line_index, line in enumerate(raw_page.get("lines") or [], start=1):
            text_value = str(line.get("text") or "").strip()
            if not text_value:
                if str(line.get("type") or "").lower() in {
                    "text",
                    "math",
                    "equation",
                    "formula",
                    "section_header",
                    "title",
                    "heading",
                    "table",
                }:
                    empty_lines += 1
                else:
                    nontext_regions += 1
                continue
            region = line.get("region") or {}
            x = _number(region.get("top_left_x"))
            y = _number(region.get("top_left_y"))
            width = _number(region.get("width"))
            height = _number(region.get("height"))
            if width <= 0 or height <= 0:
                invalid_regions += 1
                continue
            left = x / page_width
            top = y / page_height
            norm_width = width / page_width
            norm_height = height / page_height
            if left < -0.01 or top < -0.01 or left + norm_width > 1.01 or top + norm_height > 1.01:
                invalid_regions += 1
                continue
            kind = _block_type(line.get("type"))
            copy_value = _copy_text(text_value, kind)
            block_id = str(line.get("id") or f"p{page_index:04d}-l{line_index:04d}")
            if block_id in block_ids:
                raise ValueError(f"Duplicate OCR line id: {block_id}")
            block_ids.add(block_id)
            block = {
                "id": block_id,
                "page": page_index,
                "type": kind,
                "raw_type": str(line.get("type") or "text").lower(),
                "text": text_value,
                "copy": copy_value,
                "confidence": line.get("confidence"),
                "bbox": {
                    "left": max(0.0, left),
                    "top": max(0.0, top),
                    "width": min(1.0 - max(0.0, left), norm_width),
                    "height": min(1.0 - max(0.0, top), norm_height),
                },
            }
            if isinstance(line.get("confidence"), (int, float)):
                confidences.append(float(line["confidence"]))
            page_blocks.append(block)
            all_blocks.append(block)
        pages.append(
            {
                "page": page_index,
                "ocr_width": page_width,
                "ocr_height": page_height,
                "image": f"pages/page-{page_index:04d}.jpg",
                "blocks": page_blocks,
            }
        )

    if invalid_regions:
        warnings.append(f"Skipped {invalid_regions} OCR lines with invalid regions")
    if empty_lines:
        warnings.append(f"Skipped {empty_lines} empty OCR lines")
    data = {
        "title": title,
        "pages": pages,
        "blocks": all_blocks,
    }
    metrics = {
        "ocr_pages": len(pages),
        "ocr_lines": len(all_blocks),
        "selectable_characters": sum(len(block["copy"]) for block in all_blocks),
        "formula_lines": sum(block["type"] == "formula" for block in all_blocks),
        "invalid_regions": invalid_regions,
        "empty_lines": empty_lines,
        "nontext_regions": nontext_regions,
        "minimum_confidence": min(confidences) if confidences else None,
        "mean_confidence": sum(confidences) / len(confidences) if confidences else None,
        "lines_below_0_9_confidence": sum(value < 0.9 for value in confidences),
        "warnings": warnings,
    }
    return data, metrics


def _apply_corrections(data: dict[str, Any], corrections_path: Path) -> list[dict[str, Any]]:
    raw = json.loads(corrections_path.read_text(encoding="utf-8"))
    corrections = raw.get("replacements") if isinstance(raw, dict) else None
    if not isinstance(corrections, list):
        raise ValueError("Corrections JSON must contain a replacements array")
    block_by_id = {block["id"]: block for block in data["blocks"]}
    applied: list[dict[str, Any]] = []
    for index, correction in enumerate(corrections, start=1):
        old = correction.get("old")
        new = correction.get("new")
        expected = int(correction.get("expected_count", 1))
        block_id = correction.get("id")
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            raise ValueError(f"Correction {index} must have non-empty old and string new values")
        if block_id:
            if block_id not in block_by_id:
                raise ValueError(f"Correction {index} references unknown OCR line id {block_id}")
            candidates = [block_by_id[block_id]]
        else:
            candidates = data["blocks"]
        actual = sum(block["text"].count(old) for block in candidates)
        if actual != expected:
            raise ValueError(
                f"Correction {index} expected {expected} occurrence(s), found {actual}: "
                f"{correction.get('description', old)!r}"
            )
        for block in candidates:
            if old in block["text"]:
                block["text"] = block["text"].replace(old, new)
                block["copy"] = _copy_text(block["text"], block["type"])
        applied.append(
            {
                "description": correction.get("description", f"Correction {index}"),
                "id": block_id,
                "count": actual,
            }
        )
    return applied


def _run_semantic_checks(data: dict[str, Any], checks_path: Path) -> list[dict[str, Any]]:
    raw = json.loads(checks_path.read_text(encoding="utf-8"))
    checks = raw.get("checks") if isinstance(raw, dict) else None
    if not isinstance(checks, list):
        raise ValueError("Checks JSON must contain a checks array")
    page_text = {
        page["page"]: " ".join(block["text"] for block in page["blocks"])
        for page in data["pages"]
    }
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    normalize = lambda value: " ".join(str(value).split())
    for index, check in enumerate(checks, start=1):
        page = int(check.get("page", 0))
        haystack = normalize(page_text.get(page, ""))
        missing = [value for value in check.get("contains", []) if normalize(value) not in haystack]
        forbidden = [value for value in check.get("not_contains", []) if normalize(value) in haystack]
        passed = not missing and not forbidden
        description = str(check.get("description") or f"Check {index}")
        results.append(
            {
                "page": page,
                "description": description,
                "passed": passed,
                "missing": missing,
                "forbidden_present": forbidden,
            }
        )
        if not passed:
            failures.append(f"page {page}: {description}")
    if failures:
        raise ValueError("Semantic HTML reader checks failed: " + "; ".join(failures))
    return results


def _render_jpegs(source: Path, pages_dir: Path, dpi: int, quality: int) -> list[Path]:
    renderer = require_renderer()
    pages_dir.mkdir(parents=True, exist_ok=True)
    prefix = pages_dir / "raw"
    subprocess.run(
        [
            renderer,
            "-cropbox",
            "-r",
            str(dpi),
            "-jpeg",
            "-jpegopt",
            f"quality={quality},progressive=y,optimize=y",
            str(source),
            str(prefix),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    rendered = sorted(
        pages_dir.glob("raw-*.jpg"), key=lambda path: int(path.stem.rsplit("-", 1)[-1])
    )
    final: list[Path] = []
    for index, path in enumerate(rendered, start=1):
        destination = pages_dir / f"page-{index:04d}.jpg"
        path.replace(destination)
        final.append(destination)
    return final


def _reader_html(data: dict[str, Any]) -> str:
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    encoded = encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    build_date = str(data.get("build", {}).get("manifest", {}).get("created_at", ""))[:10]
    return (
        READER_TEMPLATE.replace("__READER_TITLE__", html.escape(data["title"]))
        .replace("__READER_BUILD_DATE__", html.escape(build_date or "unknown"))
        .replace("__READER_DATA__", encoded)
    )


def build_html_reader(
    source: Path,
    lines_path: Path,
    output_dir: Path,
    *,
    title: str | None = None,
    dpi: int = 144,
    quality: int = 88,
    corrections_path: Path | None = None,
    checks_path: Path | None = None,
    make_zip: bool = True,
    standalone: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    source = source.resolve()
    lines_path = lines_path.resolve()
    output_dir = output_dir.resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    if not lines_path.exists():
        raise FileNotFoundError(lines_path)
    if dpi < 72 or dpi > 300:
        raise ValueError("HTML reader DPI must be between 72 and 300")
    if quality < 50 or quality > 100:
        raise ValueError("JPEG quality must be between 50 and 100")
    if standalone and output_dir.suffix.lower() != ".html":
        output_dir = output_dir.with_suffix(".html")
    if output_dir.exists() and not force:
        if standalone or output_dir.is_file() or any(output_dir.iterdir()):
            raise ValueError(f"Output already exists: {output_dir} (use --force to replace it)")

    pdf_reader = PdfReader(source)
    metadata_title = ""
    if pdf_reader.metadata:
        metadata_title = str(pdf_reader.metadata.title or "").strip()
    reader_title = title or metadata_title or source.stem
    data, metrics = _prepare_data(lines_path, reader_title)
    corrections = _apply_corrections(data, corrections_path.resolve()) if corrections_path else []
    semantic_checks = _run_semantic_checks(data, checks_path.resolve()) if checks_path else []
    metrics["selectable_characters"] = sum(len(block["copy"]) for block in data["blocks"])
    outline = _add_navigation_metadata(data)
    exercises = _add_exercise_metadata(data)
    answers = _add_answer_metadata(data)
    toc_links = _add_toc_metadata(data)
    pdf_pages = pdf_reader.pages
    source_pages = len(pdf_pages)
    if source_pages != metrics["ocr_pages"]:
        raise ValueError(
            f"Page count mismatch: PDF has {source_pages}, lines JSON has {metrics['ocr_pages']}"
        )

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output_dir.name}-", dir=output_dir.parent) as temp_name:
        staging = Path(temp_name) / "reader"
        pages_dir = staging / "pages"
        rendered = _render_jpegs(source, pages_dir, dpi, quality)
        if len(rendered) != source_pages:
            raise RuntimeError(f"Rendered {len(rendered)} pages; expected {source_pages}")

        page_checks = []
        for page, image_path, pdf_page in zip(data["pages"], rendered, pdf_pages, strict=True):
            with Image.open(image_path) as image:
                image_width, image_height = image.size
            image_ratio = image_width / image_height
            ocr_ratio = page["ocr_width"] / page["ocr_height"]
            source_width, source_height = _display_page_size(pdf_page)
            source_ratio = source_width / source_height
            page["image_width"] = image_width
            page["image_height"] = image_height
            page_checks.append(
                {
                    "page": page["page"],
                    "source_width": round(source_width, 3),
                    "source_height": round(source_height, 3),
                    "image_width": image_width,
                    "image_height": image_height,
                    "ocr_width": page["ocr_width"],
                    "ocr_height": page["ocr_height"],
                    "image_ocr_aspect_ratio_delta": round(abs(image_ratio - ocr_ratio), 6),
                    "image_source_aspect_ratio_delta": round(abs(image_ratio - source_ratio), 6),
                    "ocr_source_aspect_ratio_delta": round(abs(ocr_ratio - source_ratio), 6),
                    "blocks": len(page["blocks"]),
                }
            )

        image_ocr_mismatch = [
            p["page"] for p in page_checks if p["image_ocr_aspect_ratio_delta"] > 0.02
        ]
        image_source_mismatch = [
            p["page"] for p in page_checks if p["image_source_aspect_ratio_delta"] > 0.01
        ]
        ocr_source_mismatch = [
            p["page"] for p in page_checks if p["ocr_source_aspect_ratio_delta"] > 0.02
        ]
        geometry_failures = []
        if image_ocr_mismatch:
            geometry_failures.append(f"OCR/image pages {image_ocr_mismatch}")
        if image_source_mismatch:
            geometry_failures.append(f"PDF/image pages {image_source_mismatch}")
        if ocr_source_mismatch:
            geometry_failures.append(f"PDF/OCR pages {ocr_source_mismatch}")
        if geometry_failures:
            raise ValueError("Reader geometry QA failed: " + "; ".join(geometry_failures))
        warnings = list(metrics["warnings"])
        pages_without_text = [p["page"] for p in page_checks if p["blocks"] == 0]
        if pages_without_text:
            warnings.append(f"No searchable OCR text on pages {pages_without_text}")
        qa = {
            "schema_version": 1,
            "status": "warning" if warnings else "passed",
            "errors": [],
            "warnings": warnings,
            "metrics": {**metrics, "rendered_pages": len(rendered)},
            "pages": page_checks,
        }
        manifest = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "title": reader_title,
            "source_pdf": source.name,
            "source_pdf_sha256": _sha256(source),
            "lines_json_sha256": _sha256(lines_path),
            "dpi": dpi,
            "jpeg_quality": quality,
            "page_count": source_pages,
            "ocr_line_count": metrics["ocr_lines"],
            "selectable_characters": metrics["selectable_characters"],
            "corrections_applied": len(corrections),
            "semantic_checks_passed": len(semantic_checks),
            "outline_entries": len(outline),
            "toc_links": len(toc_links),
            "exercise_entries": len(exercises),
            "answer_entries": len(answers),
            "reader_performance": {
                "virtualized": True,
                "maximum_mounted_pages": 7,
                "search_index": "normalized_page_text+structured_exercises+structured_answers",
            },
        }
        if corrections_path:
            manifest["corrections_sha256"] = _sha256(corrections_path.resolve())
        if checks_path:
            manifest["checks_sha256"] = _sha256(checks_path.resolve())
        qa["corrections"] = corrections
        qa["semantic_checks"] = semantic_checks
        exercise_aliases_by_page: dict[int, list[str]] = {}
        for item in exercises:
            chapter_alias = f" kapitel {item['chapter']}" if item["chapter"] is not None else ""
            exercise_aliases_by_page.setdefault(item["page"], []).append(
                f"{item['label']}{chapter_alias} uppgift {item['number']}"
            )
        for item in answers:
            chapter_alias = f" kapitel {item['chapter']}" if item["chapter"] is not None else ""
            exercise_aliases_by_page.setdefault(item["page"], []).append(
                f"{item['label']}{chapter_alias} {item['kind']} uppgift {item['number']}"
            )
        for page in data["pages"]:
            exercise_aliases = " ".join(exercise_aliases_by_page.get(page["page"], []))
            page["search"] = _normalize_search(
                " ".join(f"{block['text']} {block['type']}" for block in page["blocks"])
                + " "
                + exercise_aliases
            )
        data.pop("blocks", None)
        data["build"] = {"manifest": manifest, "qa": qa}
        if standalone:
            for page, image_path in zip(data["pages"], rendered, strict=True):
                payload = base64.b64encode(image_path.read_bytes()).decode("ascii")
                page["image"] = f"data:image/jpeg;base64,{payload}"
            temporary_html = Path(temp_name) / "reader.html"
            temporary_html.write_text(_reader_html(data), encoding="utf-8")
            if output_dir.exists():
                if output_dir.is_dir():
                    shutil.rmtree(output_dir)
                else:
                    output_dir.unlink()
            temporary_html.replace(output_dir)
        else:
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "index.html").write_text(_reader_html(data), encoding="utf-8")
            (staging / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            (staging / "qa.json").write_text(
                json.dumps(qa, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            if output_dir.exists():
                if output_dir.is_dir():
                    shutil.rmtree(output_dir)
                else:
                    output_dir.unlink()
            staging.replace(output_dir)

    zip_path = output_dir.with_suffix(".zip")
    if make_zip and not standalone:
        temporary_zip = zip_path.with_suffix(".zip.part")
        with zipfile.ZipFile(temporary_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(output_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, Path(output_dir.name) / path.relative_to(output_dir))
        temporary_zip.replace(zip_path)
    return {
        "output_dir": None if standalone else str(output_dir),
        "index": str(output_dir if standalone else output_dir / "index.html"),
        "zip": str(zip_path) if make_zip and not standalone else None,
        "standalone": standalone,
        "bytes": output_dir.stat().st_size if standalone else None,
        "manifest": manifest,
        "qa": qa,
    }


READER_TEMPLATE = r'''<!--
PDF++ standalone reader/wrapper
Author: Isac Carlsson
Copyright © 2026 Isac Carlsson
Initial release: 2026-09-16
This file generated: __READER_BUILD_DATE__

The reader/wrapper code in this file, including its HTML, CSS, JavaScript, UI, and reader logic, is licensed under the WTFPL v2.

This license DOES NOT apply to the embedded document or any document-derived content, including page images, text/OCR data, metadata, fonts, or other content. Such material remains subject to its original copyright and licensing terms. The wrapper author does not provide, license, or claim ownership of that content and is not responsible for its selection, legality, use, or distribution.

The reader/wrapper is provided "AS IS", without warranty of any kind. To the maximum extent permitted by applicable law, its author shall not be liable for any claim, loss, damage, or other liability arising from the reader/wrapper or any embedded content.

DO WHAT THE FUCK YOU WANT TO PUBLIC LICENSE
Version 2, December 2004

Copyright (C) 2004 Sam Hocevar sam@hocevar.net

Everyone is permitted to copy and distribute verbatim or modified
copies of this license document, and changing it is allowed as long
as the name is changed.

DO WHAT THE FUCK YOU WANT TO PUBLIC LICENSE
TERMS AND CONDITIONS FOR COPYING, DISTRIBUTION AND MODIFICATION

0. You just DO WHAT THE FUCK YOU WANT TO.
-->
<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="description" content="Sökbar originaltrogen kursläsare">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline';">
  <title>__READER_TITLE__</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='6' fill='%231b2a35'/%3E%3Cpath d='M8 6h12l4 4v16H8z' fill='%23f4efe2'/%3E%3Cpath d='M20 6v5h5M11 16h10M11 20h8' fill='none' stroke='%23c9973d' stroke-width='2'/%3E%3C/svg%3E">
  <style>
    :root { color-scheme:light; --ink:#17242d; --muted:#65727a; --paper:#fffefa; --chrome:#f5f7f7; --line:#cbd3d6; --accent:#986516; --page-width:900px; --page-gap:18px; }
    * { box-sizing:border-box; }
    html { -webkit-text-size-adjust:100%; text-size-adjust:100%; }
    body { margin:0; overflow-x:auto; background:#cbd2d5; color:var(--ink); font:16px/1.45 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    button,input { color:inherit; font:inherit; }
    button { min-width:40px; min-height:40px; touch-action:manipulation; }
    .search-ui { position:fixed; z-index:40; top:calc(10px + env(safe-area-inset-top,0px)); left:calc(10px + env(safe-area-inset-left,0px)); width:40px; opacity:.62; transition:width .16s ease,opacity .14s ease; }
    .search-ui:hover,.search-ui:focus-within,.search-ui.expanded,.search-ui.has-results { opacity:.98; }
    .search-ui.expanded,.search-ui.has-results { width:min(370px,calc(100vw - 260px)); }
    .search-wrap { position:relative; height:40px; }
    .search-toggle { position:absolute; z-index:1; inset:0 auto 0 0; width:40px; height:40px; padding:0; display:grid; place-items:center; border:1px solid rgba(98,113,121,.62); border-radius:50%; background:rgba(250,252,252,.9); box-shadow:0 2px 10px rgba(17,31,39,.12); }
    .search-toggle svg { width:18px; height:18px; fill:none; stroke:currentColor; stroke-width:2; }
    #search { width:40px; height:40px; padding:8px 38px 8px 40px; border:1px solid transparent; border-radius:20px; background:transparent; opacity:0; pointer-events:none; transition:width .16s ease,opacity .12s ease,background .12s ease; }
    .search-ui.expanded #search,.search-ui.has-results #search { width:100%; border-color:rgba(98,113,121,.62); background:rgba(250,252,252,.94); box-shadow:0 2px 10px rgba(17,31,39,.12); opacity:1; pointer-events:auto; }
    .search-ui.expanded .search-toggle,.search-ui.has-results .search-toggle { border-color:transparent; background:transparent; box-shadow:none; }
    #search:focus { border-color:var(--accent); outline:3px solid rgba(152,101,22,.22); }
    #clear { position:absolute; right:1px; top:0; width:38px; height:40px; border:0; background:transparent; border-radius:8px; font-size:21px; opacity:0; pointer-events:none; }
    .search-ui.expanded #clear,.search-ui.has-results #clear { opacity:1; pointer-events:auto; }
    .search-results { max-height:min(48vh,430px); margin-top:5px; overflow:auto; border:1px solid rgba(98,113,121,.6); border-radius:9px; background:rgba(250,252,252,.98); box-shadow:0 8px 22px rgba(17,31,39,.18); }
    .search-results[hidden] { display:none; }
    .results-head { position:sticky; z-index:1; top:0; padding:8px 11px; border-bottom:1px solid var(--line); background:rgba(245,247,247,.98); color:var(--muted); font-size:13px; }
    .result { display:block; width:100%; padding:9px 11px; border:0; border-bottom:1px solid #dbe1e3; background:transparent; color:inherit; text-align:left; text-decoration:none; }
    .result:hover,.result:focus-visible { background:#fff8e8; }
    .result-meta { display:block; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
    .result-text { display:block; margin-top:3px; overflow-wrap:anywhere; }
    .empty { margin:0; padding:11px; color:var(--muted); }
    .floating-controls { position:fixed; z-index:42; top:calc(10px + env(safe-area-inset-top,0px)); right:calc(10px + env(safe-area-inset-right,0px)); display:flex; align-items:center; gap:6px; }
    .page-control { position:fixed; z-index:42; right:calc(12px + env(safe-area-inset-right,0px)); bottom:calc(12px + env(safe-area-inset-bottom,0px)); display:flex; height:36px; align-items:center; gap:4px; padding:0 3px; border:0; border-radius:7px; background:transparent; color:#7b838b; font-variant-numeric:tabular-nums; transition:color .12s ease; }
    .page-control:hover,.page-control:focus-within { color:#505a63; }
    #pageNumber { width:34px; height:28px; padding:2px 1px; border:0; border-bottom:2px solid #98a0a7; border-radius:0; background:transparent; color:#59626a; text-align:center; font-weight:650; appearance:textfield; -moz-appearance:textfield; }
    #pageNumber:focus { border-bottom-color:#626d76; outline:none; }
    #pageNumber::-webkit-inner-spin-button,#pageNumber::-webkit-outer-spin-button { margin:0; -webkit-appearance:none; }
    .ocr-toggle,.spread-toggle,.info-toggle { position:relative; width:42px; height:36px; padding:0; display:grid; place-items:center; border:1px solid rgba(255,255,255,.85); border-radius:9px; background:linear-gradient(145deg,rgba(255,255,255,.96),rgba(237,241,244,.93)); box-shadow:0 4px 10px rgba(38,49,58,.13),inset 0 1px 0 rgba(255,255,255,.98); color:#626c74; transition:transform .12s ease,box-shadow .12s ease,background .12s ease,color .12s ease; }
    .ocr-toggle:hover,.spread-toggle:hover,.info-toggle:hover { color:#3f4951; }
    .ocr-toggle:active,.spread-toggle:active,.info-toggle:active { transform:translateY(1px); box-shadow:0 2px 5px rgba(38,49,58,.12),inset 0 1px 3px rgba(56,68,78,.1); }
    .ocr-toggle::after,.spread-toggle::after { content:""; position:absolute; left:50%; bottom:3px; width:12px; height:2px; border-radius:2px; background:transparent; transform:translateX(-50%); }
    .ocr-icon { display:flex; width:100%; height:100%; align-items:center; justify-content:center; font-size:9px; line-height:1; font-weight:900; letter-spacing:.03em; }
    .ocr-toggle[aria-pressed="true"],.spread-toggle[aria-pressed="true"] { border-color:#697681; background:linear-gradient(145deg,#76838d,#5f6c76); color:white; box-shadow:0 2px 6px rgba(38,49,58,.2),inset 0 1px 2px rgba(20,29,36,.16); }
    .ocr-toggle[aria-pressed="true"]::after,.spread-toggle[aria-pressed="true"]::after { background:#dce3e8; }
    .spread-icon { display:block; width:20px; height:18px; overflow:visible; }
    .spread-icon rect { fill:none; stroke:currentColor; stroke-width:1.5; }
    .info-icon { display:grid; width:18px; height:18px; place-items:center; border:1.5px solid currentColor; border-radius:50%; font:700 13px/1 Georgia,serif; }
    .info-panel { position:fixed; z-index:41; top:calc(52px + env(safe-area-inset-top,0px)); right:calc(10px + env(safe-area-inset-right,0px)); width:min(340px,calc(100vw - 20px)); max-height:min(70vh,520px); overflow:auto; padding:15px 16px; border:1px solid rgba(98,113,121,.58); border-radius:10px; background:rgba(250,252,252,.98); box-shadow:0 9px 26px rgba(17,31,39,.2); color:#35434c; font-size:14px; line-height:1.48; }
    .info-panel[hidden] { display:none; }
    .info-panel h2 { margin:0 0 8px; color:var(--ink); font-size:16px; }
    .info-panel p { margin:8px 0; }
    .info-panel ul { margin:8px 0; padding-left:20px; }
    .info-panel a { color:#6f490f; font-weight:650; }
    .viewer { width:100%; min-height:100vh; min-height:100dvh; padding:12px max(10px,env(safe-area-inset-left,0px)) calc(50px + env(safe-area-inset-bottom,0px)); touch-action:pan-x pan-y pinch-zoom; overflow-anchor:none; }
    .spacer { width:1px; pointer-events:none; }
    .page-shell { position:relative; width:var(--page-width); max-width:none; margin:0 auto var(--page-gap); background:var(--paper); box-shadow:0 8px 26px rgba(17,31,39,.2); contain:layout paint; }
    body.two-page #pageWindow { display:grid; grid-template-columns:repeat(2,var(--page-width)); justify-content:center; gap:var(--page-gap); }
    body.two-page .page-shell { margin:0; }
    .page-image { display:block; width:100%; height:100%; max-width:none; object-fit:contain; user-select:none; -webkit-user-drag:none; }
    .text-layer { position:absolute; inset:0; overflow:clip; pointer-events:none; text-align:initial; line-height:1; text-size-adjust:none; forced-color-adjust:none; transform-origin:0 0; }
    .ocr-line { position:absolute; display:inline-block; width:max-content; color:transparent; white-space:pre; line-height:1; font-family:"Times New Roman",Times,serif; transform-origin:left top; cursor:text; user-select:text; -webkit-user-select:text; pointer-events:auto; border:1px solid transparent; }
    .ocr-line::selection { color:transparent; background:rgba(30,112,175,.28); }
    .end-of-content { display:block; position:absolute; inset:100% 0 0; z-index:0; cursor:default; user-select:none; -webkit-user-select:none; }
    .text-layer.selecting .end-of-content { top:0; }
    .ocr-line.hit { background:rgba(255,215,65,.42); border-color:rgba(152,101,22,.7); }
    .ocr-line.flash { animation:search-flash 1.4s ease-out; }
    .ocr-line.toc-link { cursor:pointer; text-decoration:none; }
    .ocr-line.toc-link:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
    body.show-zones .ocr-line { background:rgba(45,135,172,.08); border-color:rgba(45,135,172,.3); }
    body.show-zones .ocr-line[data-kind="formula"] { background:rgba(190,119,32,.1); border-color:rgba(152,101,22,.42); }
    @keyframes search-flash { 0%,100% { background:transparent; } 20%,72% { background:rgba(255,209,43,.58); border-color:var(--accent); } }
    @media (prefers-reduced-motion:reduce) { .search-ui { transition:none; } .page-control { transition:none; } .ocr-line.flash { animation:none; background:rgba(255,209,43,.58); border-color:var(--accent); } }
    @media (forced-colors:active) { .ocr-line.hit,.ocr-line.flash,body.show-zones .ocr-line { outline:2px solid Highlight; } }
    @media (max-width:620px) {
      .search-ui,.ocr-toggle,.spread-toggle { display:none!important; }
      .floating-controls { top:6px; right:6px; }
      .info-panel { top:calc(48px + env(safe-area-inset-top,0px)); right:calc(6px + env(safe-area-inset-right,0px)); }
      .page-control { top:calc(6px + env(safe-area-inset-top,0px)); right:calc(6px + env(safe-area-inset-right,0px)); bottom:auto; height:30px; gap:2px; padding:0 2px; color:#68727a; font-size:13px; opacity:0; pointer-events:none; transition:opacity .2s ease,color .12s ease; }
      .page-control.mobile-visible,.page-control:focus-within { opacity:var(--mobile-pager-opacity,1); pointer-events:auto; }
      #pageNumber { width:28px; height:26px; padding:1px 0; border-bottom-width:1px; font-size:13px; }
      .viewer { padding-inline:6px; }
    }
    @page { margin:0; size:auto; }
    @media print { html,body { width:100%; background:white!important; print-color-adjust:exact; -webkit-print-color-adjust:exact; } .search-ui,.floating-controls,.info-panel { display:none!important; } .viewer { width:100%; padding:0; } .spacer { display:none; } #pageWindow,body.two-page #pageWindow { display:block; } .page-shell { width:min(calc(100vw - 2px),calc((100vh - 2px)*var(--page-ratio)))!important; height:min(calc(100vh - 2px),calc((100vw - 2px)*var(--page-inverse-ratio)))!important; aspect-ratio:var(--page-ratio)!important; margin:0 auto!important; box-shadow:none; contain:none; overflow:hidden; break-inside:avoid; break-after:page; page-break-after:always; } .page-shell:last-child { break-after:auto; page-break-after:auto; } .page-image { width:100%!important; height:100%!important; object-fit:fill; } .text-layer { display:block!important; overflow:hidden; } .end-of-content { display:none!important; } .ocr-line { color:rgba(0,0,0,.012)!important; -webkit-text-fill-color:rgba(0,0,0,.012)!important; background:none!important; border-color:transparent!important; animation:none!important; } }
  </style>
</head>
<body>
  <div id="searchUI" class="search-ui">
    <div class="search-wrap"><button id="searchToggle" class="search-toggle" aria-label="Öppna sökning" aria-expanded="false"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"></circle><path d="m15.5 15.5 5 5"></path></svg></button><input id="search" type="search" autocomplete="off" aria-label="Sök i dokumentet" placeholder="Sök… t.ex. tp kap 3 14ac"><button id="clear" aria-label="Rensa sökning">×</button></div>
    <div id="searchResults" class="search-results" hidden><div class="results-head"><strong id="resultCount" aria-live="polite">Sökresultat</strong></div><div id="resultsList"></div></div>
  </div>
  <div class="floating-controls">
    <label class="page-control" aria-label="Aktuell sida"><input id="pageNumber" type="text" value="1" inputmode="numeric" pattern="[0-9]*" aria-label="Gå till sida"><span id="pageCount">/ 0</span></label>
    <button id="zones" class="ocr-toggle" aria-pressed="false" aria-label="Visa OCR-zoner" title="Visa OCR-zoner"><span class="ocr-icon" aria-hidden="true">OCR</span></button>
    <button id="spread" class="spread-toggle" aria-pressed="false" aria-label="Visa två sidor" title="Visa två sidor"><svg class="spread-icon" viewBox="0 0 22 18" aria-hidden="true"><rect x="1.5" y="1.5" width="8" height="14" rx="1.5"></rect><rect x="12.5" y="3" width="8" height="14" rx="1.5"></rect></svg></button>
    <button id="info" class="info-toggle" aria-expanded="false" aria-controls="infoPanel" aria-label="Om läsaren" title="Om läsaren"><span class="info-icon" aria-hidden="true">i</span></button>
  </div>
  <aside id="infoPanel" class="info-panel" hidden>
    <h2>Om läsaren</h2>
    <p>Det här är en fristående PDF++-läsare. Originalsidorna visas oförändrade, med ett osynligt textlager från Mathpix OCR ovanpå.</p>
    <ul><li>Sök med förstoringsglaset eller Ctrl/Cmd+F.</li><li>Markera och kopiera text och formler.</li><li>Klicka på poster i dokumentets innehållsförteckning.</li><li>Sök strukturerat, exempelvis <code>tp kap 3 14ac</code> eller <code>svar tp kap 3 14</code>.</li><li>Använd OCR-knappen för att kontrollera textzoner och tvåsideknappen för bokuppslag.</li></ul>
    <p>Text, formler, rubriker och länkar är maskinlästa. OCR kan vara fel, särskilt i formler, små tecken och slitna skanningar. Kontrollera alltid mot den synliga originalsidan.</p>
    <p><a href="https://github.com/issssse/pdf-plus-reader" target="_blank" rel="noopener noreferrer">Källkod och verktyg för att bygga en egen PDF++-läsare</a>.</p>
  </aside>
  <main><div id="viewer" class="viewer" aria-label="Dokumentsidor"><div id="topSpacer" class="spacer"></div><div id="pageWindow"></div><div id="bottomSpacer" class="spacer"></div></div></main>
  <script id="reader-data" type="application/json">__READER_DATA__</script>
  <script>
  (() => {
    'use strict';
    const dataElement = document.getElementById('reader-data');
    const data = JSON.parse(dataElement.textContent);
    dataElement.remove();
    const pages = data.pages;
    const viewer = document.getElementById('viewer');
    const pageWindow = document.getElementById('pageWindow');
    const topSpacer = document.getElementById('topSpacer');
    const bottomSpacer = document.getElementById('bottomSpacer');
    const searchUI = document.getElementById('searchUI');
    const searchResults = document.getElementById('searchResults');
    const list = document.getElementById('resultsList');
    const search = document.getElementById('search');
    const searchToggle = document.getElementById('searchToggle');
    const pageControl = document.querySelector('.page-control');
    const pageNumber = document.getElementById('pageNumber');
    const spreadButton = document.getElementById('spread');
    const infoButton = document.getElementById('info');
    const infoPanel = document.getElementById('infoPanel');
    const mounted = new Map();
    const elementByBlock = new Map();
    const pendingHits = new Set();
    const PAGE_GAP = 18;
    const WINDOW_RADIUS = 3;
    const RESULT_LIMIT = 150;
    let offsets = [];
    let pageHeights = [];
    let rowOffsets = [];
    let rowStarts = [];
    let pageRows = [];
    let totalHeight = 0;
    let pageWidth = 900;
    let currentPage = 1;
    let zoomScale = 1;
    let scrollFrame = 0;
    let zoomFrame = 0;
    let pendingZoomFactor = 1;
    let zoomPoint = {x:0,y:0};
    let resizeTimer = 0;
    let pagerHideTimer = 0;
    let pagerScrollDistance = 0;
    let pagerLastScrollY = window.scrollY;
    let pagerLastScrollAt = 0;
    let pendingBlockJump = null;
    let blockJumpTimer = 0;
    let printing = false;
    let twoPage = false;
    const mobilePagerQuery = window.matchMedia ? window.matchMedia('(max-width:620px)') : null;

    const esc = value => String(value || '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    const norm = value => { let text=String(value || '').toLowerCase(); if (text.normalize) text=text.normalize('NFKD'); return text.replace(/[\u0300-\u036f]/g,'').replace(/\\/g,'').replace(/[{}_^$]/g,' ').replace(/\s+/g,' ').trim(); };
    const short = (value, n=170) => { const s=String(value || '').replace(/\s+/g,' ').trim(); return s.length>n ? s.slice(0,n-1)+'…' : s; };
    const pct = value => `${value * 100}%`;
    const smooth = () => window.matchMedia && !window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    function makePage(index) {
      const page = pages[index];
      const shell = document.createElement('section');
      shell.className = 'page-shell'; shell.id = `page-${page.page}`; shell.dataset.page = String(page.page); shell.style.height = `${pageHeights[index]}px`;
      shell.style.setProperty('--page-ratio',String(page.image_width/page.image_height)); shell.style.setProperty('--page-inverse-ratio',String(page.image_height/page.image_width));
      const image = document.createElement('img');
      image.className = 'page-image'; image.src = page.image; image.alt = `Sida ${page.page}`; image.loading = 'eager'; image.decoding = 'async';
      image.width = page.image_width; image.height = page.image_height;
      shell.style.aspectRatio = `${page.image_width} / ${page.image_height}`;
      const layer = document.createElement('div'); layer.className = 'text-layer'; layer.setAttribute('aria-label', `OCR-text sida ${page.page}`);
      for (const block of page.blocks) {
        const span = document.createElement(block.toc?'a':'span');
        span.className = 'ocr-line'; span.dataset.id = block.id; span.dataset.kind = block.type;
        if (block.toc) { span.classList.add('toc-link'); span.href=pageReference(block.toc.page,block.toc.block?{id:block.toc.block}:null); span.dataset.targetPage=String(block.toc.page); span.dataset.targetBlock=block.toc.block||''; span.setAttribute('aria-label',`${block.copy}, sida ${block.toc.page}`); }
        span.dataset.width = String(block.bbox.width); span.dataset.height = String(block.bbox.height);
        span.style.left = pct(block.bbox.left); span.style.top = pct(block.bbox.top);
        span.style.fontSize = `${Math.max(4, block.bbox.height * pageHeights[index] * .78)}px`;
        span.textContent = block.copy;
        if (pendingHits.has(block.id)) span.classList.add('hit');
        layer.appendChild(span); elementByBlock.set(block.id, span);
      }
      const end=document.createElement('div'); end.className='end-of-content'; layer.appendChild(end);
      shell.append(image, layer);
      return shell;
    }

    function fitTextLayer(shell,immediate=false) {
      const fit=()=>{
        if (!shell.isConnected) return;
        for (const line of shell.querySelectorAll('.ocr-line')) {
          line.style.transform='none';
          line.style.fontSize=`${Math.max(4,Number(line.dataset.height)*shell.clientHeight*.78)}px`;
          const natural=line.getBoundingClientRect().width;
          const target=Number(line.dataset.width)*shell.clientWidth;
          if (natural>0&&target>0) line.style.transform=`scaleX(${Math.max(.35,Math.min(3.5,target/natural))})`;
        }
      };
      if (immediate) fit(); else requestAnimationFrame(fit);
    }

    function mountRange(start, end) {
      if (twoPage&&!printing) {
        start=Math.floor(start/2)*2;
        end=Math.min(pages.length-1,Math.floor(end/2)*2+1);
      }
      const desired = new Set();
      const fragment = document.createDocumentFragment();
      for (let index=start; index<=end; index += 1) {
        desired.add(index);
        let shell = mounted.get(index);
        if (!shell) {
          shell = makePage(index);
          mounted.set(index, shell);
        }
        fragment.appendChild(shell);
      }
      for (const [index, shell] of mounted) {
        if (!desired.has(index)) {
          for (const block of pages[index].blocks) elementByBlock.delete(block.id);
          shell.remove(); mounted.delete(index);
        }
      }
      pageWindow.replaceChildren(fragment);
      const firstRow=pageRows[start]||0; const lastRow=pageRows[end]||0;
      topSpacer.style.height = `${rowOffsets[firstRow]||0}px`;
      bottomSpacer.style.height = `${Math.max(0,totalHeight-(rowOffsets[lastRow+1]||totalHeight))}px`;
      for (const shell of mounted.values()) fitTextLayer(shell);
    }

    function mountWindow(page) {
      if (printing) return;
      const center = Math.max(0, Math.min(pages.length-1, page-1));
      mountRange(Math.max(0,center-WINDOW_RADIUS), Math.min(pages.length-1,center+WINDOW_RADIUS));
    }

    function recalculateGeometry(preservePosition) {
      const columns=twoPage&&!printing?2:1;
      const viewportWidth=document.documentElement.clientWidth;
      const fitWidth=columns===2
        ? Math.min(720,Math.max(140,(viewportWidth-PAGE_GAP*3)/2))
        : Math.min(960,Math.max(160,viewportWidth-20));
      pageWidth = fitWidth*zoomScale;
      document.documentElement.style.setProperty('--page-width', `${pageWidth}px`);
      offsets = []; pageHeights = []; rowOffsets=[0]; rowStarts=[]; pageRows=[];
      for (const page of pages) {
        pageHeights.push(pageWidth * page.image_height / page.image_width);
      }
      for (let index=0; index<pages.length; index+=columns) {
        const row=rowStarts.length; rowStarts.push(index);
        let rowHeight=0;
        for (let column=0; column<columns&&index+column<pages.length; column+=1) {
          const pageIndex=index+column; offsets[pageIndex]=rowOffsets[row]; pageRows[pageIndex]=row;
          rowHeight=Math.max(rowHeight,pageHeights[pageIndex]);
        }
        rowOffsets.push(rowOffsets[row]+rowHeight+PAGE_GAP);
      }
      totalHeight = rowOffsets[rowOffsets.length-1];
      for (const [index,shell] of mounted) {
        shell.style.height = `${pageHeights[index]}px`;
        for (const block of pages[index].blocks) {
          const span = elementByBlock.get(block.id);
          if (span) span.style.fontSize = `${Math.max(4,block.bbox.height*pageHeights[index]*.78)}px`;
        }
        fitTextLayer(shell);
      }
      mountWindow(currentPage);
      if (preservePosition) jumpToPage(currentPage,false);
    }

    function pageAtPosition(position) {
      let low=0, high=rowStarts.length-1;
      while (low<high) { const middle=Math.floor((low+high+1)/2); if (rowOffsets[middle]<=position) low=middle; else high=middle-1; }
      return rowStarts[low]+1;
    }

    function viewerTop() { return viewer.getBoundingClientRect().top + window.scrollY + 12; }

    function syncFromScroll() {
      const position = Math.max(0,window.scrollY-viewerTop()+window.innerHeight*.3);
      const page = pageAtPosition(position);
      if (page!==currentPage) {
        currentPage=page; pageNumber.value=String(page); mountWindow(page);
        if (pendingBlockJump) {
          if (page===pendingBlockJump.page) {
            const block=pendingBlockJump; pendingBlockJump=null; clearTimeout(blockJumpTimer); updateReference(page,block);
          }
        } else updateReference(page);
      }
    }

    function hideMobilePager() {
      if (document.activeElement===pageNumber) { scheduleMobilePagerHide(); return; }
      pagerScrollDistance=0;
      pageControl.classList.remove('mobile-visible');
      pageControl.style.removeProperty('--mobile-pager-opacity');
    }

    function scheduleMobilePagerHide() {
      clearTimeout(pagerHideTimer);
      pagerHideTimer=setTimeout(hideMobilePager,1050);
    }

    function trackMobilePagerScroll() {
      const y=window.scrollY;
      if (!mobilePagerQuery?.matches) { pagerLastScrollY=y; return; }
      const now=performance.now();
      const delta=Math.abs(y-pagerLastScrollY);
      pagerLastScrollY=y;
      if (!delta) return;
      if (now-pagerLastScrollAt>240) pagerScrollDistance=0;
      pagerLastScrollAt=now;
      pagerScrollDistance=Math.min(150,pagerScrollDistance+delta);
      const progress=Math.max(0,Math.min(1,(pagerScrollDistance-32)/88));
      if (progress>0) {
        pageControl.classList.add('mobile-visible');
        pageControl.style.setProperty('--mobile-pager-opacity',String(.18+.82*progress));
      }
      scheduleMobilePagerHide();
    }

    function resetMobilePager() {
      clearTimeout(pagerHideTimer);
      pagerScrollDistance=0;
      pagerLastScrollY=window.scrollY;
      pageControl.classList.remove('mobile-visible');
      pageControl.style.removeProperty('--mobile-pager-opacity');
    }

    function pageReference(page,block=null) { return `#page=${page}${block?`&block=${encodeURIComponent(block.id)}`:''}`; }

    function updateReference(page,block=null,push=false) {
      const exercise=block?.exercise||block?.answer;
      const section=exercise?`${exercise.label} ${exercise.number}${exercise.chapter?` · Kapitel ${exercise.chapter}`:''}`:pages[page-1]?.section;
      document.title=section&&section!==data.title?`${section} — ${data.title}`:`${data.title} — sida ${page}`;
      try { history[push?'pushState':'replaceState'](null,'',pageReference(page,block)); } catch { /* file URL fallback */ }
    }

    function jumpToPage(page,animate=true,updateHash=false) {
      const target=Math.max(1,Math.min(pages.length,Number(page)||1));
      const originPage=currentPage;
      pendingBlockJump=null; clearTimeout(blockJumpTimer);
      currentPage=target; pageNumber.value=String(target); mountWindow(target);
      const nearby=Math.abs(target-originPage)<=WINDOW_RADIUS;
      window.scrollTo({top:Math.max(0,viewerTop()+offsets[target-1]),behavior:animate&&nearby&&smooth()?'smooth':'auto'});
      if (updateHash) updateReference(target,null,true); else updateReference(target);
    }

    function applyZoomAt(clientX,clientY,factor) {
      const next=Math.max(.5,Math.min(4,zoomScale*factor));
      if (Math.abs(next-zoomScale)<.001) return;
      const position=Math.max(0,window.scrollY+clientY-viewerTop());
      const anchorPage=pageAtPosition(position);
      const index=anchorPage-1;
      const within=Math.max(0,Math.min(1,(position-offsets[index])/Math.max(1,pageHeights[index])));
      const shell=mounted.get(index);
      const oldRect=shell?.getBoundingClientRect();
      const across=oldRect ? Math.max(0,Math.min(1,(clientX-oldRect.left)/Math.max(1,oldRect.width))) : .5;
      zoomScale=next;
      recalculateGeometry(false);
      const newShell=mounted.get(index);
      const newRect=newShell?.getBoundingClientRect();
      const left=window.scrollX+(newRect ? newRect.left+across*newRect.width-clientX : 0);
      const top=viewerTop()+offsets[index]+within*pageHeights[index]-clientY;
      window.scrollTo({left:Math.max(0,left),top:Math.max(0,top),behavior:'auto'});
      currentPage=anchorPage; pageNumber.value=String(anchorPage);
    }

    function queueWheelZoom(event) {
      if (!event.ctrlKey&&!event.metaKey) return;
      event.preventDefault();
      zoomPoint={x:event.clientX,y:event.clientY};
      pendingZoomFactor*=Math.exp(-Math.max(-100,Math.min(100,event.deltaY))*.004);
      cancelAnimationFrame(zoomFrame);
      zoomFrame=requestAnimationFrame(()=>{ const factor=pendingZoomFactor; pendingZoomFactor=1; applyZoomAt(zoomPoint.x,zoomPoint.y,factor); });
    }

    function parseExerciseQuery(value) {
      let query=norm(value).replace(/[.,:;()[\]]/g,' ').replace(/\s+/g,' ').trim();
      const answer=/\b(svar|facit|losning|losningar|answer|answers|solution|solutions)\b/.test(query);
      if (answer) query=query.replace(/\b(svar|facit|losning|losningar|answer|answers|solution|solutions)\b/g,' ');
      let kind=null; let label='';
      if (/\b(testproblem|tp)\b/.test(query)) { kind='testproblem'; label='Testproblem'; query=query.replace(/\b(testproblem|tp)\b/g,' '); }
      else if (/\b(ovningar|ovning|ov|exercises|exercise|problems|problem|uppgifter|uppgift)\b/.test(query)) { kind='exercise'; label='Övningar'; query=query.replace(/\b(ovningar|ovning|ov|exercises|exercise|problems|problem|uppgifter|uppgift)\b/g,' '); }
      if (!kind&&!answer) return null;
      let chapter=null;
      const chapterMatch=query.match(/\b(?:kap|kapitel|chapter|ch)\s*(\d{1,3})\b/);
      if (chapterMatch) { chapter=Number(chapterMatch[1]); query=query.replace(chapterMatch[0],' '); }
      query=query.replace(/\s+/g,' ').trim();
      let requests=[...query.matchAll(/\b(\d{1,3})\s*([a-z]{1,8})?\b/g)].map(match=>({number:Number(match[1]),parts:match[2]||''}));
      if (chapter===null&&requests.length>1) chapter=requests.shift().number;
      if (chapter===null&&!requests.length) return null;
      return {kind,label,chapter,requests,answer};
    }

    function exerciseMatches(query) {
      const parsed=parseExerciseQuery(query);
      if (!parsed) return null;
      const collection=parsed.answer?data.answers:data.exercises;
      if (!Array.isArray(collection)) return [];
      const requested=new Map(parsed.requests.map((item,index)=>[item.number,{...item,index}]));
      let records=collection.filter(item=>(!parsed.kind||item.kind===parsed.kind||(parsed.answer&&item.kind==='problem'))&&(parsed.chapter===null||item.chapter===parsed.chapter));
      if (requested.size) records=records.filter(item=>requested.has(item.number));
      records.sort((a,b)=>(requested.get(a.number)?.index??a.number)-(requested.get(b.number)?.index??b.number)||a.page-b.page);
      return records.slice(0,RESULT_LIMIT).map(record=>{
        const block=pages[record.page-1]?.blocks.find(item=>item.id===record.block);
        return block?{block,exercise:record,answer:parsed.answer,request:requested.get(record.number)||null}:null;
      }).filter(Boolean);
    }

    function scoreBlocks(query) {
      const indexed=exerciseMatches(query); if (indexed) return indexed;
      const q=norm(query); if (!q) return [];
      const terms=q.split(' ').filter(Boolean); const scored=[];
      for (const page of pages) {
        if (!page.search.includes(q) && !terms.every(term=>page.search.includes(term))) continue;
        for (const block of page.blocks) {
          const hay=norm(`${block.text} ${block.type}`); let score=hay.includes(q)?100:0;
          for (const term of terms) if (hay.includes(term)) score+=8;
          if (block.type==='formula'&&score) score+=12;
          if (score) scored.push({block,score});
        }
        if (scored.length>=RESULT_LIMIT*3) break;
      }
      scored.sort((a,b)=>b.score-a.score||a.block.page-b.block.page);
      return scored.slice(0,RESULT_LIMIT).map(item=>({block:item.block,exercise:null,request:null}));
    }

    function renderSearch() {
      pendingHits.clear(); for (const element of elementByBlock.values()) element.classList.remove('hit');
      const query=search.value.trim();
      if (!query) { searchResults.hidden=true; searchUI.classList.remove('has-results'); list.replaceChildren(); return; }
      const found=scoreBlocks(query); searchResults.hidden=false; searchUI.classList.add('has-results');
      document.getElementById('resultCount').textContent = found.length===RESULT_LIMIT ? `${RESULT_LIMIT}+ träffar` : `${found.length} träff${found.length===1?'':'ar'}`;
      if (!found.length) { list.innerHTML='<p class="empty">Inga träffar.</p>'; return; }
      for (const hit of found) {
        const block=hit.block;
        pendingHits.add(block.id);
        const element=elementByBlock.get(block.id);
        if (element) element.classList.add('hit');
      }
      list.innerHTML=found.map(hit=>{ const block=hit.block; const page=pages[block.page-1]; const section=page.section&&page.section!==data.title?`${page.section} · `:''; const parts=hit.request?.parts?[...hit.request.parts].join(', '):''; const exerciseMeta=hit.exercise?`${hit.answer?hit.exercise.label:hit.exercise.label}${hit.exercise.chapter?` · Kapitel ${hit.exercise.chapter}`:''} · Uppgift ${hit.exercise.number}${parts?` (${parts})`:''}`:`${section}Sida ${block.page} · ${block.type}`; return `<a class="result" href="${pageReference(block.page,block)}" data-id="${esc(block.id)}" data-page="${block.page}"><span class="result-meta">${esc(exerciseMeta)}${hit.exercise?` · Sida ${block.page}`:''}</span><span class="result-text">${esc(short(block.copy))}</span></a>`; }).join('');
      list.querySelectorAll('.result').forEach(link => link.addEventListener('click', event => {
        if (event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey) return;
        event.preventDefault();
        const page=pages[Number(link.dataset.page)-1]; const block=page.blocks.find(item=>item.id===link.dataset.id);
        if (block) jumpToBlock(block,true);
      }));
    }

    function jumpToBlock(block,push=false) {
      const originPage=currentPage;
      pendingBlockJump=originPage===block.page?null:block;
      clearTimeout(blockJumpTimer);
      if (pendingBlockJump) blockJumpTimer=setTimeout(()=>{ if (!pendingBlockJump) return; pendingBlockJump=null; updateReference(currentPage); },2000);
      currentPage=block.page; pageNumber.value=String(block.page); mountWindow(block.page);
      const top=viewerTop()+offsets[block.page-1]+block.bbox.top*pageHeights[block.page-1]-24;
      const nearby=Math.abs(block.page-originPage)<=WINDOW_RADIUS;
      window.scrollTo({top:Math.max(0,top),behavior:nearby&&smooth()?'smooth':'auto'});
      updateReference(block.page,block,push);
      requestAnimationFrame(()=>{ const target=elementByBlock.get(block.id); if (target) { target.classList.add('hit'); target.classList.remove('flash'); requestAnimationFrame(()=>target.classList.add('flash')); } });
    }

    function expandSearch(focus=true) {
      searchUI.classList.add('expanded'); searchToggle.setAttribute('aria-expanded','true');
      if (focus) requestAnimationFrame(()=>{ search.focus(); search.select(); });
    }

    function collapseSearch() {
      if (search.value.trim()) return;
      searchUI.classList.remove('expanded'); searchToggle.setAttribute('aria-expanded','false');
    }

    function resetSelectionLayer(layer) {
      const end=layer.querySelector('.end-of-content'); if (!end) return;
      layer.appendChild(end); end.style.width=''; end.style.height=''; end.style.userSelect='none'; layer.classList.remove('selecting');
    }

    let selectionPointerDown=false; let previousSelectionRange=null;
    document.addEventListener('pointerdown',()=>{ selectionPointerDown=true; });
    document.addEventListener('pointerup',()=>{ selectionPointerDown=false; document.querySelectorAll('.text-layer').forEach(resetSelectionLayer); });
    window.addEventListener('blur',()=>{ selectionPointerDown=false; document.querySelectorAll('.text-layer').forEach(resetSelectionLayer); });
    document.addEventListener('keyup',()=>{ if (!selectionPointerDown) document.querySelectorAll('.text-layer').forEach(resetSelectionLayer); });
    document.addEventListener('selectionchange',()=>{
      const selection=document.getSelection(); const layers=[...document.querySelectorAll('.text-layer')];
      if (!selection||selection.rangeCount===0) { layers.forEach(resetSelectionLayer); return; }
      const active=new Set();
      for (let i=0;i<selection.rangeCount;i+=1) { const range=selection.getRangeAt(i); for (const layer of layers) { if (!active.has(layer)&&range.intersectsNode(layer)) active.add(layer); } }
      for (const layer of layers) { if (active.has(layer)) layer.classList.add('selecting'); else resetSelectionLayer(layer); }
      const range=selection.getRangeAt(0);
      const modifyStart=previousSelectionRange&&(range.compareBoundaryPoints(Range.END_TO_END,previousSelectionRange)===0||range.compareBoundaryPoints(Range.START_TO_END,previousSelectionRange)===0);
      let anchor=modifyStart?range.startContainer:range.endContainer;
      if (anchor.nodeType===Node.TEXT_NODE) anchor=anchor.parentNode;
      if (!modifyStart&&range.endOffset===0) { while (anchor&&anchor.previousSibling) anchor=anchor.previousSibling; }
      const layer=anchor?.closest?.('.text-layer'); const end=layer?.querySelector('.end-of-content');
      if (layer&&end&&anchor.parentElement===layer) { end.style.width=`${layer.clientWidth}px`; end.style.height=`${layer.clientHeight}px`; end.style.userSelect='text'; layer.insertBefore(end,modifyStart?anchor:anchor.nextSibling); }
      previousSelectionRange=range.cloneRange();
    });

    let searchTimer=0; search.addEventListener('input',()=>{ clearTimeout(searchTimer); searchTimer=setTimeout(renderSearch,80); });
    searchToggle.addEventListener('click',()=>expandSearch(true));
    search.addEventListener('blur',()=>setTimeout(()=>{ if (!searchUI.contains(document.activeElement)) collapseSearch(); },80));
    document.getElementById('clear').addEventListener('click', () => { search.value=''; renderSearch(); search.focus(); });
    const submitPage=()=>{ const value=Number(pageNumber.value.replace(/\D/g,'')); jumpToPage(value||currentPage,true,true); };
    pageNumber.addEventListener('change',submitPage);
    pageNumber.addEventListener('keydown',event=>{ if (event.key==='Enter') { event.preventDefault(); submitPage(); pageNumber.select(); } });
    pageNumber.addEventListener('focus',()=>{ if (mobilePagerQuery?.matches) { pageControl.classList.add('mobile-visible'); pageControl.style.setProperty('--mobile-pager-opacity','1'); clearTimeout(pagerHideTimer); } });
    pageNumber.addEventListener('blur',()=>{ if (mobilePagerQuery?.matches) scheduleMobilePagerHide(); });
    document.getElementById('zones').addEventListener('click', event => { const enabled=document.body.classList.toggle('show-zones'); event.currentTarget.setAttribute('aria-pressed',String(enabled)); });
    spreadButton.addEventListener('click',()=>{ twoPage=!twoPage; document.body.classList.toggle('two-page',twoPage); spreadButton.setAttribute('aria-pressed',String(twoPage)); recalculateGeometry(true); });
    infoButton.addEventListener('click',()=>{ const open=infoPanel.hidden; infoPanel.hidden=!open; infoButton.setAttribute('aria-expanded',String(open)); });
    document.addEventListener('click',event=>{ if (!infoPanel.hidden&&!infoPanel.contains(event.target)&&!infoButton.contains(event.target)) { infoPanel.hidden=true; infoButton.setAttribute('aria-expanded','false'); } });
    viewer.addEventListener('click',event=>{ const link=event.target.closest?.('.toc-link'); if (!link||event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey) return; event.preventDefault(); const targetPage=Number(link.dataset.targetPage); const targetBlock=link.dataset.targetBlock?pages[targetPage-1]?.blocks.find(block=>block.id===link.dataset.targetBlock):null; if (targetBlock) jumpToBlock(targetBlock,true); else jumpToPage(targetPage,true,true); });
    document.addEventListener('keydown',event=>{ if ((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='f') { event.preventDefault(); expandSearch(true); } else if (event.key==='Escape'&&!infoPanel.hidden) { infoPanel.hidden=true; infoButton.setAttribute('aria-expanded','false'); infoButton.focus(); } else if (event.key==='Escape'&&document.activeElement===search) { search.blur(); collapseSearch(); } });
    window.addEventListener('wheel',queueWheelZoom,{passive:false});
    window.addEventListener('scroll',()=>{ if (printing) return; cancelAnimationFrame(scrollFrame); scrollFrame=requestAnimationFrame(()=>{ syncFromScroll(); trackMobilePagerScroll(); }); },{passive:true});
    window.addEventListener('resize',()=>{ clearTimeout(resizeTimer); resizeTimer=setTimeout(()=>recalculateGeometry(true),100); });
    window.addEventListener('orientationchange',()=>{ clearTimeout(resizeTimer); resizeTimer=setTimeout(()=>recalculateGeometry(true),120); });
    if (mobilePagerQuery) { const onPagerBreakpoint=()=>resetMobilePager(); if (mobilePagerQuery.addEventListener) mobilePagerQuery.addEventListener('change',onPagerBreakpoint); else if (mobilePagerQuery.addListener) mobilePagerQuery.addListener(onPagerBreakpoint); }
    function preparePrint() {
      if (printing) return;
      printing=true; mountRange(0,pages.length-1); topSpacer.style.height='0'; bottomSpacer.style.height='0';
      void document.body.offsetHeight;
      for (const shell of mounted.values()) fitTextLayer(shell,true);
    }
    function restoreAfterPrint() { if (!printing) return; printing=false; recalculateGeometry(false); mountWindow(currentPage); jumpToPage(currentPage,false); }
    window.addEventListener('beforeprint',preparePrint);
    window.addEventListener('afterprint',restoreAfterPrint);
    const printMedia=window.matchMedia?window.matchMedia('print'):null;
    if (printMedia) { const onPrintChange=event=>event.matches?preparePrint():restoreAfterPrint(); if (printMedia.addEventListener) printMedia.addEventListener('change',onPrintChange); else if (printMedia.addListener) printMedia.addListener(onPrintChange); }

    function navigateHash() {
      const params=new URLSearchParams(location.hash.slice(1));
      const page=Math.max(1,Math.min(pages.length,Number(params.get('page'))||1));
      const id=params.get('block'); const block=id?pages[page-1].blocks.find(item=>item.id===id):null;
      if (block) jumpToBlock(block,false); else jumpToPage(page,false,false);
    }
    window.addEventListener('popstate',navigateHash);
    document.getElementById('pageCount').textContent=`/ ${pages.length}`;
    recalculateGeometry(false); navigateHash();
  })();
  </script>
</body>
</html>
'''
