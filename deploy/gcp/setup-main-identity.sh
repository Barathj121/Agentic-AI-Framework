#!/usr/bin/env bash
# One-time keyless CI identity setup. Does NOT deploy, build, start or resize anything.
set -euo pipefail
PROJECT_ID="${PROJECT_ID:-project-834ebdfa-aa5e-417f-b09}"
ZONE="${ZONE:-us-central1-a}"
VM="${VM:-clarifi-demo-vm}"
REPOSITORY=Barathj121/Agentic-AI-Framework
POOL=clarifi-github-main
PROVIDER=github
SA_NAME=clarifi-main-deploy
[[ "$PROJECT_ID" =~ ^[a-z][a-z0-9-]+[a-z0-9]$ && "$ZONE" =~ ^[a-z]+-[a-z]+[0-9]-[a-z]$ && "$VM" =~ ^[a-z][a-z0-9-]+$ ]] || exit 2
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
VM_SA=$(gcloud compute instances describe "$VM" --project="$PROJECT_ID" --zone="$ZONE" --format='value(serviceAccounts[0].email)')
VM_IP=$(gcloud compute instances describe "$VM" --project="$PROJECT_ID" --zone="$ZONE" --format='value(networkInterfaces[0].networkIP)')
[[ "$PROJECT_NUMBER" =~ ^[0-9]+$ && "$VM_IP" =~ ^10\.42\.0\.[0-9]+$ && "$VM_SA" == *"@$PROJECT_ID.iam.gserviceaccount.com" ]] || exit 2
# Numeric identities prevent a renamed/deleted repository or owner being impersonated.
IDENTIFIERS=$(python3 - <<'PY'
import json, os, subprocess, urllib.error, urllib.request
url = 'https://api.github.com/repos/Barathj121/Agentic-AI-Framework'
headers = {'User-Agent': 'ClariFi-deployment-setup'}
def read_repo():
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as response:
        return json.load(response)
try:
    repo = read_repo()
except urllib.error.HTTPError as error:
    if error.code != 404:
        raise SystemExit('GitHub repository metadata lookup failed; no CI identity changes were made') from None
    # Private repositories return 404 anonymously. Reuse the existing Git login
    # for this exact repository, in memory; never print or write the credential.
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        result = subprocess.run(['git', 'credential', 'fill'], input='protocol=https\nhost=github.com\npath=Barathj121/Agentic-AI-Framework.git\n\n',
                                text=True, capture_output=True, env={**os.environ, 'GIT_TERMINAL_PROMPT':'0'})
        fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        token = fields.get('password')
    if not token:
        raise SystemExit('Sign in to this GitHub repository through Git, or supply GH_TOKEN privately, before identity setup')
    headers['Authorization'] = 'Bearer ' + token
    try:
        repo = read_repo()
    except Exception:
        raise SystemExit('Authenticated GitHub metadata lookup failed; check repository access') from None
assert repo['full_name'] == 'Barathj121/Agentic-AI-Framework'
print(str(int(repo['id'])) + ' ' + str(int(repo['owner']['id'])))
PY
)
read -r REPOSITORY_ID OWNER_ID <<< "$IDENTIFIERS"
SA="$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
CONDITION="assertion.repository_id == '$REPOSITORY_ID' && assertion.repository_owner_id == '$OWNER_ID' && assertion.repository == '$REPOSITORY' && assertion.ref == 'refs/heads/main' && assertion.event_name in ['push', 'workflow_dispatch'] && assertion.workflow_ref == '$REPOSITORY/.github/workflows/deploy-main.yml@refs/heads/main'"
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com --project="$PROJECT_ID"
if ! gcloud iam service-accounts describe "$SA" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" --project="$PROJECT_ID" --display-name='ClariFi main-only VM deployment'
fi
if ! gcloud iam workload-identity-pools describe "$POOL" --project="$PROJECT_ID" --location=global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "$POOL" --project="$PROJECT_ID" --location=global --display-name='ClariFi GitHub main pushes'
fi
if gcloud iam workload-identity-pools providers describe "$PROVIDER" --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL" >/dev/null 2>&1; then
  EXISTING=$(gcloud iam workload-identity-pools providers describe "$PROVIDER" --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL" --format='value(attributeCondition)')
  PRIOR_CONDITION=$(python3 - "$CONDITION" <<'PYCOND'
import sys
print(sys.argv[1].replace("assertion.event_name in ['push', 'workflow_dispatch']", "assertion.event_name == 'push'"))
PYCOND
)
  if [[ "$EXISTING" == "$PRIOR_CONDITION" ]]; then
    gcloud iam workload-identity-pools providers update-oidc "$PROVIDER" --project="$PROJECT_ID" --location=global \
      --workload-identity-pool="$POOL" --attribute-condition="$CONDITION"
  else
    [[ "$EXISTING" == "$CONDITION" ]] || { echo 'Existing provider differs; inspect it instead of silently broadening trust.' >&2; exit 1; }
  fi
else
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --project="$PROJECT_ID" --location=global \
    --workload-identity-pool="$POOL" --issuer-uri=https://token.actions.githubusercontent.com \
    --attribute-mapping='google.subject=assertion.sub,attribute.repository_id=assertion.repository_id' \
    --attribute-condition="$CONDITION"
fi
gcloud iam service-accounts add-iam-policy-binding "$SA" --project="$PROJECT_ID" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL/attribute.repository_id/$REPOSITORY_ID" --quiet --format=none
# Read project/instance metadata; no VM creation/start/resize permission.
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" --role=roles/compute.viewer --condition=None --quiet --format=none
# CI needs sudo to replace application code and restart the two existing services.
gcloud compute instances add-iam-policy-binding "$VM" --project="$PROJECT_ID" --zone="$ZONE" \
  --member="serviceAccount:$SA" --role=roles/compute.osAdminLogin --quiet --format=none
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" --role=roles/iap.tunnelResourceAccessor \
  --condition="expression=destination.port == 22 && destination.ip == '$VM_IP',title=clarifi-main-ssh-only" --quiet --format=none
gcloud iam service-accounts add-iam-policy-binding "$VM_SA" --project="$PROJECT_ID" \
  --member="serviceAccount:$SA" --role=roles/iam.serviceAccountUser --quiet --format=none
printf 'GCP_WIF_PROVIDER=projects/%s/locations/global/workloadIdentityPools/%s/providers/%s\n' "$PROJECT_NUMBER" "$POOL" "$PROVIDER"
printf 'GCP_DEPLOY_SERVICE_ACCOUNT=%s\nNo application deployment was performed.\n' "$SA"
