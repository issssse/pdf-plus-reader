from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .client import MathpixClient
from .config import ALWAYS_OUTPUTS, credentials, load_options
from .html_reader import build_html_reader
from .pdf_tools import page_count, parse_pages, select_pages
from .qa import run_qa
from .tex_tools import compile_tuned_tex


def safe_stem(path: Path) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", path.stem).strip("-.")
    return value or "document"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_client(args: argparse.Namespace) -> MathpixClient:
    app_id, app_key = credentials(args.env_file)
    base = "https://eu.api.mathpix.com" if args.region == "eu" else "https://api.mathpix.com"
    return MathpixClient(app_id, app_key, base_url=base, request_timeout=args.request_timeout)


def finalize_run(
    args: argparse.Namespace,
    run_dir: Path,
    manifest: dict[str, Any],
    selected: Path,
) -> dict[str, Any]:
    client = build_client(args)
    pdf_id = manifest["pdf_id"]
    options = manifest["options"]
    requested = [name for name, enabled in options.get("conversion_formats", {}).items() if enabled]

    def progress(state: dict[str, Any]) -> None:
        conversions = state.get("conversion_status") or {}
        short = {key: value.get("status") for key, value in conversions.items() if isinstance(value, dict)}
        print(
            f"OCR={state.get('status')} percent={state.get('percent_done', '?')} "
            f"conversions={short}",
            flush=True,
        )

    state = client.wait(
        pdf_id,
        requested,
        poll_seconds=args.poll_seconds,
        max_wait_seconds=args.max_wait,
        progress=progress,
    )
    dump(run_dir / "status.json", state)
    outputs_dir = run_dir / "outputs"
    for extension in [*requested, *ALWAYS_OUTPUTS]:
        destination = outputs_dir / f"result.{extension}"
        if not destination.exists() or destination.stat().st_size == 0:
            client.download(pdf_id, extension, destination)
            print(f"Downloaded {destination.name}")
    output_pdfs = [outputs_dir / f"result.{name}" for name in requested if name.endswith("pdf")]
    report = run_qa(
        selected,
        output_pdfs,
        outputs_dir / "result.mmd",
        outputs_dir / "result.lines.json",
        run_dir / "qa",
        golden_path=args.golden,
        baseline_path=args.baseline,
        checks_path=args.checks,
        dpi=args.qa_dpi,
    )
    print(f"QA report: {run_dir / 'qa' / 'report.json'}")
    for name, metrics in report["outputs"].items():
        consistency = metrics.get("mmd_pdf_consistency", {}).get("cer")
        print(
            f"{name}: pages={metrics['page_count']}, selectable_chars={metrics['selectable_chars']}, "
            f"blank_pages={metrics['blank_pages']}, mmd_pdf_cer={consistency}, "
            f"warnings={metrics['warnings']}"
        )
    print(f"Run directory: {run_dir}")
    return report


def command_sample(args: argparse.Namespace) -> int:
    source = args.input.resolve()
    pages = parse_pages(args.pages, page_count(source))
    select_pages(source, args.output.resolve(), pages)
    print(f"Created {args.output} with source pages {pages}")
    return 0


def command_run(args: argparse.Namespace) -> int:
    source = args.input.resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    options = load_options(args.options)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = f"-{args.label}" if args.label else ""
    run_dir = (args.output_root / safe_stem(source) / f"{stamp}{label}").resolve()
    run_dir.mkdir(parents=True)
    selected = source
    pages: list[int] | None = None
    if args.pages:
        pages = parse_pages(args.pages, page_count(source))
        selected = run_dir / "input-sample.pdf"
        select_pages(source, selected, pages)
    dump(run_dir / "options.json", options)
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "source_sha256": sha256(source),
        "selected_pages": pages,
        "submitted_file": selected.name if selected.parent == run_dir else str(selected),
        "region": args.region,
        "options": options,
    }
    dump(run_dir / "manifest.json", manifest)
    if args.dry_run:
        print(f"Dry run prepared: {run_dir}")
        return 0
    client = build_client(args)
    pdf_id = client.submit(selected, options)
    manifest["pdf_id"] = pdf_id
    dump(run_dir / "manifest.json", manifest)
    print(f"Submitted Mathpix job {pdf_id}")
    finalize_run(args, run_dir, manifest, selected)
    return 0


