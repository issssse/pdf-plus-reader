from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any


def tune_tex_source(
    source: str,
    *,
    document_class: str,
    font_size: str,
    paper: str,
    margin: str,
    babel_language: str | None = None,
    omit_packages: list[str] | None = None,
    replacements: list[dict[str, Any]] | None = None,
) -> str:
    class_pattern = re.compile(r"\\documentclass(?:\[[^]]*\])?\{[^}]+\}")
    class_line = f"\\documentclass[{font_size},{paper}]{{{document_class}}}"
    source, count = class_pattern.subn(lambda _: class_line, source, count=1)
    if count != 1:
        raise ValueError("Expected exactly one LaTeX documentclass")
    geometry_line = f"\\usepackage[{paper},margin={margin}]{{geometry}}"
    geometry_pattern = re.compile(r"\\usepackage(?:\[[^]]*\])?\{geometry\}")
    if geometry_pattern.search(source):
        source = geometry_pattern.sub(lambda _: geometry_line, source, count=1)
    else:
        source = source.replace(class_line, class_line + "\n" + geometry_line, 1)
    if babel_language:
        babel_pattern = re.compile(r"\\usepackage\[[^]]+\]\{babel\}")
        source, count = babel_pattern.subn(
            lambda _: f"\\usepackage[{babel_language}]{{babel}}", source, count=1
        )
        if count != 1:
            raise ValueError("--babel-language was set but no babel package line was found")
    for package in omit_packages or []:
        package_pattern = re.compile(
            rf"^\\usepackage(?:\[[^]]*\])?\{{{re.escape(package)}\}}\s*$", re.MULTILINE
        )
        source, count = package_pattern.subn(
            lambda match: "% omitted by mathpix-pdf tex-tune: " + match.group(0), source
        )
        if count != 1:
            raise ValueError(f"Expected exactly one usepackage line for {package!r}")
    for replacement in replacements or []:
        old = str(replacement["old"])
        new = str(replacement["new"])
        expected = int(replacement.get("expected_count", 1))
        actual = source.count(old)
        if actual != expected:
            raise ValueError(
                f"Replacement {replacement.get('description', old[:60])!r} expected "
                f"{expected} occurrence(s), found {actual}"
            )
        source = source.replace(old, new)
    return source


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        if root != target and root not in target.parents:
            raise ValueError(f"Unsafe path in tex.zip: {member.filename}")
    archive.extractall(destination)


def compile_tuned_tex(
    tex_zip: Path,
    output: Path,
    *,
    document_class: str = "extarticle",
    font_size: str = "14pt",
    paper: str = "a4paper",
    margin: str = "18mm",
    babel_language: str | None = None,
    omit_packages: list[str] | None = None,
    replacements_path: Path | None = None,
    engine: str = "pdflatex",
) -> dict[str, Any]:
    executable = shutil.which(engine)
    if not executable:
        raise RuntimeError(f"LaTeX engine not found: {engine}")
    replacement_spec = (
        json.loads(replacements_path.read_text(encoding="utf-8")) if replacements_path else {}
    )
    with tempfile.TemporaryDirectory(prefix="mathpix-tex-") as temporary_name:
        temporary = Path(temporary_name)
        with zipfile.ZipFile(tex_zip) as archive:
            _safe_extract(archive, temporary)
        tex_files = list(temporary.rglob("*.tex"))
        if len(tex_files) != 1:
            raise ValueError(f"Expected exactly one .tex file in archive, found {len(tex_files)}")
        tex_file = tex_files[0]
        tuned = tune_tex_source(
            tex_file.read_text(encoding="utf-8"),
            document_class=document_class,
            font_size=font_size,
            paper=paper,
            margin=margin,
            babel_language=babel_language,
            omit_packages=omit_packages,
            replacements=replacement_spec.get("replacements", []),
        )
        tex_file.write_text(tuned, encoding="utf-8")
        command = [executable, "-interaction=nonstopmode", "-halt-on-error", tex_file.name]
        last = None
        for _ in range(2):
            last = subprocess.run(
                command,
                cwd=tex_file.parent,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            if last.returncode:
                raise RuntimeError("LaTeX compilation failed:\n" + last.stdout[-4000:])
        compiled = tex_file.with_suffix(".pdf")
        if not compiled.exists():
            raise RuntimeError("LaTeX reported success but produced no PDF")
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(compiled, output)
    return {
        "tex_zip": str(tex_zip.resolve()),
        "output": str(output.resolve()),
        "document_class": document_class,
        "font_size": font_size,
        "paper": paper,
        "margin": margin,
        "babel_language": babel_language,
        "omit_packages": omit_packages or [],
        "replacements": str(replacements_path.resolve()) if replacements_path else None,
        "engine": engine,
    }
