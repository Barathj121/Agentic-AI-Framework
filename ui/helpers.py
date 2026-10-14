"""Bounded, non-executing artifact previews used by the Streamlit interface."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from workbench.workspace import safe_path

TEXT_PREVIEW_BYTES = 256 * 1024
JSON_PREVIEW_BYTES = 2 * 1024 * 1024
DOWNLOAD_BYTES = 100 * 1024 * 1024


def read_text(workspace: Path, relative: str, limit: int = TEXT_PREVIEW_BYTES) -> str:
    path = safe_path(workspace, relative)
    if not path.is_file():
        return ""
    with path.open("rb") as handle:
        value = handle.read(limit + 1)
    clipped = len(value) > limit
    return value[:limit].decode("utf-8", errors="replace") + ("\n… preview truncated …" if clipped else "")


def read_json_preview(workspace: Path, relative: str) -> Any:
    path = safe_path(workspace, relative)
    if path.stat().st_size > JSON_PREVIEW_BYTES:
        raise ValueError("JSON exceeds the 2 MB preview limit; download it to inspect the complete file.")
    return json.loads(path.read_text(encoding="utf-8"))


def quoted_table(location: str) -> str:
    parts = location.split(".")
    if len(parts) != 2 or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts):
        raise ValueError("Expected a registered schema.table name containing letters, numbers and underscores.")
    return ".".join('"' + part + '"' for part in parts)


def proposals_from(value: Any) -> list[dict]:
    if isinstance(value, dict):
        value = value.get("proposals", [])
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def notebook_preview(document: dict) -> list[dict]:
    """Only display notebook sources and existing plain text outputs. Never execute cells."""
    cells = []
    for cell in document.get("cells", [])[:50]:
        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(source)
        outputs = []
        for output in cell.get("outputs", [])[:10]:
            plain = output.get("text", output.get("data", {}).get("text/plain", ""))
            outputs.append("".join(plain) if isinstance(plain, list) else str(plain))
        cells.append({"type": cell.get("cell_type"), "source": str(source)[:20000], "outputs": outputs})
    return cells


def chart_has_external_resources(value: Any) -> bool:
    if isinstance(value, dict):
        return any(chart_has_external_resources(item) for item in value.values())
    if isinstance(value, list):
        return any(chart_has_external_resources(item) for item in value)
    return isinstance(value, str) and value.strip().lower().startswith(("http:", "https:", "file:", "javascript:", "//"))