def command_resume(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if "pdf_id" not in manifest:
        raise ValueError("Run manifest has no pdf_id; the dry run was never submitted")
    args.region = args.region or manifest.get("region", "eu")
    submitted = Path(manifest["submitted_file"])
    selected = submitted if submitted.is_absolute() else run_dir / submitted
    if not selected.exists():
        selected = Path(manifest["source"])
    finalize_run(args, run_dir, manifest, selected)
    return 0


def command_rerender(args: argparse.Namespace) -> int:
    base_run = args.run_dir.resolve()
    base_manifest = json.loads((base_run / "manifest.json").read_text(encoding="utf-8"))
    mmd_path = args.mmd.resolve() if args.mmd else base_run / "outputs" / "result.mmd"
    if not mmd_path.exists():
        raise FileNotFoundError(mmd_path)
    options = load_options(args.options)
    formats = {
        name: enabled for name, enabled in options.get("conversion_formats", {}).items() if enabled
    }
    if not formats:
        raise ValueError("Rerender options request no conversion formats")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = f"-{args.label}" if args.label else ""
    render_dir = base_run / "rerenders" / f"{stamp}{label}"
    render_dir.mkdir(parents=True)
    client = build_client(args)
    conversion_id = client.submit_conversion(
        mmd_path.read_text(encoding="utf-8"),
        formats,
        options.get("conversion_options"),
        options.get("metadata"),
    )
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "base_run": str(base_run),
        "source_mmd_sha256": sha256(mmd_path),
        "conversion_id": conversion_id,
        "region": args.region,
        "options": options,
    }
    dump(render_dir / "manifest.json", manifest)
    requested = list(formats)

    def progress(state: dict[str, Any]) -> None:
        conversions = state.get("conversion_status") or {}
        short = {key: value.get("status") for key, value in conversions.items() if isinstance(value, dict)}
        print(f"conversion={conversion_id} formats={short}", flush=True)

    state = client.wait_conversion(
        conversion_id,
        requested,
        poll_seconds=args.poll_seconds,
        max_wait_seconds=args.max_wait,
        progress=progress,
    )
    dump(render_dir / "status.json", state)
    outputs_dir = render_dir / "outputs"
    for extension in requested:
        client.download_conversion(conversion_id, extension, outputs_dir / f"result.{extension}")
        print(f"Downloaded result.{extension}")
    submitted = Path(base_manifest["submitted_file"])
    source = submitted if submitted.is_absolute() else base_run / submitted
    if not source.exists():
        source = Path(base_manifest["source"])
    output_pdfs = [outputs_dir / f"result.{name}" for name in requested if name.endswith("pdf")]
    run_qa(
        source,
        output_pdfs,
        mmd_path,
        base_run / "outputs" / "result.lines.json",
        render_dir / "qa",
        golden_path=args.golden,
        baseline_path=args.baseline or (base_run / "qa" / "report.json"),
        checks_path=args.checks,
        dpi=args.qa_dpi,
    )
    print(f"Rerender directory: {render_dir}")
    return 0


