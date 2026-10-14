"""Report local dependencies without starting engines, pulling models, or modifying settings."""
from __future__ import annotations

import json
import platform
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    from workbench.service import Service
    from workbench.settings import Settings

    settings = Settings.from_env()
    report = {"python": platform.python_version(), "python_executable": sys.executable,
              "platform": platform.platform(), "docker_command": shutil.which("docker"),
              "ollama_command": shutil.which("ollama"), "workspace_root": str(settings.workspace_root.resolve()),
              "configured_provider": settings.llm_provider,
              "configured_model": settings.ollama_model if settings.llm_provider == "ollama" else settings.llm_model,
              "sandbox_image": settings.sandbox_image}
    try:
        report["services"] = Service(settings).diagnostics()
    except Exception as exc:
        report["diagnostic_error"] = str(exc)
    print(json.dumps(report, indent=2, default=str))
    print("\nIf Docker is unavailable, start Docker Desktop (WSL2, Linux containers) or Rancher Desktop (Moby/dockerd).")
    print("If the sandbox image is missing: docker build -t agentic-workbench-sandbox:latest -f sandbox/Dockerfile .")
    if settings.llm_provider == "ollama":
        print(f"If the model is missing: ollama pull {settings.ollama_model}")
    else:
        print("If the model is missing: enable the local model API in Bionic/LM Studio, load the model, and set LLM_MODEL to its exact /v1/models ID.")
    services = report.get("services", {})
    model = services.get("llm", services.get("ollama", {}))
    docker = services.get("docker", {})
    ready = (model.get("available") and model.get("model_available") and
             docker.get("available") and docker.get("image_available"))
    return 0 if ready and "diagnostic_error" not in report else 1


if __name__ == "__main__":
    raise SystemExit(main())
