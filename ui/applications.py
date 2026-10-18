"""Native Streamlit controls for the built-in and trusted local applications."""
from __future__ import annotations

import streamlit as st

from workbench.applications import ApplicationRegistry
from workbench.settings import public_demo_enabled


def render_applications(settings, navigate=None):
    """Render the registry; navigate('migration') opens the built-in application."""
    st.subheader("Applications")
    if public_demo_enabled():
        st.write("Explore the workbench and a preconfigured external application.")
        with st.container(border=True):
            st.markdown("**Migration Studio**")
            st.write("Review mappings and transform your data inside the workbench.")
            if st.button("Launch Migration Studio", key="application_builtin_migration"):
                if navigate is not None:
                    navigate("migration")
                else:
                    st.session_state["page"] = "Migration Studio"
                st.rerun()
        with st.container(border=True):
            st.markdown("**Customer Insights demo**")
            st.write("Open the preconfigured Docker application in your browser.")
            st.link_button("Open application", "/apps/demo/")
        return
    st.write("Open Migration Studio or launch a trusted application image already installed in Docker.")
    registry = ApplicationRegistry(settings)
    with st.expander("Register an external Docker application", expanded=False):
        st.caption("Applications run as user 10001 with a read-only filesystem and writable /tmp, "
                   "2 GB memory and 2 CPUs. No host folders are mounted. Applications must listen on "
                   "0.0.0.0 inside the container. Their local URL is available only on this computer.")
        with st.form("register_external_application"):
            name = st.text_input("Application name", max_chars=100)
            image = st.text_input("Local Docker image", placeholder="customer-insights:latest", max_chars=255)
            port = st.number_input("Internal application port", min_value=1, max_value=65535, value=8501, step=1)
            description = st.text_area("Description", max_chars=2000)
            trusted = st.checkbox("I trust this local image and want to make it available to launch.")
            submitted = st.form_submit_button("Register application")
        if submitted:
            try:
                registry.register(name, image, int(port), description, trusted=trusted)
                st.success("Application registered. Click Launch below to start it.")
            except Exception as exc:
                st.error(str(exc)[:1200])
    try:
        applications = registry.list()
    except Exception as exc:
        st.error(f"Cannot read applications: {exc}")
        return
    for app in applications:
        with st.container(border=True):
            st.markdown(f"**{app['name']}**")
            st.write(app["description"])
            if app["kind"] == "builtin":
                if st.button("Launch Migration Studio", key="application_builtin_migration"):
                    if navigate is not None:
                        navigate(app["page"])
                    else:
                        st.session_state["page"] = app["page"]
                    st.rerun()
                continue
            st.caption(f"Image: {app['image']} · Internal port: {app['internal_port']}")
            with st.expander("Registered image identity"):
                st.code(app["image_id"], language=None)
                st.caption("The registered image ID is used even if its tag later changes.")
            try:
                status = registry.status(app["id"])
                st.write(f"Status: {status['status']}")
            except Exception as exc:
                st.error(f"Status unavailable: {str(exc)[:1200]}")
                continue
            controls = st.columns(3)
            if controls[0].button("Launch", key=f"launch_{app['id']}", disabled=status["status"] == "running"):
                try:
                    with st.spinner("Starting local application…"):
                        registry.start(app["id"])
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc)[:1200])
            if status.get("url"):
                controls[1].link_button("Open application", status["url"])
            if controls[2].button("Stop", key=f"stop_{app['id']}", disabled=not app.get("container_name")):
                try:
                    with st.spinner("Stopping local application…"):
                        registry.stop(app["id"])
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc)[:1200])
    st.button("Refresh application status", key="refresh_applications")
