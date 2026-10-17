"""Real local-image lifecycle check; build examples/external_app first."""
import time

import httpx
import pytest

from workbench.applications import ApplicationRegistry
from workbench.settings import Settings


@pytest.mark.docker
def test_real_application_http_and_stop(tmp_path):
    registry = ApplicationRegistry(Settings(workspace_root=tmp_path / "workspaces"))
    app = registry.register("Actual local demo", "workbench-launcher-demo:latest", 8501, trusted=True)
    try:
        status = registry.start(app["id"])
        assert status["status"] == "running"
        assert status["url"].startswith("http://127.0.0.1:")
        with httpx.Client(trust_env=False, timeout=3) as client:
            for attempt in range(30):
                try:
                    response = client.get(status["url"])
                    response.raise_for_status()
                    break
                except httpx.HTTPError:
                    if attempt == 29:
                        raise
                    time.sleep(0.5)
        assert "Customer Insights demo" in response.text
    finally:
        stopped = registry.stop(app["id"])
    assert stopped["status"] == "stopped"
    assert stopped["url"] is None
