#!/usr/bin/env bash
# Fresh full-workbench installation. Creates billable resources; no automatic deletion.
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${PROJECT_ID:?Set the GCP project ID, not its numeric project number}"
: "${REGION:?Set REGION}"
: "${ZONE:?Set ZONE}"
: "${TAG:?Set a unique build tag}"
: "${CLARIFI_EVIDENCE_BUCKET:?Set a globally unique NEW private bucket name}"
: "${OLLAMA_IMAGE:?Choose an explicit ollama/ollama release tag}"
PREFIX="${PREFIX:-agentic-workbench}"
CLARIFI_MODE="${CLARIFI_MODE:-gemini}"
CLARIFI_API_MODEL="${CLARIFI_API_MODEL:-gemini-3.5-flash-lite}"
OLLAMA_MODEL="${OLLAMA_MODEL:-gemma3:4b}"
CLARIFI_LOCAL_MODEL="${CLARIFI_LOCAL_MODEL:-$OLLAMA_MODEL}"
MACHINE_TYPE="${MACHINE_TYPE:-e2-standard-8}"
BOOT_DISK_GB="${BOOT_DISK_GB:-100}"
[[ "$PROJECT_ID" =~ ^[a-z][a-z0-9-]{4,61}[a-z0-9]$ ]]
[[ "$PREFIX" =~ ^[a-z][a-z0-9-]{2,19}$ ]]
[[ "$REGION" =~ ^[a-z]+-[a-z]+[0-9]+$ && "$ZONE" == "$REGION"-[a-z] ]]
[[ "$TAG" =~ ^[a-zA-Z0-9_.-]+$ && "$BOOT_DISK_GB" =~ ^[0-9]+$ ]]
[[ "$OLLAMA_IMAGE" =~ ^ollama/ollama:[a-zA-Z0-9._-]+$ && "$OLLAMA_IMAGE" != *REPLACE* && "$OLLAMA_IMAGE" != *:latest ]]
[[ "$OLLAMA_MODEL" =~ ^[a-zA-Z0-9._:/-]+$ ]]
[[ "$CLARIFI_LOCAL_MODEL" =~ ^[a-zA-Z0-9._:/-]+$ ]]
if [[ "$CLARIFI_MODE" == gemini ]]; then
  : "${CLARIFI_API_KEY_SECRET:?Set an existing Secret Manager name:version, never the API key}"
  [[ "$CLARIFI_API_KEY_SECRET" =~ ^[a-zA-Z0-9_-]+:[a-zA-Z0-9_-]+$ ]]
  REMOTE_MODEL="$CLARIFI_API_MODEL"
elif [[ "$CLARIFI_MODE" == vertex ]]; then
  : "${CLARIFI_GEMINI_MODEL:?Select an available Vertex Gemini model ID}"
  REMOTE_MODEL="$CLARIFI_GEMINI_MODEL"
else
  echo "Cloud deployment supports gemini or vertex" >&2; exit 2
fi
[[ "$REMOTE_MODEL" =~ ^[a-zA-Z0-9._/-]+$ ]]
command -v gcloud >/dev/null || { echo "Install gcloud or run from Cloud Shell" >&2; exit 1; }
PYTHON="${PYTHON:-python3}"
# Bash 3.2 (macOS) treats empty arrays as unset under nounset.
SSH_FLAGS=("--quiet")
if [[ -n "${GCP_SSH_KEY_FILE:-}" ]]; then
  mkdir -p "$(dirname "$GCP_SSH_KEY_FILE")"
  SSH_FLAGS+=("--ssh-key-file=$GCP_SSH_KEY_FILE")
