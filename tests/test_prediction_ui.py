from __future__ import annotations

import copy
from pathlib import Path

import pytest


@pytest.fixture
def prediction_ui(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from ui import prediction

    spec = {
        "model_name": "Saved test model", "threshold": .4, "score_label": "Predicted escalation score",
        "fields": [
            {"name": "activity", "label": "Recent activity", "kind": "integer", "default": 2,
             "min": 0, "max": 100, "step": 1, "help": "A prior activity count."},
            {"name": "ratio", "label": "Relative amount", "kind": "number", "default": 1.0,
             "min": .1, "max": 20., "step": .1, "help": "A historical comparison."},
        ],
        "presets": {"Low activity": {"activity": 1, "ratio": .5},
                    "Unusual activity": {"activity": 20, "ratio": 4.0}},
    }
    calls = []

    def infer(settings, workspace, values):
        calls.append({"workspace": workspace, "values": copy.deepcopy(values)})
        score = min(values["ratio"] / 10, .99)
        return {"request_id": f"request-{len(calls)}", "score": score, "threshold": .4,
                "review_recommended": score >= .4, "model": "Saved test model",
                "scored_at": "2026-10-15T02:00:00+00:00", "inputs": copy.deepcopy(values)}

    monkeypatch.setattr(prediction, "get_prediction_spec", lambda workspace: spec)
    monkeypatch.setattr(prediction, "predict_alert", infer)
    app = AppTest.from_string(
        "from pathlib import Path\nfrom ui.prediction import render_prediction\n"
        f"render_prediction(None, Path({str(tmp_path)!r}))\n",
        default_timeout=20,
    )
    return app, calls, prediction, spec


def test_presets_and_input_changes_require_an_explicit_prediction_click(prediction_ui):
    app, calls, _, _ = prediction_ui
    app.run()
    assert not app.exception
    assert not calls
    assert len(app.number_input) == 2
    app.selectbox[0].select("Unusual activity").run()
    assert not app.exception
    assert {item.label: item.value for item in app.number_input} == {
        "Recent activity": 20, "Relative amount": 4.0,
    }
    assert not calls
    next(item for item in app.button if item.label == "Predict this alert").click().run()
    assert not app.exception
    assert [item["values"] for item in calls] == [{"activity": 20, "ratio": 4.0}]
    assert any(item.value == "Prediction: Prioritize for review" for item in app.success)
    assert any(item.label == "Predicted escalation score" and item.value == "0.400" for item in app.metric)
    assert any("request-1" in item.value for item in app.caption)

    app.selectbox[0].select("Low activity").run()
    assert len(calls) == 1
    assert any(item.label == "Predicted escalation score" and item.value == "0.400" for item in app.metric)
    assert any("Editing the fields below does not change it" in item.value for item in app.caption)
    assert any(item.label == "Inputs used for this prediction" for item in app.expander)
    next(item for item in app.button if item.label == "Predict this alert").click().run()
    assert not app.exception
    assert len(calls) == 2
    assert calls[1]["values"] == {"activity": 1, "ratio": .5}
    assert any(item.value == "Prediction: Below review threshold" for item in app.info)
    assert any(item.label == "Predicted escalation score" and item.value == "0.050" for item in app.metric)
    assert any("request-2" in item.value for item in app.caption)
    assert any("actual analyst outcome is not yet observed" in item.value for item in app.caption)


def test_prediction_failure_is_visible_and_does_not_replace_previous_score(prediction_ui, monkeypatch):
    app, calls, prediction, _ = prediction_ui
    app.run()
    next(item for item in app.button if item.label == "Predict this alert").click().run()
    assert len(calls) == 1

    def failed(*args):
        raise RuntimeError("Docker could not start")

    monkeypatch.setattr(prediction, "predict_alert", failed)
    next(item for item in app.button if item.label == "Predict this alert").click().run()
    assert not app.exception
    assert any("Docker could not start" in item.value for item in app.error)
    assert any("request-1" in item.value for item in app.caption)
    assert any(item.label == "Predicted escalation score" and item.value == "0.100" for item in app.metric)


def test_unsupported_workspace_hides_prediction_ui(prediction_ui, monkeypatch):
    app, calls, prediction, _ = prediction_ui
    monkeypatch.setattr(prediction, "get_prediction_spec", lambda workspace: None)
    app.run()
    assert not app.exception
    assert not app.button and not app.number_input and not app.warning
    assert not calls


def test_unreadable_saved_model_shows_recoverable_warning(prediction_ui, monkeypatch):
    app, calls, prediction, _ = prediction_ui

    def unavailable(workspace):
        raise ValueError("Saved model validation no longer matches")

    monkeypatch.setattr(prediction, "get_prediction_spec", unavailable)
    app.run()
    assert not app.exception
    assert any("Saved model validation no longer matches" in item.value for item in app.warning)
    assert not calls
