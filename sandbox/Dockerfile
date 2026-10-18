FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY sandbox/requirements.txt /opt/workbench/requirements.txt
RUN pip install --no-cache-dir -r /opt/workbench/requirements.txt \
    && groupadd --gid 10001 workbench \
    && useradd --uid 10001 --gid 10001 --no-create-home workbench \
    && mkdir -p /workspace/input /workspace/generated /workspace/database /workspace/results /workspace/models /workspace/notebooks
COPY sandbox/runner.py /opt/workbench/runner.py
COPY workbench/agents/clarifi/__init__.py workbench/agents/clarifi/schemas.py workbench/agents/clarifi/tools.py /opt/workbench/clarifi_runtime/
RUN chmod 0555 /opt/workbench/runner.py && chmod 0755 /workspace
USER 10001:10001
WORKDIR /workspace
CMD ["python", "-I", "/opt/workbench/runner.py", "execute", "/contract/contract.json"]