def command_qa(args: argparse.Namespace) -> int:
    report = run_qa(
        args.source.resolve(),
        [path.resolve() for path in args.outputs],
        args.mmd.resolve() if args.mmd else None,
        args.lines.resolve() if args.lines else None,
        args.qa_dir.resolve(),
        golden_path=args.golden.resolve() if args.golden else None,
        baseline_path=args.baseline.resolve() if args.baseline else None,
        checks_path=args.checks.resolve() if args.checks else None,
        dpi=args.qa_dpi,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def command_tex_tune(args: argparse.Namespace) -> int:
    result = compile_tuned_tex(
        args.tex_zip.resolve(),
        args.output.resolve(),
        document_class=args.document_class,
        font_size=args.font_size,
        paper=args.paper,
        margin=args.margin,
        babel_language=args.babel_language,
        omit_packages=args.omit_package,
        replacements_path=args.replacements.resolve() if args.replacements else None,
        engine=args.engine,
    )
    result["tex_zip_sha256"] = sha256(args.tex_zip.resolve())
    if args.replacements:
        result["replacements_sha256"] = sha256(args.replacements.resolve())
    result["output_sha256"] = sha256(args.output.resolve())
    manifest = args.output.resolve().with_suffix(".manifest.json")
    dump(manifest, result)
    print(f"Created {args.output.resolve()}")
    print(f"Manifest: {manifest}")
    return 0


def command_html(args: argparse.Namespace) -> int:
    target = args.input.resolve()
    if target.is_dir():
        manifest_path = target / "manifest.json"
        if not manifest_path.exists():
            raise ValueError(f"Run directory has no manifest.json: {target}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        submitted = Path(manifest.get("submitted_file", ""))
        source = submitted if submitted.is_absolute() else target / submitted
        if not source.exists():
            source = Path(manifest.get("source", ""))
        lines = args.lines.resolve() if args.lines else target / "outputs" / "result.lines.json"
    else:
        source = target
        if not args.lines:
            raise ValueError("--lines is required when INPUT is a PDF")
        lines = args.lines.resolve()
    standalone = not args.folder
    if args.output:
        output = args.output.resolve()
    elif standalone:
        output = (Path("output/html") / f"{safe_stem(source)}.html").resolve()
    else:
        output = (Path("output/html") / safe_stem(source)).resolve()
    result = build_html_reader(
        source,
        lines,
        output,
        title=args.title,
        dpi=args.dpi,
        quality=args.quality,
        corrections_path=args.corrections,
        checks_path=args.checks,
        make_zip=not args.no_zip,
        standalone=standalone,
        force=args.force,
    )
    qa = result["qa"]
    print(f"Reader: {result['index']}")
    if result["zip"]:
        print(f"Shareable ZIP: {result['zip']}")
    print(
        f"QA={qa['status']} pages={qa['metrics']['rendered_pages']} "
        f"lines={qa['metrics']['ocr_lines']} "
        f"selectable_chars={qa['metrics']['selectable_characters']} "
        f"warnings={qa['warnings']}"
    )
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="mathpix-pdf",
        description="Reproducible Mathpix PDF conversion experiments with multi-axis QA.",
    )
    commands = root.add_subparsers(dest="command", required=True)
    sample = commands.add_parser("sample", help="Create a small PDF from selected source pages")
    sample.add_argument("input", type=Path)
    sample.add_argument("--pages", required=True, help="1-based selection, e.g. 1,3-5,last")
    sample.add_argument("--output", type=Path, required=True)
    sample.set_defaults(func=command_sample)

    run = commands.add_parser("run", help="Submit, download, and QA a Mathpix conversion")
    run.add_argument("input", type=Path)
    run.add_argument("--pages", help="Submit only selected 1-based pages")
    run.add_argument("--label", default="")
    run.add_argument("--options", type=Path, help="Mathpix v3/pdf options JSON")
    run.add_argument("--output-root", type=Path, default=Path("output/runs"))
    run.add_argument("--env-file", type=Path, default=Path(".env"))
    run.add_argument("--region", choices=("global", "eu"), default="eu")
    run.add_argument("--poll-seconds", type=float, default=5)
    run.add_argument("--max-wait", type=float, default=1800)
    run.add_argument("--request-timeout", type=float, default=90)
    run.add_argument("--qa-dpi", type=int, default=120)
    run.add_argument("--golden", type=Path, help="Manually verified UTF-8 transcript")
    run.add_argument("--checks", type=Path, help="Manually verified page-level JSON assertions")
    run.add_argument("--baseline", type=Path, help="Earlier QA report.json for regression deltas")
    run.add_argument("--dry-run", action="store_true", help="Prepare run files without API calls")
    run.set_defaults(func=command_run)

    resume = commands.add_parser("resume", help="Resume polling/download/QA for a submitted run")
    resume.add_argument("run_dir", type=Path)
    resume.add_argument("--env-file", type=Path, default=Path(".env"))
    resume.add_argument("--region", choices=("global", "eu"))
    resume.add_argument("--poll-seconds", type=float, default=5)
    resume.add_argument("--max-wait", type=float, default=1800)
    resume.add_argument("--request-timeout", type=float, default=90)
    resume.add_argument("--qa-dpi", type=int, default=120)
    resume.add_argument("--golden", type=Path)
    resume.add_argument("--checks", type=Path)
    resume.add_argument("--baseline", type=Path)
    resume.set_defaults(func=command_resume)

    rerender = commands.add_parser(
        "rerender", help="Convert saved MMD with new output settings, without rerunning OCR"
    )
    rerender.add_argument("run_dir", type=Path, help="Completed OCR run containing result.mmd")
    rerender.add_argument("--options", type=Path, required=True)
    rerender.add_argument("--mmd", type=Path, help="Edited MMD; defaults to the base run result.mmd")
    rerender.add_argument("--label", default="")
    rerender.add_argument("--env-file", type=Path, default=Path(".env"))
    rerender.add_argument("--region", choices=("global", "eu"), default="eu")
    rerender.add_argument("--poll-seconds", type=float, default=3)
    rerender.add_argument("--max-wait", type=float, default=900)
    rerender.add_argument("--request-timeout", type=float, default=90)
    rerender.add_argument("--qa-dpi", type=int, default=120)
    rerender.add_argument("--golden", type=Path)
    rerender.add_argument("--checks", type=Path)
    rerender.add_argument("--baseline", type=Path)
    rerender.set_defaults(func=command_rerender)

    qa = commands.add_parser("qa", help="QA existing regenerated PDFs")
    qa.add_argument("source", type=Path)
    qa.add_argument("outputs", type=Path, nargs="+")
    qa.add_argument("--mmd", type=Path)
    qa.add_argument("--lines", type=Path, help="Mathpix lines.json for confidence diagnostics")
    qa.add_argument("--qa-dir", type=Path, required=True)
    qa.add_argument("--golden", type=Path)
    qa.add_argument("--checks", type=Path, help="Manually verified page-level JSON assertions")
    qa.add_argument("--baseline", type=Path)
    qa.add_argument("--qa-dpi", type=int, default=120)
    qa.set_defaults(func=command_qa)

    tex = commands.add_parser(
        "tex-tune", help="Compile a tex.zip with reproducible page geometry and corrections"
    )
    tex.add_argument("tex_zip", type=Path)
    tex.add_argument("--output", type=Path, required=True)
    tex.add_argument("--document-class", default="extarticle")
    tex.add_argument("--font-size", default="14pt")
    tex.add_argument("--paper", default="a4paper")
    tex.add_argument("--margin", default="18mm")
    tex.add_argument("--babel-language")
    tex.add_argument("--omit-package", action="append", default=[])
    tex.add_argument("--replacements", type=Path, help="Validated exact string replacements JSON")
    tex.add_argument("--engine", choices=("pdflatex", "xelatex", "lualatex"), default="pdflatex")
    tex.set_defaults(func=command_tex_tune)

    html_cmd = commands.add_parser(
        "html", help="Build a standalone searchable HTML reader from PDF and Mathpix lines"
    )
    html_cmd.add_argument(
        "input", type=Path, help="A completed run directory, or a source PDF used with --lines"
    )
    html_cmd.add_argument("--lines", type=Path, help="Mathpix result.lines.json")
    html_cmd.add_argument(
        "--output", type=Path, help="Output HTML file (default: output/html/<name>.html)"
    )
    html_cmd.add_argument("--title", help="Reader title; defaults to the PDF filename")
    html_cmd.add_argument("--dpi", type=int, default=144, help="Page image resolution (72-300)")
    html_cmd.add_argument("--quality", type=int, default=88, help="JPEG quality (50-100)")
    html_cmd.add_argument(
        "--corrections", type=Path, help="Validated exact OCR line corrections JSON"
    )
    html_cmd.add_argument("--checks", type=Path, help="Page-level semantic QA checks JSON")
    html_cmd.add_argument(
        "--folder", action="store_true", help="Build the legacy folder/ZIP format instead"
    )
    html_cmd.add_argument(
        "--no-zip", action="store_true", help="With --folder, do not create a shareable ZIP"
    )
    html_cmd.add_argument("--force", action="store_true", help="Replace an existing output")
    html_cmd.set_defaults(func=command_html)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError, TimeoutError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
