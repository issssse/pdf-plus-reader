import json

from mathpix_pipeline.qa import (
    compare_to_baseline,
    analyze_page_flow,
    edit_distance,
    error_rates,
    lines_summary,
    normalize_text,
    run_benchmark_checks,
)


def test_normalize_text_handles_unicode_and_punctuation():
    assert normalize_text("RÄTT—svar!") == "rätt svar"


def test_error_rates_exact_and_changed():
    assert error_rates("abc def", "ABC, def")["cer"] == 0
    changed = error_rates("abc def", "abc xyz")
    assert changed["wer"] == 0.5
    assert changed["cer"] > 0


def test_edit_distance_scales_to_full_book_text():
    reference = "transformteori " * 20_000
    candidate = reference[:-13] + "laplaceteori "
    assert edit_distance(reference, candidate) == 7


def test_baseline_deltas_keep_axes_separate():
    baseline = {
        "outputs": {
            "result.pdf": {
                "selectable_chars": 100,
                "blank_pages": 1,
                "mmd_pdf_consistency": {"cer": 0.1},
            }
        }
    }
    report = {
        "outputs": {
            "result.pdf": {
                "selectable_chars": 120,
                "blank_pages": 0,
                "mmd_pdf_consistency": {"cer": 0.08},
            }
        }
    }
    delta = compare_to_baseline(report, baseline)["result.pdf"]
    assert delta == {
        "selectable_chars_delta": 20,
        "blank_pages_delta": -1,
        "mmd_pdf_cer_delta": -0.02,
        "ground_truth_cer_delta": None,
    }


def test_lines_summary(tmp_path):
    path = tmp_path / "lines.json"
    path.write_text(
        json.dumps(
            {
                "pages": [
                    {
                        "page": 1,
                        "languages_detected": ["sv"],
                        "lines": [{"confidence": 1.0}, {"confidence": 0.5}],
                    }
                ]
            }
        )
    )
    summary = lines_summary(path)
    assert summary["mean_line_confidence"] == 0.75
    assert summary["low_confidence_lines_below_0_8"] == 1
    assert summary["lowest_confidence_lines"][0]["confidence"] == 0.5


def test_benchmark_checks_report_missing_and_forbidden(tmp_path):
    lines = tmp_path / "lines.json"
    checks = tmp_path / "checks.json"
    lines.write_text(json.dumps({"pages": [{"page": 1, "lines": [{"text": "good bad"}]}]}))
    checks.write_text(
        json.dumps(
            {
                "checks": [
                    {"page": 1, "contains": ["good"], "not_contains": ["bad"]},
                    {"page": 1, "contains": ["missing"]},
                ]
            }
        )
    )
    result = run_benchmark_checks(lines, checks)
    assert result["passed"] == 0
    assert result["failed"] == 2


def test_page_flow_detects_split_and_preserved_boundaries():
    source = [
        "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda",
        "one two three four five six seven eight nine ten eleven twelve",
    ]
    preserved = analyze_page_flow(source, source)
    assert preserved["page_boundaries_preserved"] is True
    split = analyze_page_flow(
        source,
        [
            "alpha beta gamma delta epsilon zeta one two three four five six",
            "eta theta iota kappa lambda seven eight nine ten eleven twelve",
        ],
        ngram_size=3,
    )
    assert split["page_boundaries_preserved"] is False
    assert any(page["split_across_output_pages"] for page in split["source_pages"])
