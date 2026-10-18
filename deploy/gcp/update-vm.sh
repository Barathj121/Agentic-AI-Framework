#!/usr/bin/env bash
# Executed on the existing VM as root by the deployment workflow.
set -Eeuo pipefail
[[ $EUID == 0 ]] || { echo 'Run as root on the existing VM.' >&2; exit 2; }
ARCHIVE=$(realpath "${1:?Source archive required}")
REVISION="${2:?Commit SHA required}"
CHECKSUM="${3:?Archive checksum required}"
[[ "$REVISION" =~ ^[0-9a-f]{40}$ && "$CHECKSUM" =~ ^[0-9a-f]{64}$ ]] || exit 2
echo "$CHECKSUM  $ARCHIVE" | sha256sum --check --status
exec 9>/var/lock/agentic-workbench-deploy.lock
flock -n 9 || { echo 'Another deployment is active.' >&2; exit 1; }
CURRENT=/opt/agentic-workbench
RELEASES=/opt/agentic-workbench-releases
RELEASE="$RELEASES/$REVISION-$(date -u +%Y%m%dT%H%M%SZ)-$$"
[[ -d "$CURRENT/.venv" && -f /etc/agentic-workbench.env ]] || { echo 'Existing installation required; bootstrap is not run by CI.' >&2; exit 1; }
for service in agentic-workbench agentic-workbench-admin; do
  systemctl is-active --quiet "$service"
done
if [[ -f /var/lib/agentic-workbench/deployed-release-sha ]] && [[ "$(cat /var/lib/agentic-workbench/deployed-release-sha)" == "$REVISION" ]]; then
  echo 'This revision is already deployed.'
  exit 0
fi
AVAILABLE_KB=$(df -Pk /opt | awk 'NR==2 {print $4}')
(( AVAILABLE_KB >= 8*1024*1024 )) || { echo 'At least 8 GiB free disk space is required before preparing a release.' >&2; exit 1; }
install -d -m 0755 "$RELEASE"
# Refuse links, traversal, credentials and special files before extracting as root.
python3 - "$ARCHIVE" "$RELEASE" <<'PY'
import pathlib, sys, tarfile
with tarfile.open(sys.argv[1]) as archive:
    members = archive.getmembers()
    if len(members) > 3000 or sum(m.size for m in members) > 100*1024*1024:
        raise ValueError('Release archive exceeds supported limits')
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if (not member.isfile() or path.is_absolute() or '..' in path.parts
                or any(p in {'.git', '.venv', 'workspaces'} or p.startswith('.env') for p in path.parts)
                or path.suffix.lower() in {'.key', '.pem', '.p12'}):
            raise ValueError('Unsupported release archive member')
    archive.extractall(sys.argv[2], members=members)
PY
cd "$RELEASE"
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements-clarifi.txt
.venv/bin/python -c 'import streamlit, duckdb, google.adk, workbench.service'
IMAGE="agentic-workbench-sandbox:$REVISION"
docker build -t "$IMAGE" -f sandbox/Dockerfile .
docker run --rm --network none --user 10001:10001 --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --pids-limit 128 --memory 512m --cpus 1 \
  "$IMAGE" python -c 'from clarifi_runtime.schemas import InvestigationInput; from clarifi_runtime.tools import TOOLS; assert TOOLS'
# Preserve the current environment, models, workspaces and application workflow.
# Both services continue using /opt/agentic-workbench and the existing systemd units.
PREVIOUS=$(readlink -f "$CURRENT")
if [[ ! -L "$CURRENT" ]]; then PREVIOUS="$RELEASES/legacy-$(date -u +%Y%m%dT%H%M%SZ)"; fi
STATE=$(mktemp -d /tmp/workbench-release.XXXXXX)
ENV_FILE=/etc/agentic-workbench-release.env
HAD_ENV=false
if [[ -f "$ENV_FILE" ]]; then cp "$ENV_FILE" "$STATE/previous.env"; HAD_ENV=true; fi
SWITCHED=false
STOPPED=false
rollback() {
  code=$?
  trap - ERR
  set +e
  rollback_failed=false
  if [[ "$SWITCHED" == true ]]; then
    if ! ln -s "$PREVIOUS" "$CURRENT.rollback.$$" || ! python3 -c 'import os,sys; os.replace(sys.argv[1],sys.argv[2])' "$CURRENT.rollback.$$" "$CURRENT"; then rollback_failed=true; fi
    if [[ "$HAD_ENV" == true ]]; then
      cp "$STATE/previous.env" "$ENV_FILE" || rollback_failed=true
    else
      : > "$ENV_FILE" || rollback_failed=true
    fi
    chmod 0640 "$ENV_FILE" || rollback_failed=true
  fi
  if [[ "$STOPPED" == true ]]; then
    systemctl daemon-reload || rollback_failed=true
    systemctl restart agentic-workbench agentic-workbench-admin || rollback_failed=true
    curl --fail --silent --retry 10 --retry-connrefused --retry-delay 2 --max-time 5 http://127.0.0.1:8510/_stcore/health || rollback_failed=true
    curl --fail --silent --retry 10 --retry-connrefused --retry-delay 2 --max-time 5 http://127.0.0.1:8511/_stcore/health || rollback_failed=true
  fi
  if [[ "$rollback_failed" == true ]]; then
    echo "Deployment and automatic recovery failed. Restore the retained release at $PREVIOUS and inspect systemd logs." >&2
  else
    echo 'Deployment failed; previous application and health checks restored.' >&2
  fi
  exit "$code"
}
trap rollback ERR
STOPPED=true
systemctl stop agentic-workbench agentic-workbench-admin
if [[ ! -L "$CURRENT" ]]; then mv "$CURRENT" "$PREVIOUS"; fi
SWITCHED=true
ln -s "$RELEASE" "$CURRENT.next.$$"
python3 -c 'import os,sys; os.replace(sys.argv[1],sys.argv[2])' "$CURRENT.next.$$" "$CURRENT"
printf 'SANDBOX_IMAGE=%s\nWORKBENCH_RELEASE_SHA=%s\n' "$IMAGE" "$REVISION" > "$ENV_FILE"
chmod 0640 "$ENV_FILE"
for service in agentic-workbench agentic-workbench-admin; do
  install -d "/etc/systemd/system/$service.service.d"
  printf '[Service]\nEnvironmentFile=/etc/agentic-workbench-release.env\n' > "/etc/systemd/system/$service.service.d/zz-release.conf"
done
systemctl daemon-reload
systemctl restart agentic-workbench agentic-workbench-admin
# A failed health probe triggers rollback; no financial/model requests are made.
curl --fail --silent --show-error --retry 20 --retry-connrefused --retry-delay 2 --max-time 5 http://127.0.0.1:8510/_stcore/health
curl --fail --silent --show-error --retry 20 --retry-connrefused --retry-delay 2 --max-time 5 http://127.0.0.1:8511/_stcore/health
trap - ERR
printf '%s\n' "$REVISION" > /var/lib/agentic-workbench/deployed-release-sha
printf 'Application revision %s is healthy. Previous release retained at %s.\n' "$REVISION" "$PREVIOUS"
# No automatic deletion of old releases, images, models or financial evidence.
