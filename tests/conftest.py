import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def pytest_collection_modifyitems(items):
    for item in items:
        for marker, variable in (("docker", "RUN_DOCKER_TESTS"), ("live_ollama", "RUN_OLLAMA_TESTS")):
            if marker in item.keywords and os.getenv(variable) != "1":
                item.add_marker(pytest.mark.skip(reason=f"Set {variable}=1 to run this integration test"))
