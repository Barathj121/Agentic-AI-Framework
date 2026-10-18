#!/usr/bin/env bash
# Fresh Debian 12 VM only. Invoked by deploy.sh over IAP SSH.
set -euo pipefail
[[ $EUID == 0 ]] || { echo "Run with sudo on the new VM" >&2; exit 1; }
: "${CLARIFI_API_URL:?Set the private Cloud Run URL}"
: "${CLARIFI_REMOTE_MODEL:?Set the deployed Gemini model}"
: "${OLLAMA_IMAGE:?Set the Ollama image tag to install}"
: "${PUBLIC_HOST:?Set the public Cloud Run hostname}"
OLLAMA_MODEL="${OLLAMA_MODEL:-gemma3:4b}"
ARCHIVE="${1:?Supply the source archive path}"
[[ ! -e /opt/agentic-workbench ]] || { echo "Existing installation found; use the documented update procedure" >&2; exit 1; }
# Only expected resource identifiers may enter the systemd environment file.
[[ "$CLARIFI_API_URL" =~ ^https://[a-z0-9-]+(\.[a-z0-9-]+)?\.run\.app$ ]]
[[ "$CLARIFI_REMOTE_MODEL" =~ ^[a-zA-Z0-9._/-]+$ ]]
[[ "$OLLAMA_MODEL" =~ ^[a-zA-Z0-9._:/-]+$ ]]
[[ "$PUBLIC_HOST" =~ ^[a-z0-9-]+(\.[a-z0-9-]+)?\.run\.app$ ]]
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv docker.io ca-certificates curl libgomp1 nginx iptables
systemctl enable --now docker
useradd --system --user-group --create-home --home-dir /var/lib/agentic-workbench --shell /usr/sbin/nologin workbench
usermod -aG docker workbench
install -d -m 0755 /opt/agentic-workbench
tar --extract --gzip --file "$ARCHIVE" --directory /opt/agentic-workbench --no-same-owner
cd /opt/agentic-workbench
python3 -m venv .venv
.venv/bin/pip install -r requirements-clarifi.txt
docker build -t agentic-workbench-sandbox:latest -f sandbox/Dockerfile .
docker build -t workbench-example:demo -f examples/external_app/Dockerfile .
# Containers do not need the VM service-account metadata endpoint.
cat >/etc/systemd/system/workbench-container-network.service <<'EOF'
[Unit]
Description=Block VM metadata access from Docker containers
After=docker.service
Requires=docker.service
Before=agentic-workbench.service agentic-workbench-admin.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c '/usr/sbin/iptables -C DOCKER-USER -d 169.254.169.254/32 -j REJECT || /usr/sbin/iptables -I DOCKER-USER -d 169.254.169.254/32 -j REJECT'
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now workbench-container-network
docker run --detach --name workbench-public-demo --restart unless-stopped \
  --publish 127.0.0.1:18501:8501 --user 10001:10001 --read-only \
  --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 128 \
  --memory 256m --memory-swap 256m --cpus 1 --tmpfs /tmp:rw,nosuid,size=64m \
  --log-driver local --log-opt max-size=2m --log-opt max-file=1 workbench-example:demo
docker pull "$OLLAMA_IMAGE"
# Resolve the downloaded image to its immutable local ID for subsequent restarts.
OLLAMA_IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$OLLAMA_IMAGE")
install -d -m 0700 /var/lib/workbench-ollama
docker run --detach --name workbench-ollama --restart unless-stopped \
  --publish 127.0.0.1:11434:11434 \
  --volume /var/lib/workbench-ollama:/root/.ollama "$OLLAMA_IMAGE_ID"
ready=false
for attempt in {1..60}; do
  if curl --fail --silent http://127.0.0.1:11434/api/tags >/dev/null; then ready=true; break; fi
  sleep 2
done
[[ "$ready" == true ]] || { echo "Ollama did not become ready" >&2; exit 1; }
docker exec workbench-ollama ollama pull "$OLLAMA_MODEL"
cat >/etc/agentic-workbench.env <<EOF
WORKSPACE_ROOT=/var/lib/agentic-workbench/workspaces
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=$OLLAMA_MODEL
LLM_TIMEOUT_SECONDS=600
CLARIFI_API_URL=$CLARIFI_API_URL
CLARIFI_REMOTE_MODEL=$CLARIFI_REMOTE_MODEL
EOF
chmod 0640 /etc/agentic-workbench.env
cat >/etc/agentic-workbench-public.env <<EOF
PUBLIC_DEMO=true
MAX_UPLOAD_MB=25
STREAMLIT_SERVER_MAX_UPLOAD_SIZE=25
STREAMLIT_BROWSER_SERVER_ADDRESS=$PUBLIC_HOST
STREAMLIT_BROWSER_SERVER_PORT=443
EOF
chmod 0640 /etc/agentic-workbench-public.env
cat >/etc/systemd/system/agentic-workbench.service <<'EOF'
[Unit]
Description=Agentic Workbench - Streamlit, migration and ClariFi
After=network-online.target docker.service workbench-container-network.service
Wants=network-online.target
Requires=docker.service workbench-container-network.service

[Service]
User=workbench
Group=workbench
SupplementaryGroups=docker
WorkingDirectory=/opt/agentic-workbench
EnvironmentFile=/etc/agentic-workbench.env
Environment=PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/var/lib/agentic-workbench
ExecStart=/opt/agentic-workbench/.venv/bin/python -m streamlit run app.py --server.address=127.0.0.1 --server.port=8510 --server.headless=true
Restart=on-failure
RestartSec=5
UMask=0077
# Docker bind mounts must see the same /tmp paths as the host daemon.
PrivateTmp=false
ProtectSystem=strict
ReadWritePaths=/var/lib/agentic-workbench /tmp

[Install]
WantedBy=multi-user.target
EOF
# The original operator UI remains reachable only through IAP port forwarding.
sed 's/--server.port=8510/--server.port=8511/' /etc/systemd/system/agentic-workbench.service \
  >/etc/systemd/system/agentic-workbench-admin.service
install -d /etc/systemd/system/agentic-workbench.service.d
cat >/etc/systemd/system/agentic-workbench.service.d/public.conf <<'EOF'
[Service]
EnvironmentFile=/etc/agentic-workbench-public.env
EOF
install -m 0644 deploy/gcp/vm-public.conf /etc/nginx/sites-available/default
nginx -t
systemctl daemon-reload
systemctl enable --now agentic-workbench agentic-workbench-admin nginx
systemctl reload nginx
ready=false
for attempt in {1..60}; do
  if curl --fail --silent http://127.0.0.1:8510/_stcore/health >/dev/null; then ready=true; break; fi
  sleep 2
done
[[ "$ready" == true ]] || { journalctl -u agentic-workbench -n 60 --no-pager; exit 1; }
echo "Workbench installed. Verify model generation and both execution paths before the demo."
