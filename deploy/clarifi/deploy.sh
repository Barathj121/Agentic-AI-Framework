#!/usr/bin/env bash
# Run explicitly from repository root after reviewing docs/CLARIFI.md.
set -euo pipefail
: "${PROJECT_ID:?Set PROJECT_ID}"
: "${REGION:?Set REGION}"
: "${CLARIFI_EVIDENCE_BUCKET:?Set an existing private evidence bucket}"
: "${CLARIFI_SERVICE_ACCOUNT:?Set a dedicated runtime service-account email}"
: "${TAG:?Set an immutable build tag}"
REPOSITORY="${REPOSITORY:-clarifi}"
CLARIFI_MODE="${CLARIFI_MODE:-vertex}"
CLARIFI_SERVICE_NAME="${CLARIFI_SERVICE_NAME:-clarifi-financial-agents}"
case "$CLARIFI_MODE" in
  vertex)
    : "${CLARIFI_GEMINI_MODEL:?Select an available Vertex Gemini model ID}"
    MODEL_ENV="CLARIFI_MODE=vertex,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,CLARIFI_GEMINI_MODEL=$CLARIFI_GEMINI_MODEL,CLARIFI_EVIDENCE_BUCKET=$CLARIFI_EVIDENCE_BUCKET"
    MODEL_FLAGS=("--set-env-vars=$MODEL_ENV")
    ;;
  gemini)
    : "${CLARIFI_API_KEY_SECRET:?Set the existing Secret Manager name:version reference, not an API key}"
    CLARIFI_API_MODEL="${CLARIFI_API_MODEL:-gemini-3.5-flash-lite}"
    MODEL_ENV="CLARIFI_MODE=gemini,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,CLARIFI_API_MODEL=$CLARIFI_API_MODEL,CLARIFI_EVIDENCE_BUCKET=$CLARIFI_EVIDENCE_BUCKET"
    MODEL_FLAGS=("--set-env-vars=$MODEL_ENV" "--set-secrets=GEMINI_API_KEY=$CLARIFI_API_KEY_SECRET")
    ;;
  *) echo "CLARIFI_MODE must be vertex or gemini for Cloud Run" >&2; exit 2 ;;
esac
BUILD_FLAGS=("--quiet")
if [[ -n "${BUILD_SERVICE_ACCOUNT:-}" ]]; then
  : "${BUILD_SOURCE_BUCKET:?Set the private build-source bucket}"
  BUILD_FLAGS+=("--service-account=projects/$PROJECT_ID/serviceAccounts/$BUILD_SERVICE_ACCOUNT"
               "--gcs-source-staging-dir=gs://$BUILD_SOURCE_BUCKET/source" "--region=$REGION")
fi
gcloud builds submit . --project="$PROJECT_ID" --config=deploy/clarifi/cloudbuild.yaml \
  "${BUILD_FLAGS[@]}" \
  --substitutions="_REGION=$REGION,_REPOSITORY=$REPOSITORY,_TAG=$TAG"
gcloud run deploy "$CLARIFI_SERVICE_NAME" --project="$PROJECT_ID" --region="$REGION" \
  --image="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/clarifi:$TAG" \
  --service-account="$CLARIFI_SERVICE_ACCOUNT" --port=8080 --no-allow-unauthenticated \
  --cpu=2 --memory=2Gi --concurrency=2 --max-instances=2 --timeout=300 \
  "${MODEL_FLAGS[@]}"
