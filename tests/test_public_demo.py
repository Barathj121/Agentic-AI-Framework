from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from workbench.service import Service
from workbench.settings import Settings


@pytest.fixture
def public_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_DEMO", "true")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(Service, "diagnostics", lambda self: {})
    st.cache_resource.clear()
    yield tmp_path
    st.cache_resource.clear()


def open_home(session_id=None):
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=30)
    if session_id is not None:
        app.query_params["public_session_id"] = session_id
    return app.run()


def test_public_visitors_do_not_share_workspace_history(public_environment):
    first = open_home()
    second = open_home()
    assert not first.exception and not second.exception
    first_scope = first.session_state["public_visitor_scope"]
    second_scope = second.session_state["public_visitor_scope"]
    assert first_scope != second_scope
    first_service = Service(Settings(workspace_root=public_environment / "public-sessions" / first_scope))
    first_service.create_workspace("Visitor one private case", "Sum amounts", b"amount\n1\n", "data.csv")
    first.run()
    second.run()
    assert not first.exception and not second.exception
    assert first.dataframe[0].value["Name"].tolist() == ["Visitor one private case"]
    assert not second.dataframe
    # Existing private operator workspaces are not listed by public visitors either.
    Service(Settings(workspace_root=public_environment)).create_workspace(
        "Operator case", "Sum", b"amount\n2\n", "data.csv")
    second.run()
    assert not second.dataframe
    assert second.session_state["public_visitor_scope"] == second_scope


def test_public_application_demo_has_no_docker_management(public_environment, monkeypatch):
    from ui import applications

    def forbidden(*args, **kwargs):
        raise AssertionError("Anonymous visitors must not operate the Docker registry")

    monkeypatch.setattr(applications, "ApplicationRegistry", forbidden)
    app = open_home()
    next(item for item in app.radio if item.label == "Workspace workflow").set_value("External applications").run()
    assert not app.exception
    assert not app.text_input
    assert not any(item.label in {"Register application", "Launch", "Stop"} for item in app.button)
    assert app.get("link_button")[0].proto.url == "/apps/demo/"
    next(item for item in app.button if item.label == "Launch Migration Studio").click().run()
    assert not app.exception
    assert app.session_state["page"] == "Migration Studio"


def test_public_service_rejects_unscoped_and_traversal_access(public_environment):
    import app

    for scope in ["", "../operator", "/tmp", "a" * 31, "A" * 32]:
        with pytest.raises(ValueError, match="server-generated"):
            app.get_service(scope)


def test_private_link_reopens_saved_history_after_new_browser_session(public_environment):
    first = open_home()
    scope = first.session_state["public_visitor_scope"]
    assert first.query_params["public_session_id"] == scope
    Service(Settings(workspace_root=public_environment / "public-sessions" / scope)).create_workspace(
        "Saved migration review", "Review records", b"amount\n1\n", "data.csv")
    # Clearing the process cache also exercises a server restart, where only
    # the on-disk scope and the private link survive.
    st.cache_resource.clear()
    resumed = open_home(scope)
    assert not resumed.exception
    assert resumed.session_state["public_visitor_scope"] == scope
    assert resumed.dataframe[0].value["Name"].tolist() == ["Saved migration review"]
    unrelated = open_home()
    assert unrelated.session_state["public_visitor_scope"] != scope
    assert not unrelated.dataframe


@pytest.mark.parametrize("scope", ["", "../operator", "/tmp", "a" * 31, "A" * 32, "f" * 32])
def test_invalid_private_link_never_creates_or_opens_a_scope(public_environment, scope):
    app = open_home(scope)
    assert not app.exception
    assert any("invalid or no longer available" in item.value for item in app.error)
    assert "public_visitor_scope" not in app.session_state
    assert not (public_environment / "public-sessions").exists()
    next(item for item in app.button if item.label == "Start a new private workspace session").click().run()
    assert not app.exception
    assert app.session_state["public_visitor_scope"] != scope
    assert app.query_params["public_session_id"] == app.session_state["public_visitor_scope"]


def test_switching_private_links_clears_previous_session_results(public_environment):
    first = open_home()
    second = open_home()
    second_scope = second.session_state["public_visitor_scope"]
    first.session_state["cached_report_from_previous_visitor"] = {"private": "data"}
    first.session_state["workspace_id"] = "uc_previous_case"
    first.query_params["public_session_id"] = second_scope
    first.run()
    assert not first.exception
    assert first.session_state["public_visitor_scope"] == second_scope
    assert "cached_report_from_previous_visitor" not in first.session_state
    assert "workspace_id" not in first.session_state


def test_existing_browser_session_receives_a_resume_link(public_environment):
    app = open_home()
    original_scope = app.session_state["public_visitor_scope"]
    app.query_params.clear()
    app.run()
    assert not app.exception
    assert app.session_state["public_visitor_scope"] == original_scope
    assert app.query_params["public_session_id"] == original_scope
