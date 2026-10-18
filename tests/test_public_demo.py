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


def open_home():
    return AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=30).run()


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
