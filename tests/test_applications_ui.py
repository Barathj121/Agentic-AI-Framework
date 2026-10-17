from streamlit.testing.v1 import AppTest

from workbench.applications import BUILTIN


def application_page():
    import streamlit as st
    from ui.applications import render_applications

    def navigate(page):
        st.session_state["selected_application"] = page

    render_applications(None, navigate=navigate)


class FakeRegistry:
    def __init__(self, settings):
        pass

    records = []
    calls = []

    def list(self):
        return [dict(BUILTIN), *[dict(record) for record in self.records]]

    def register(self, name, image, port, description, trusted=False):
        if not trusted:
            raise ValueError("Confirm that this is a trusted local image before registering it")
        self.calls.append(("register", name, image, port))
        self.records.append({"id": "app_test", "kind": "docker", "name": name, "image": image,
                             "image_id": "sha256:" + "a" * 64, "internal_port": port,
                             "description": description, "status": "registered", "container_name": None})

    def status(self, app_id):
        record = dict(self.records[0])
        record["url"] = "http://127.0.0.1:49152/" if record["status"] == "running" else None
        return record

    def start(self, app_id):
        self.calls.append(("start", app_id))
        self.records[0].update(status="running", container_name="test-container")

    def stop(self, app_id):
        self.calls.append(("stop", app_id))
        self.records[0].update(status="stopped", container_name=None)


def setup(monkeypatch):
    import ui.applications
    FakeRegistry.records = []
    FakeRegistry.calls = []
    monkeypatch.setattr(ui.applications, "ApplicationRegistry", FakeRegistry)
    return AppTest.from_function(application_page).run()


def button(app, label):
    return next(item for item in app.button if item.label == label)


def test_builtin_navigation_uses_callback(monkeypatch):
    app = setup(monkeypatch)
    assert not app.exception
    button(app, "Launch Migration Studio").click().run()
    assert not app.exception
    assert app.session_state["selected_application"] == "migration"
    assert not FakeRegistry.calls


def test_registration_requires_explicit_trust(monkeypatch):
    app = setup(monkeypatch)
    app.text_input[0].set_value("Demo")
    app.text_input[1].set_value("demo:latest")
    button(app, "Register application").click().run()
    assert "trusted local image" in app.error[0].value
    assert not FakeRegistry.calls


def test_register_launch_open_stop_controls(monkeypatch):
    app = setup(monkeypatch)
    app.text_input[0].set_value("Demo")
    app.text_input[1].set_value("demo:latest")
    app.checkbox[0].check()
    button(app, "Register application").click().run()
    assert not app.exception
    assert FakeRegistry.calls == [("register", "Demo", "demo:latest", 8501)]
    assert button(app, "Stop").disabled
    button(app, "Launch").click().run()
    assert not app.exception
    assert FakeRegistry.calls[-1] == ("start", "app_test")
    assert button(app, "Launch").disabled
    assert app.get("link_button")[0].proto.url == "http://127.0.0.1:49152/"
    button(app, "Stop").click().run()
    assert not app.exception
    assert FakeRegistry.calls[-1] == ("stop", "app_test")
    assert button(app, "Stop").disabled
    assert not app.get("link_button")
