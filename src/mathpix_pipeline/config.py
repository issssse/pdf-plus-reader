from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_OPTIONS: dict[str, Any] = {
    "include_page_breaks": True,
    "metadata": {"improve_mathpix": False},
    "conversion_formats": {
        "latex.pdf": True,
        "pdf": True,
        "tex.zip": True,
    },
    "conversion_options": {
        "latex.pdf": {"fontSize": "10pt", "font": "CMU Serif"},
        "pdf": {"fontSize": 17, "margin": 40, "text_color": "black"},
    },
}

ALWAYS_OUTPUTS = ("mmd", "lines.json")


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE entries without replacing exported variables."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0:1] == value[-1:] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def credentials(env_file: Path) -> tuple[str, str]:
    load_env_file(env_file)
    app_id = os.getenv("MATHPIX_API_ID", "").strip()
    app_key = os.getenv("MATHPIX_API_KEY", "").strip()
    missing = [
        name
        for name, value in (("MATHPIX_API_ID", app_id), ("MATHPIX_API_KEY", app_key))
        if not value
    ]
    if missing:
        raise ValueError(f"Missing credentials: {', '.join(missing)}")
    return app_id, app_key


def load_options(path: Path | None) -> dict[str, Any]:
    if path is None:
        # JSON round-trip makes a defensive deep copy without another dependency.
        return json.loads(json.dumps(DEFAULT_OPTIONS))
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Options file must contain a JSON object")
    formats = data.get("conversion_formats", {})
    invalid = sorted(set(formats).intersection(ALWAYS_OUTPUTS))
    if invalid:
        raise ValueError(
            "Always-produced outputs cannot be conversion_formats: " + ", ".join(invalid)
        )
    return data
