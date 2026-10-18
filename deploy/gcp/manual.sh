#!/usr/bin/env bash
# Run only after the operator explicitly requests deployment of the supplied ref.
# This command never commits, pushes or merges; dirty working-tree files do not ship.
set -euo pipefail
cd "$(dirname "$0")/../.."
[[ "${GITHUB_ACTIONS:-false}" != true ]] || { echo 'Use workflow_dispatch for manual CI deployment.' >&2; exit 2; }
REFERENCE="${1:?Supply the branch, tag or full commit SHA explicitly}"
REVISION=$(git rev-parse --verify --end-of-options "$REFERENCE^{commit}")
git cat-file -e "$REVISION:deploy/gcp/update-vm.sh" || { echo 'Selected revision must contain the deployment updater.' >&2; exit 1; }
STAGING=$(mktemp -d)
python3 deploy/gcp/package.py "$STAGING/workbench-source.tar.gz" --revision "$REVISION"
export PROJECT_ID="${PROJECT_ID:-project-834ebdfa-aa5e-417f-b09}"
export ZONE="${ZONE:-us-central1-a}"
export VM="${VM:-clarifi-demo-vm}"
export PUBLIC_URL="${PUBLIC_URL:-https://clarifi-demo-web-n65xgyqlaa-uc.a.run.app}"
export MANUAL_DEPLOY=true
printf 'Manual source %s resolved to commit %s; local uncommitted changes are excluded.\n' "$REFERENCE" "$REVISION"
bash deploy/gcp/update.sh "$STAGING/workbench-source.tar.gz" "$REVISION"
