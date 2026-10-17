#!/usr/bin/env bash
# Run explicitly from repository root after reviewing docs/CLARIFI.md.
set -euo pipefail
: "${PROJECT_ID:?Set PROJECT_ID}"
: "${REGION:?Set REGION}"
: "${CLARIFI_GEMINI_MODEL:?Select an available Vertex Gemini model ID}"
: "${CLARIFI_EVIDENCE_BUCKET:?Set an existing private evidence bucket}"
: "${CLARIFI_SERVICE_ACCOUNT:?Set a dedicated runtime service-account email}"
: "${TAG:?Set an immutable build tag}"
REPOSITORY="${REPOSITORY:-clarifi}"
gcloud builds submit . --project="$PROJECT_ID" --config=deploy/clarifi/cloudbuild.yaml \
  --substitutions="_REGION=$REGION,_REPOSITORY=$REPOSITORY,_TAG=$TAG"
gcloud run deploy clarifi-financial-agents --project="$PROJECT_ID" --region="$REGION" \
  --image="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/clarifi:$TAG" \
  --service-account="$CLARIFI_SERVICE_ACCOUNT" --port=8080 --no-allow-unauthenticated \
  --cpu=2 --memory=2Gi --concurrency=2 --max-instances=2 --timeout=300 \
  --set-env-vars="CLARIFI_MODE=vertex,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,CLARIFI_GEMINI_MODEL=$CLARIFI_GEMINI_MODEL,CLARIFI_EVIDENCE_BUCKET=$CLARIFI_EVIDENCE_BUCKET"
