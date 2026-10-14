"""Display a bounded dashboard declared by validated execution artifacts.

The report contains presentation data, never analysis logic or executable code.
Its chart locations must independently name validated entries in the registry.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import plotly.io as pio
import streamlit as st

from ui.helpers import chart_has_external_resources, read_json_preview
from workbench.workspace import safe_path

SCHEMA = "workbench.dashboard.v1"
MAX_CANDIDATES = 32
MAX_METRICS = 8
MAX_FINDINGS = 12
MAX_CHARTS = 4


def ordered_artifacts(artifacts: list, expected_outputs: list) -> list[dict]:
    """Prioritize validated declared results without changing the saved registry."""
    expected = {item.get("location") for item in expected_outputs if isinstance(item, dict)}

    def priority(item: dict) -> int:
        location = str(item.get("location", ""))
        if location.startswith("audit."):
            return 5
        if item.get("type") in {"python_script", "sql_script", "notebook"}:
            return 3
        if item.get("validation_status") == "PASSED":
            return 0 if location in expected else 1
        return 4 if item.get("validation_status") == "DISCOVERED" else 2

    return sorted((item for item in artifacts if isinstance(item, dict)), key=priority)


def _text(value: Any, name: str, maximum: int, *, optional: bool = False) -> str:
    if optional and value is None:
        return ""
    if not isinstance(value, str) or len(value) > maximum or (not optional and not value.strip()):
        raise ValueError(f"Dashboard {name} must be text of at most {maximum} characters.")
    return value


def _items(value: Any, name: str, maximum: int) -> list:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"Dashboard {name} must be a list of at most {maximum} entries.")
    return value


def _escape_markdown(value: str) -> str:
    # Labels and help accept Markdown in Streamlit; keep report text literal.
    return re.sub(r"([\\`*_{}\[\]()<>#+.!|~-])", r"\\\1", value)


def _external_chart_content(value: Any) -> bool:
    if chart_has_external_resources(value):
        return True
    if isinstance(value, dict):
        return any(_external_chart_content(item) for item in value.values())
    if isinstance(value, list):
        return any(_external_chart_content(item) for item in value)
    # Also catch resources embedded inside chart annotations/HTML, including data URLs.
    return isinstance(value, str) and bool(re.search(
        r"(?:https?|ftp|file|javascript|data):|(?:^|[\s\"'=])//|<(?:img|iframe|script)\b",
        value, re.IGNORECASE,
    ))


def _load_chart(workspace: Path, location: str, registry: dict[str, dict]) -> Any:
    artifact = registry.get(location)
    if not artifact or artifact.get("type") != "chart" or artifact.get("validation_status") != "PASSED":
        raise ValueError(f"Dashboard chart is not a validated registered chart: {location}")
    path = safe_path(workspace, location)
    if path.suffix.lower() != ".json":
        raise ValueError("Dashboard charts must be registered Plotly JSON files.")
    spec = read_json_preview(workspace, location)
    if not isinstance(spec, dict) or _external_chart_content(spec):
        raise ValueError("Dashboard chart contains unsupported or external resource references.")
    # Geographic trace types can fetch basemaps even without an explicit URL.
    geographic = {"scattergeo", "choropleth", "scattermap", "choroplethmap", "densitymap",
                  "scattermapbox", "choroplethmapbox", "densitymapbox"}
    if any(isinstance(trace, dict) and trace.get("type") in geographic for trace in spec.get("data", [])):
        raise ValueError("Dashboard geographic charts require resources outside this local preview.")
    return pio.from_json(json.dumps(spec))


def load_dashboard(workspace: Path, artifacts: list, validation: dict) -> dict | None:
    """Return one fully checked report, or None when no eligible format is present.

    Invalid recognized reports raise ValueError for a recoverable UI warning.
    Ordinary JSON artifacts without this schema remain in the artifact browser.
    """
    if validation.get("passed") is not True:
        return None
    registry = {item["location"]: item for item in artifacts[:500]
                if isinstance(item, dict) and isinstance(item.get("location"), str)}
    candidates = [item for item in registry.values()
                  if item.get("type") == "json" and item.get("validation_status") == "PASSED"]
    candidates.sort(key=lambda item: (Path(item["location"]).name != "dashboard.json", item["location"]))
    for artifact in candidates[:MAX_CANDIDATES]:
        location = artifact["location"]
        try:
            document = read_json_preview(workspace, location)
        except (OSError, ValueError):
            if Path(location).name == "dashboard.json":
                raise ValueError("Registered dashboard JSON is missing, unsafe, too large, or unreadable.") from None
            continue
        if not isinstance(document, dict) or document.get("schema") != SCHEMA:
            continue
        result = {
            "title": _text(document.get("title"), "title", 160),
            "summary": _text(document.get("summary"), "summary", 2000),
            "provenance": _text(document.get("provenance"), "provenance", 1000, optional=True),
            "location": location,
            "metrics": [],
            "findings": [],
            "charts": [],
        }
        for metric in _items(document.get("metrics", []), "metrics", MAX_METRICS):
            if not isinstance(metric, dict):
                raise ValueError("Dashboard metrics must be objects.")
            value = metric.get("value")
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError("Dashboard metric values must be text or finite numbers.")
            if isinstance(value, (int, float)) and not math.isfinite(value):
                raise ValueError("Dashboard metric values must be finite.")
            result["metrics"].append({
                "label": _text(metric.get("label"), "metric label", 100),
                "value": _text(str(value), "metric value", 80),
                "help": _text(metric.get("help"), "metric help", 500, optional=True),
            })
        result["findings"] = [_text(item, "finding", 1000) for item in
                              _items(document.get("findings", []), "findings", MAX_FINDINGS)]
        chart_locations = [_text(item, "chart location", 240) for item in
                           _items(document.get("charts", []), "charts", MAX_CHARTS)]
        if len(chart_locations) != len(set(chart_locations)):
            raise ValueError("Dashboard chart locations must be distinct.")
        result["charts"] = [_load_chart(workspace, item, registry) for item in chart_locations]
        return result
    return None


def render_dashboard(workspace: Path, artifacts: list, validation: dict) -> bool:
    """Render saved computed values without deriving new business conclusions."""
    try:
        report = load_dashboard(workspace, artifacts, validation)
    except Exception as exc:
        st.warning(f"Dashboard preview is unavailable: {str(exc)[:300]}")
        return False
    if report is None:
        return False
    with st.container(key="workbench_dashboard"):
        # Static, scoped presentation styles; no report content is inserted into CSS/HTML.
        st.markdown("""<style>
        .st-key-workbench_dashboard [data-testid="stMetricLabel"],
        .st-key-workbench_dashboard [data-testid="stMetricLabel"] p {
            white-space: normal !important; height: auto !important;
        }
        @media (max-width: 900px) {
            .st-key-workbench_dashboard .st-key-dashboard_metrics [data-testid="stHorizontalBlock"] {
                flex-wrap: wrap !important;
            }
            .st-key-workbench_dashboard .st-key-dashboard_metrics [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
                flex: 1 1 calc(50% - 1rem) !important;
                min-width: calc(50% - 1rem) !important;
                width: calc(50% - 1rem) !important;
            }
            .st-key-workbench_dashboard .st-key-dashboard_charts [data-testid="stHorizontalBlock"] {
                flex-direction: column !important;
            }
            .st-key-workbench_dashboard .st-key-dashboard_charts [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
                flex: 1 1 100% !important; min-width: 100% !important; width: 100% !important;
            }
        }
        </style>""", unsafe_allow_html=True)
        st.subheader(_escape_markdown(report["title"]))
        st.text(report["summary"])
        with st.container(key="dashboard_metrics"):
            for start in range(0, len(report["metrics"]), 4):
                row = report["metrics"][start:start + 4]
                for column, metric in zip(st.columns(len(row)), row):
                    column.metric(_escape_markdown(metric["label"]), metric["value"],
                                  help=_escape_markdown(metric["help"]) if metric["help"] else None)
        if report["provenance"]:
            st.caption(_escape_markdown(report["provenance"]))
        with st.container(key="dashboard_charts"):
            for start in range(0, len(report["charts"]), 2):
                for index, (column, figure) in enumerate(zip(st.columns(2), report["charts"][start:start + 2]), start=start):
                    with column:
                        # Keep the saved Plotly template's text/background colors together.
                        st.plotly_chart(figure, theme=None, width="stretch", key=f"dashboard_{workspace.name}_{index}")
        if report["findings"]:
            st.markdown("**Computed findings**")
            for finding in report["findings"]:
                st.text("• " + finding)
        st.caption("Saved execution output · Dashboard and displayed charts passed their recorded validation checks.")
        st.divider()
    return True
