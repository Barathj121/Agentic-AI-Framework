"""Generic numeric prediction form for a validated saved model.

The backend supplies the feature labels, limits, defaults, presets and inference
contract. The UI does not calculate scores or interpret input business rules.
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from workbench.prediction import get_prediction_spec, predict_alert


def _apply_preset(prefix: str, spec: dict) -> None:
    values = spec.get("presets", {}).get(st.session_state.get(prefix + "preset"), {})
    for field in spec["fields"]:
        if field["name"] in values:
            convert = int if field.get("kind") == "integer" else float
            st.session_state[prefix + field["name"]] = convert(values[field["name"]])


def _show_result(result: dict, score_label: str) -> None:
    st.markdown("**Last prediction**")
    if result["review_recommended"]:
        st.success("Prediction: Prioritize for review")
    else:
        st.info("Prediction: Below review threshold")
    left, right = st.columns(2)
    left.metric(score_label, f"{result['score']:.3f}")
    right.metric("Saved review threshold", f"{result['threshold']:.3f}")
    st.caption("The score is a model prediction on a 0–1 scale, not a verified fraud probability. "
               "The actual analyst outcome is not yet observed; it is not needed to score this input.")
    st.text(f"Model: {result['model']} · Scored: {result['scored_at']}")
    st.caption(f"Request: {result['request_id']}")
    with st.expander("Inputs used for this prediction"):
        st.json(result["inputs"], expanded=True)
    st.caption("This result belongs to those submitted values. Editing the fields below does not change it; "
               "click Predict this alert to score the new values.")
    st.divider()


def render_prediction(settings, workspace: Path) -> bool:
    """Return False quietly for unsupported workspaces; score only on submission."""
    try:
        spec = get_prediction_spec(workspace)
    except Exception as exc:
        st.warning(f"Saved-model prediction is unavailable: {str(exc)[:300]}")
        return False
    if spec is None:
        return False
    prefix = f"prediction_{workspace.name}_"
    fields = spec["fields"]
    with st.expander("Predict a new alert", expanded=True):
        previous = st.session_state.get(prefix + "result")
        if previous:
            _show_result(previous, spec.get("score_label", "Predicted score"))
        else:
            st.write("Enter a new set of values and get a prediction from this workspace's saved model.")
        st.caption("Each click runs saved-model inference in Docker. The model is not retrained.")
        if spec.get("presets"):
            st.selectbox("Start from an example", ["Custom inputs", *spec["presets"]],
                         key=prefix + "preset", on_change=_apply_preset, args=(prefix, spec),
                         help="Examples fill the fields only. Click Predict this alert to score them.")
        with st.form(prefix + "form"):
            columns = st.columns(2)
            values = {}
            for index, field in enumerate(fields):
                convert = int if field.get("kind") == "integer" else float
                key = prefix + field["name"]
                if key not in st.session_state:
                    st.session_state[key] = convert(field["default"])
                minimum = field.get("min")
                maximum = field.get("max")
                with columns[index % 2]:
                    values[field["name"]] = st.number_input(
                        field["label"], key=key,
                        min_value=convert(minimum) if minimum is not None else None,
                        max_value=convert(maximum) if maximum is not None else None,
                        step=convert(field.get("step", 1 if convert is int else .1)),
                        help=field.get("help"),
                    )
            submitted = st.form_submit_button("Predict this alert", type="primary", width="stretch")
        if submitted:
            try:
                with st.spinner("Scoring the submitted values in Docker…"):
                    result = predict_alert(settings, workspace, values)
                st.session_state[prefix + "result"] = result
                st.rerun()
            except Exception as exc:
                st.error(f"Prediction could not be completed: {str(exc)[:300]}")
    return True