fi
VM="$PREFIX-vm"
VM_SA="$PREFIX-vm@$PROJECT_ID.iam.gserviceaccount.com"
API_SA="$PREFIX-api@$PROJECT_ID.iam.gserviceaccount.com"
WEB_SA="$PREFIX-web@$PROJECT_ID.iam.gserviceaccount.com"
BUILD_SERVICE_ACCOUNT="$PREFIX-build@$PROJECT_ID.iam.gserviceaccount.com"
BUILD_SOURCE_BUCKET="${BUILD_SOURCE_BUCKET:-$PROJECT_ID-$PREFIX-builds}"
export PROJECT_ID REGION TAG CLARIFI_MODE CLARIFI_API_MODEL CLARIFI_EVIDENCE_BUCKET
export CLARIFI_SERVICE_ACCOUNT="$API_SA" CLARIFI_SERVICE_NAME="$PREFIX-clarifi"
export REPOSITORY="$PREFIX"
export BUILD_SERVICE_ACCOUNT BUILD_SOURCE_BUCKET
echo "Creating $MACHINE_TYPE VM ($BOOT_DISK_GB GiB disk), private Cloud Run API, bucket and IAM resources in $PROJECT_ID."
echo "This script is for a NEW installation; it stops on existing resources and never deletes them."
gcloud services enable compute.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com storage.googleapis.com \
  iap.googleapis.com iam.googleapis.com oslogin.googleapis.com aiplatform.googleapis.com --project="$PROJECT_ID"
if [[ "$CLARIFI_MODE" == gemini ]]; then
  gcloud secrets versions describe "${CLARIFI_API_KEY_SECRET##*:}" \
    --secret="${CLARIFI_API_KEY_SECRET%:*}" --project="$PROJECT_ID" >/dev/null
fi
gcloud iam service-accounts create "$PREFIX-api" --project="$PROJECT_ID"
gcloud iam service-accounts create "$PREFIX-vm" --project="$PROJECT_ID"
gcloud iam service-accounts create "$PREFIX-web" --project="$PROJECT_ID"
gcloud iam service-accounts create "$PREFIX-build" --project="$PROJECT_ID"
gcloud storage buckets create "gs://$CLARIFI_EVIDENCE_BUCKET" --project="$PROJECT_ID" \
  --location="$REGION" --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets add-iam-policy-binding "gs://$CLARIFI_EVIDENCE_BUCKET" \
  --member="serviceAccount:$API_SA" --role=roles/storage.objectCreator
if [[ "$CLARIFI_MODE" == gemini ]]; then
  gcloud secrets add-iam-policy-binding "${CLARIFI_API_KEY_SECRET%:*}" --project="$PROJECT_ID" \
    --member="serviceAccount:$API_SA" --role=roles/secretmanager.secretAccessor
  export CLARIFI_API_KEY_SECRET
else
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$API_SA" --role=roles/aiplatform.user
  export CLARIFI_GEMINI_MODEL
fi
gcloud artifacts repositories create "$REPOSITORY" --project="$PROJECT_ID" \
  --location="$REGION" --repository-format=docker
gcloud artifacts repositories add-iam-policy-binding "$REPOSITORY" --project="$PROJECT_ID" \
  --location="$REGION" --member="serviceAccount:$BUILD_SERVICE_ACCOUNT" --role=roles/artifactregistry.writer
gcloud storage buckets create "gs://$BUILD_SOURCE_BUCKET" --project="$PROJECT_ID" \
  --location="$REGION" --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets add-iam-policy-binding "gs://$BUILD_SOURCE_BUCKET" \
  --member="serviceAccount:$BUILD_SERVICE_ACCOUNT" --role=roles/storage.objectViewer
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$BUILD_SERVICE_ACCOUNT" --role=roles/logging.logWriter
bash deploy/clarifi/deploy.sh
API_URL=$(gcloud run services describe "$CLARIFI_SERVICE_NAME" --project="$PROJECT_ID" \
  --region="$REGION" --format='value(status.url)')
