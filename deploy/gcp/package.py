"""Package source for a fresh GCP VM, excluding credentials and local user data."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]
DIRECTORIES = {"workbench", "ui", "sandbox", "examples", "scripts", "tests", ".streamlit", "deploy"}
FILES = {"app.py", "requirements.txt", "requirements-clarifi.txt", "pyproject.toml", ".dockerignore"}


def package(destination: Path):
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT).decode().split("\0")
    with destination.open("xb") as output, tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name in sorted(set(filter(None, names))):
            path = Path(name)
            if path.parts[0] not in DIRECTORIES and name not in FILES:
                continue
            if any(part in {"__pycache__", "node_modules", ".venv", "workspaces", ".git"}
                   or part.startswith(".env") for part in path.parts):
                continue
            if path.suffix.lower() in {".pem", ".key", ".p12", ".pyc"}:
                continue
            source = ROOT / path
            if source.is_symlink() or not source.is_file():
                continue
            archive.add(source, arcname=name, recursive=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New archive path; existing files are never overwritten")
    package(parser.parse_args().output)
