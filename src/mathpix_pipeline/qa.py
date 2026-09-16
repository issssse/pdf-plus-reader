from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from rapidfuzz.distance import Levenshtein

from .pdf_tools import (
    content_bbox_fraction,
    extract_text_pages,
    ink_fraction,
    make_contact_sheet,
    page_sizes,
    render_pdf,
)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"\\(?:begin|end)\{[^}]+\}", " ", text)
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def edit_distance(left: list[str] | str, right: list[str] | str) -> int:
    return int(Levenshtein.distance(left, right))


def error_rates(reference: str, candidate: str) -> dict[str, float | int]:
    reference_n = normalize_text(reference)
    candidate_n = normalize_text(candidate)
    chars_ref = reference_n.replace(" ", "")
    chars_candidate = candidate_n.replace(" ", "")
    words_ref = reference_n.split()
    words_candidate = candidate_n.split()
    return {
        "reference_chars": len(chars_ref),
        "candidate_chars": len(chars_candidate),
        "cer": round(edit_distance(chars_ref, chars_candidate) / max(1, len(chars_ref)), 6),
        "wer": round(edit_distance(words_ref, words_candidate) / max(1, len(words_ref)), 6),
    }


def mmd_page_text(mmd: str) -> list[str]:
    # include_page_breaks uses explicit page markers. Keep a conservative fallback.
    parts = re.split(r"\n?\f\n?|\n?\\pagebreak\n?|\n?<!--\s*pagebreak\s*-->\n?", mmd)
    return [part for part in parts if part.strip()] or [mmd]


