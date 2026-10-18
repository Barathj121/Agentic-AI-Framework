#!/usr/bin/env bash
# Update existing infrastructure only. Automatic CI is main-only; operators can request manual updates.
set -euo pipefail
: "${PROJECT_ID:?Set PROJECT_ID}"
: "${ZONE:?Set ZONE}"
: "${VM:?Set VM}"
: "${PUBLIC_URL:?Set the existing Cloud Run gateway URL}"
if [[ "${MANUAL_DEPLOY:-false}" == true && "${GITHUB_ACTIONS:-false}" != true ]]; then
  echo 'Explicit operator-requested deployment; no Git push is performed.'
else
  [[ "${GITHUB_EVENT_NAME:-}" == push || "${GITHUB_EVENT_NAME:-}" == workflow_dispatch ]] || { echo 'Deployment requires main CI or explicit manual operator mode.' >&2; exit 2; }
  [[ "${GITHUB_REF:-}" == refs/heads/main && "${GITHUB_REPOSITORY:-}" == Barathj121/Agentic-AI-Framework ]] || exit 2
fi
ARCHIVE="${1:?Supply the committed source archive}"
REVISION="${2:?Supply the commit SHA}"
[[ "$REVISION" =~ ^[0-9a-f]{40}$ ]] || exit 2
if [[ "${MANUAL_DEPLOY:-false}" != true || "${GITHUB_ACTIONS:-false}" == true ]]; then
  if [[ "${GITHUB_EVENT_NAME:-}" == push ]]; then
    [[ "$REVISION" == "${GITHUB_SHA:-}" ]] || exit 2
  else
    [[ "$REVISION" == "${DEPLOY_REVISION:-}" ]] || exit 2
  fi
fi
[[ "$PROJECT_ID" =~ ^[a-z][a-z0-9-]+[a-z0-9]$ && "$ZONE" =~ ^[a-z]+-[a-z]+[0-9]-[a-z]$ && "$VM" =~ ^[a-z][a-z0-9-]+$ ]] || exit 2
[[ "$PUBLIC_URL" =~ ^https://[a-z0-9-]+(\.[a-z0-9-]+)?\.run\.app$ ]] || exit 2
[[ -f "$ARCHIVE" ]] || exit 2
state=$(gcloud compute instances describe "$VM" --project="$PROJECT_ID" --zone="$ZONE" --format='value(status)')
[[ "$state" == RUNNING ]] || { echo 'VM must already be running; CI will not start or resize it.' >&2; exit 1; }
CHECKSUM=$(sha256sum "$ARCHIVE" | cut -d' ' -f1)
REMOTE="workbench-source-$REVISION.tar.gz"
KEY="${GCP_SSH_KEY_FILE:-${RUNNER_TEMP:-${TMPDIR:-/tmp}}/workbench-ssh}"
gcloud compute scp "$ARCHIVE" "$VM:~/$REMOTE" --project="$PROJECT_ID" --zone="$ZONE" \
  --tunnel-through-iap --ssh-key-file="$KEY" --quiet
# Arguments contain validated hashes and a fixed filename, never user source text or secrets.
gcloud compute ssh "$VM" --project="$PROJECT_ID" --zone="$ZONE" --tunnel-through-iap \
  --ssh-key-file="$KEY" --quiet \
  --command="set -eu; echo '$CHECKSUM  $REMOTE' | sha256sum --check --status; tar -xOf '$REMOTE' deploy/gcp/update-vm.sh > 'workbench-update-$REVISION.sh'; sudo bash 'workbench-update-$REVISION.sh' '$REMOTE' '$REVISION' '$CHECKSUM'"
curl --fail --silent --show-error --retry 5 --retry-delay 2 --max-time 20 "$PUBLIC_URL/_stcore/health"
echo "Deployed commit $REVISION through the existing Cloud Run URL."