[[ "$API_URL" =~ ^https://[a-z0-9-]+(\.[a-z0-9-]+)?\.run\.app$ ]]
gcloud run services add-iam-policy-binding "$CLARIFI_SERVICE_NAME" --project="$PROJECT_ID" \
  --region="$REGION" --member="serviceAccount:$VM_SA" --role=roles/run.invoker
gcloud compute networks create "$PREFIX-net" --project="$PROJECT_ID" --subnet-mode=custom
gcloud compute networks subnets create "$PREFIX-subnet" --project="$PROJECT_ID" \
  --network="$PREFIX-net" --region="$REGION" --range=10.42.0.0/24
# Only IAP SSH and tagged Cloud Run gateway traffic reach the VM.
gcloud compute firewall-rules create "$PREFIX-iap-ssh" --project="$PROJECT_ID" \
  --network="$PREFIX-net" --allow=tcp:22 --source-ranges=35.235.240.0/20 --target-tags="$PREFIX"
gcloud compute firewall-rules create "$PREFIX-web-internal" --project="$PROJECT_ID" \
  --network="$PREFIX-net" --allow=tcp:8080 --source-tags="$PREFIX-web" --target-tags="$PREFIX"
gcloud compute instances create "$VM" --project="$PROJECT_ID" --zone="$ZONE" \
  --machine-type="$MACHINE_TYPE" --subnet="$PREFIX-subnet" --tags="$PREFIX" \
  --image-family=debian-12 --image-project=debian-cloud --boot-disk-size="${BOOT_DISK_GB}GB" \
  --no-boot-disk-auto-delete --service-account="$VM_SA" --scopes=cloud-platform \
  --metadata=enable-oslogin=TRUE
VM_IP=$(gcloud compute instances describe "$VM" --project="$PROJECT_ID" --zone="$ZONE" \
  --format='value(networkInterfaces[0].networkIP)')
[[ "$VM_IP" =~ ^10\.42\.0\.[0-9]+$ ]]
gcloud builds submit . --project="$PROJECT_ID" --config=deploy/gcp/cloudbuild-gateway.yaml \
  --service-account="projects/$PROJECT_ID/serviceAccounts/$BUILD_SERVICE_ACCOUNT" \
  --gcs-source-staging-dir="gs://$BUILD_SOURCE_BUCKET/source" --region="$REGION" \
  --substitutions="_REGION=$REGION,_REPOSITORY=$REPOSITORY,_TAG=$TAG"
# Public HTTPS entrypoint; no secrets or application execution privileges.
gcloud run deploy "$PREFIX-web" --project="$PROJECT_ID" --region="$REGION" \
  --image="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/gateway:$TAG" \
  --service-account="$WEB_SA" --allow-unauthenticated --ingress=all --port=8080 \
  --network="$PREFIX-net" --subnet="$PREFIX-subnet" --network-tags="$PREFIX-web" \
  --vpc-egress=private-ranges-only --cpu=1 --memory=256Mi --concurrency=40 \
  --max-instances=2 --timeout=3600 \
  --set-env-vars="WORKBENCH_UPSTREAM=$VM_IP:8080"
PUBLIC_URL=$(gcloud run services describe "$PREFIX-web" --project="$PROJECT_ID" \
  --region="$REGION" --format='value(status.url)')
[[ "$PUBLIC_URL" =~ ^https://[a-z0-9-]+(\.[a-z0-9-]+)?\.run\.app$ ]]
PUBLIC_HOST="${PUBLIC_URL#https://}"
STAGING=$(mktemp -d)
"$PYTHON" deploy/gcp/package.py "$STAGING/workbench-source.tar.gz"
gcloud compute scp "$STAGING/workbench-source.tar.gz" deploy/gcp/bootstrap.sh \
  "$VM:~/" --project="$PROJECT_ID" --zone="$ZONE" --tunnel-through-iap "${SSH_FLAGS[@]}"
gcloud compute ssh "$VM" --project="$PROJECT_ID" --zone="$ZONE" --tunnel-through-iap \
  "${SSH_FLAGS[@]}" \
  --command="sudo env CLARIFI_API_URL='$API_URL' CLARIFI_REMOTE_MODEL='$REMOTE_MODEL' PUBLIC_HOST='$PUBLIC_HOST' OLLAMA_IMAGE='$OLLAMA_IMAGE' OLLAMA_MODEL='$OLLAMA_MODEL' CLARIFI_LOCAL_MODEL='$CLARIFI_LOCAL_MODEL' bash ./bootstrap.sh ./workbench-source.tar.gz"
echo "Checking anonymous HTTPS access through the public gateway..."
curl --fail --silent --show-error --retry 10 --retry-delay 5 --max-time 20 \
  --output /dev/null "$PUBLIC_URL/_stcore/health"
curl --fail --silent --show-error --retry 10 --retry-delay 5 --max-time 20 \
  --output /dev/null "$PUBLIC_URL/apps/demo/"
echo "Public judge URL: $PUBLIC_URL"
echo "Private admin access:"
echo "gcloud compute ssh $VM --project=$PROJECT_ID --zone=$ZONE --tunnel-through-iap -- -N -L 8511:127.0.0.1:8511"
echo "Source archive retained at $STAGING/workbench-source.tar.gz . See docs/GCP-DEPLOYMENT.md for acceptance checks and cleanup."