def compare_to_baseline(report: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Report deltas without collapsing quality into a misleading single score."""
    current = report["outputs"]
    old = baseline.get("outputs", {})
    result: dict[str, Any] = {}
    for name, metrics in current.items():
        old_metrics = old.get(name, {})
        result[name] = {
            "selectable_chars_delta": metrics.get("selectable_chars", 0)
            - old_metrics.get("selectable_chars", 0),
            "blank_pages_delta": metrics.get("blank_pages", 0)
            - old_metrics.get("blank_pages", 0),
            "mmd_pdf_cer_delta": _delta(metrics, old_metrics, "mmd_pdf_consistency", "cer"),
            "ground_truth_cer_delta": _delta(metrics, old_metrics, "ground_truth", "cer"),
        }
    return result


def lines_summary(lines_path: Path | None) -> dict[str, Any] | None:
    if not lines_path or not lines_path.exists():
        return None
    data = json.loads(lines_path.read_text(encoding="utf-8"))
    pages = data.get("pages", [])
    page_summaries = []
    all_confidences: list[float] = []
    ranked_lines: list[dict[str, Any]] = []
    for page in pages:
        lines = page.get("lines", [])
        confidences = [
            float(line["confidence"])
            for line in lines
            if isinstance(line.get("confidence"), (int, float))
        ]
        all_confidences.extend(confidences)
        for line in lines:
            confidence = line.get("confidence")
            if isinstance(confidence, (int, float)):
                ranked_lines.append(
                    {
                        "page": page.get("page"),
                        "line": line.get("line"),
                        "type": line.get("type"),
                        "confidence": round(float(confidence), 6),
                        "confidence_rate": round(float(line.get("confidence_rate", 0)), 6),
                        "text": str(line.get("text", ""))[:240],
                    }
                )
        page_summaries.append(
            {
                "page": page.get("page"),
                "line_count": len(lines),
                "mean_line_confidence": round(sum(confidences) / len(confidences), 6)
                if confidences
                else None,
                "low_confidence_lines_below_0_8": sum(value < 0.8 for value in confidences),
                "languages_detected": page.get("languages_detected", []),
            }
        )
    return {
        "page_count": len(pages),
        "mean_line_confidence": round(sum(all_confidences) / len(all_confidences), 6)
        if all_confidences
        else None,
        "low_confidence_lines_below_0_8": sum(value < 0.8 for value in all_confidences),
        "lowest_confidence_lines": sorted(
            ranked_lines, key=lambda item: item["confidence"]
        )[:10],
        "pages": page_summaries,
    }


def run_benchmark_checks(lines_path: Path | None, checks_path: Path | None) -> dict[str, Any] | None:
    if not checks_path:
        return None
    if not lines_path or not lines_path.exists():
        raise ValueError("--checks requires a Mathpix lines.json file")
    specification = json.loads(checks_path.read_text(encoding="utf-8"))
    data = json.loads(lines_path.read_text(encoding="utf-8"))
    page_text = {
        int(page["page"]): "\n".join(str(line.get("text", "")) for line in page.get("lines", []))
        for page in data.get("pages", [])
    }
    results = []
    for check in specification.get("checks", []):
        page = int(check["page"])
        text = page_text.get(page, "")
        missing = [expected for expected in check.get("contains", []) if expected not in text]
        forbidden_found = [value for value in check.get("not_contains", []) if value in text]
        results.append(
            {
                "page": page,
                "source_page": check.get("source_page"),
                "description": check.get("description", ""),
                "passed": not missing and not forbidden_found,
                "missing": missing,
                "forbidden_found": forbidden_found,
            }
        )
    return {
        "passed": sum(result["passed"] for result in results),
        "failed": sum(not result["passed"] for result in results),
        "checks": results,
    }


def page_text_from_lines(lines_path: Path | None) -> list[str]:
    if not lines_path or not lines_path.exists():
        return []
    data = json.loads(lines_path.read_text(encoding="utf-8"))
    return [
        "\n".join(
            str(line.get("text", ""))
            for line in page.get("lines", [])
            if line.get("conversion_output", True)
        )
        for page in data.get("pages", [])
    ]


def _ngrams(text: str, size: int = 5) -> set[tuple[str, ...]]:
    words = normalize_text(text).split()
    return {tuple(words[index : index + size]) for index in range(len(words) - size + 1)}


def analyze_page_flow(
    ocr_pages: list[str], output_pages: list[str], ngram_size: int = 5
) -> dict[str, Any] | None:
    """Detect content crossing page boundaries without assuming pixel alignment."""
    if not ocr_pages or not output_pages:
        return None
    output_ngrams = [_ngrams(text, ngram_size) for text in output_pages]
    rows = []
    significant_outputs: list[list[int]] = []
    for source_index, source_text in enumerate(ocr_pages, start=1):
        source_ngrams = _ngrams(source_text, ngram_size)
        counts = [len(source_ngrams.intersection(candidate)) for candidate in output_ngrams]
        total = sum(counts)
        threshold = max(2, round(total * 0.1))
        significant = [index + 1 for index, count in enumerate(counts) if count >= threshold]
        significant_outputs.append(significant)
        dominant = counts.index(max(counts)) + 1 if max(counts, default=0) else None
        rows.append(
            {
                "ocr_page": source_index,
                "matching_ngrams_per_output_page": counts,
                "dominant_output_page": dominant,
                "significant_output_pages": significant,
                "split_across_output_pages": len(significant) > 1,
            }
        )
    reverse: dict[int, list[int]] = {}
    for source_index, outputs in enumerate(significant_outputs, start=1):
        for output_index in outputs:
            reverse.setdefault(output_index, []).append(source_index)
    merged = {str(page): sources for page, sources in reverse.items() if len(sources) > 1}
    preserved = (
        len(ocr_pages) == len(output_pages)
        and not any(row["split_across_output_pages"] for row in rows)
        and not merged
        and all(row["dominant_output_page"] == row["ocr_page"] for row in rows)
    )
    return {
        "ngram_size": ngram_size,
        "page_boundaries_preserved": preserved,
        "source_pages": rows,
        "output_pages_containing_multiple_source_pages": merged,
    }


def _delta(current: dict[str, Any], old: dict[str, Any], group: str, field: str) -> float | None:
    a = (current.get(group) or {}).get(field)
    b = (old.get(group) or {}).get(field)
    return None if a is None or b is None else round(a - b, 6)


def run_qa(
    source: Path,
    output_pdfs: list[Path],
    mmd_path: Path | None,
    lines_path: Path | None,
    qa_dir: Path,
    golden_path: Path | None = None,
    baseline_path: Path | None = None,
    checks_path: Path | None = None,
    dpi: int = 120,
) -> dict[str, Any]:
    qa_dir.mkdir(parents=True, exist_ok=True)
    source_rendered = render_pdf(source, qa_dir / "render-source", dpi)
    source_ink = [round(ink_fraction(path), 6) for path in source_rendered]
    source_text = "\n".join(extract_text_pages(source))
    ocr_pages = page_text_from_lines(lines_path)
    mmd = mmd_path.read_text(encoding="utf-8") if mmd_path and mmd_path.exists() else ""
    golden = golden_path.read_text(encoding="utf-8") if golden_path else None
    report: dict[str, Any] = {
        "schema_version": 1,
        "source": {
            "file": source.name,
            "page_count": len(source_rendered),
            "page_sizes_points": page_sizes(source),
            "selectable_chars": len(normalize_text(source_text).replace(" ", "")),
            "ink_fraction_per_page": source_ink,
        },
        "methodology": {
            "note": "No aggregate quality score is used; inspect semantic, structural, and visual axes separately.",
            "cer_wer": "Ground-truth metrics are authoritative only when --golden is supplied.",
            "mmd_pdf_consistency": "Detects conversion loss but is not independent OCR validation.",
            "visual": "Review side-by-side contact sheets for layout, equations, diagrams, clipping, and reading order.",
        },
        "outputs": {},
    }
    line_metrics = lines_summary(lines_path)
    if line_metrics:
        report["ocr_self_report"] = line_metrics
    check_metrics = run_benchmark_checks(lines_path, checks_path)
    if check_metrics:
        report["ocr_benchmark_checks"] = check_metrics
    for output in output_pdfs:
        name = output.name
        rendered = render_pdf(output, qa_dir / f"render-{name}", dpi)
        pages_text = extract_text_pages(output)
        combined = "\n".join(pages_text)
        ink = [round(ink_fraction(path), 6) for path in rendered]
        bboxes = [content_bbox_fraction(path) for path in rendered]
        blank_pages = sum(value < 0.002 for value in ink)
        paired = min(len(source_ink), len(ink))
        density_ratios = [
            round(ink[index] / source_ink[index], 6) if source_ink[index] else None
            for index in range(paired)
        ]
        warnings: list[str] = []
        page_flow = analyze_page_flow(ocr_pages, pages_text)
        if len(rendered) != len(source_rendered):
            warnings.append("page_count_changed")
        if page_sizes(output) != page_sizes(source):
            warnings.append("page_size_changed")
        if blank_pages:
            warnings.append("blank_or_nearly_blank_pages")
        if page_flow and not page_flow["page_boundaries_preserved"]:
            warnings.append("source_page_boundaries_not_preserved")
        if any(value is not None and value < 0.6 for value in density_ratios):
            warnings.append("substantially_lower_visual_density")
        if any(
            bbox
            and (bbox["left"] < 0.015 or bbox["top"] < 0.015 or bbox["right"] > 0.985 or bbox["bottom"] > 0.985)
            for bbox in bboxes
        ):
            warnings.append("content_near_page_edge_review_for_clipping")
        metrics: dict[str, Any] = {
            "page_count": len(rendered),
            "page_count_matches_source": len(rendered) == len(source_rendered),
            "page_sizes_points": page_sizes(output),
            "page_sizes_match_source": page_sizes(output) == page_sizes(source),
            "selectable_chars": len(normalize_text(combined).replace(" ", "")),
            "selectable_chars_per_page": [
                len(normalize_text(text).replace(" ", "")) for text in pages_text
            ],
            "ink_fraction_per_page": ink,
            "ink_density_ratio_to_source_per_paired_page": density_ratios,
            "blank_pages": blank_pages,
            "content_bbox_fraction_per_page": bboxes,
            "warnings": warnings,
        }
        if page_flow:
            metrics["page_flow"] = page_flow
        if mmd:
            metrics["mmd_pdf_consistency"] = error_rates(mmd, combined)
        if golden is not None:
            metrics["ground_truth"] = error_rates(golden, combined)
        report["outputs"][name] = metrics
        make_contact_sheet(
            source_rendered,
            rendered,
            qa_dir / f"compare-{name}.png",
            "source scan",
            name,
        )
    if baseline_path:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        report["baseline_deltas"] = compare_to_baseline(report, baseline)
    (qa_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report
