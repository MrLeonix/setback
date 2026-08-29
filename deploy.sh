#!/usr/bin/env bash
#
# deploy.sh — idempotent Cloud Run deploy for Setback.
#
# Builds the single Docker image (see ./Dockerfile) via Cloud Build, pushes
# it to a keep-last-3 Artifact Registry repo, and reconciles two Cloud Run
# deployables from it per docs/ARCHITECTURE.md §1/§5:
#
#   setback-console   Cloud Run Service  — resident-facing FastAPI console.
#                      min-instances=0, request-based billing, public
#                      (no auth system exists yet — MVP cut list), runs as
#                      sa-console.
#   setback-tribunal  Cloud Run Job      — the ADK court-graph pipeline.
#                      1 vCPU / 2GiB, 1800s task timeout, runs as
#                      sa-orchestrator, invocable only by sa-console.
#
# Safe to re-run: every gcloud call below either targets an existing
# resource idempotently (`services enable`, `*-iam-policy-binding`,
# `set-cleanup-policies`) or uses `deploy`/`describe-or-create` semantics
# that converge rather than duplicate. Each run does build and push a new
# image (Cloud Build does no cross-run layer cache here), so re-running
# deploy.sh with no source changes still produces a new image tag/revision
# — this is intentional (a deploy script's job is to converge live state to
# the current source tree, not to skip work when it can't prove nothing
# changed) and costs only a Cloud Build minute, not a live model call.
#
# This script does not seed a fixture case or execute the job — that is a
# one-off verification action (creates a real Firestore case + spends a
# job-execution's worth of compute), run separately and reported alongside
# this script's output, not repeated on every deploy.
#
# Requires: gcloud, authenticated with ADC / a user with the necessary
# roles on `vexcourt-agent` (this script grants roles to the *service*
# accounts it deploys as — it does not grant anything to the operator
# running it). No API keys are read or embedded; the one existing secret
# (`maps-api-key`) is wired by reference only via `--set-secrets`.

set -euo pipefail

PROJECT_ID="${SETBACK_GCP_PROJECT:-vexcourt-agent}"
REGION="${SETBACK_REGION:-australia-southeast1}"
REPO="${SETBACK_AR_REPO:-setback}"
IMAGE_NAME="setback"
CONSOLE_SERVICE="setback-console"
TRIBUNAL_JOB="setback-tribunal"
CONSOLE_SA="sa-console@${PROJECT_ID}.iam.gserviceaccount.com"
TRIBUNAL_SA="sa-orchestrator@${PROJECT_ID}.iam.gserviceaccount.com"
MAPS_SECRET="maps-api-key"
FIRESTORE_DB="${SETBACK_FIRESTORE_DB:-setback-au}"
GCS_UPLOADS_BUCKET="${SETBACK_GCS_UPLOADS_BUCKET:-vexcourt-agent-setback-au}"

TAG="$(date -u +%Y%m%dt%H%M%Sz)"
AR_HOST="${REGION}-docker.pkg.dev"
IMAGE="${AR_HOST}/${PROJECT_ID}/${REPO}/${IMAGE_NAME}:${TAG}"

log() { printf '\n>> %s\n' "$*" >&2; }

log "Project: ${PROJECT_ID}  Region: ${REGION}  Image: ${IMAGE}"

# --- 1. Required APIs (idempotent: enabling an already-enabled API is a no-op) ---
log "Ensuring required APIs are enabled"
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com \
  --project="${PROJECT_ID}"

# --- 2. Artifact Registry repo, created once, cleanup policy re-applied every run ---
if ! gcloud artifacts repositories describe "${REPO}" \
      --project="${PROJECT_ID}" --location="${REGION}" >/dev/null 2>&1; then
  log "Creating Artifact Registry repo ${REPO}"
  gcloud artifacts repositories create "${REPO}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --repository-format=docker \
    --description="Setback container images (setback-console, setback-tribunal)"
else
  log "Artifact Registry repo ${REPO} already exists"
fi

CLEANUP_POLICY_FILE="$(mktemp -t setback-ar-cleanup.XXXXXX.json)"
trap 'rm -f "${CLEANUP_POLICY_FILE}"' EXIT
cat >"${CLEANUP_POLICY_FILE}" <<'JSON'
[
  {
    "name": "keep-last-3",
    "action": { "type": "Keep" },
    "mostRecentVersions": { "keepCount": 3 }
  }
]
JSON
log "Applying keep-last-3 cleanup policy"
gcloud artifacts repositories set-cleanup-policies "${REPO}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --policy="${CLEANUP_POLICY_FILE}" \
  --no-dry-run

# --- 3. Build and push via Cloud Build ---
log "Building and pushing ${IMAGE} via Cloud Build"
gcloud builds submit \
  --project="${PROJECT_ID}" \
  --tag="${IMAGE}" \
  "$(dirname "${BASH_SOURCE[0]}")"

# --- 4. Deploy setback-console (Cloud Run Service) ---
# `--clear-secrets`: console never reads the Maps secret (only the tribunal
# job's evidence/imagery.py Street View fallback does, per ARCHITECTURE.md
# §5) -- explicit every run so an unspecified `--set-secrets` on a redeploy
# can never silently inherit a stale secret reference from the prior
# revision's template.
log "Deploying Cloud Run service ${CONSOLE_SERVICE}"
gcloud run deploy "${CONSOLE_SERVICE}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --image="${IMAGE}" \
  --service-account="${CONSOLE_SA}" \
  --min-instances=0 \
  --max-instances=3 \
  --cpu-throttling \
  --port=8080 \
  --set-env-vars="SETBACK_GCP_PROJECT=${PROJECT_ID},SETBACK_FIRESTORE_DB=${FIRESTORE_DB},SETBACK_GCS_UPLOADS_BUCKET=${GCS_UPLOADS_BUCKET}" \
  --clear-secrets \
  --allow-unauthenticated \
  --quiet

# --- 5. Least-privilege IAM: sa-orchestrator may read exactly the maps secret.
# Granted before the job deploy below, since Cloud Run validates secret
# access for the target service account at revision-creation time.
log "Granting sa-orchestrator secretAccessor on ${MAPS_SECRET} only (resource-scoped)"
gcloud secrets add-iam-policy-binding "${MAPS_SECRET}" \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${TRIBUNAL_SA}" \
  --role="roles/secretmanager.secretAccessor"

# --- 6. Deploy setback-tribunal (Cloud Run Job) ---
# Only the tribunal job reads the Maps secret (evidence/imagery.py's Street
# View fallback runs inside the tribunal pipeline, never in the console) --
# per ARCHITECTURE.md §5 the console SA is not granted secretAccessor at all.
log "Deploying Cloud Run job ${TRIBUNAL_JOB}"
gcloud run jobs deploy "${TRIBUNAL_JOB}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --image="${IMAGE}" \
  --command="python" \
  --args="-m,setback.job.main" \
  --service-account="${TRIBUNAL_SA}" \
  --cpu=1 \
  --memory=2Gi \
  --task-timeout=1800s \
  --max-retries=1 \
  --set-env-vars="SETBACK_GCP_PROJECT=${PROJECT_ID},SETBACK_FIRESTORE_DB=${FIRESTORE_DB},SETBACK_GCS_UPLOADS_BUCKET=${GCS_UPLOADS_BUCKET}" \
  --set-secrets="MAPS_API_KEY=${MAPS_SECRET}:latest" \
  --quiet

# --- 7. Least-privilege IAM: sa-console may invoke exactly this job, nothing broader ---
log "Granting sa-console run.invoker on ${TRIBUNAL_JOB} only (resource-scoped)"
gcloud run jobs add-iam-policy-binding "${TRIBUNAL_JOB}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --member="serviceAccount:${CONSOLE_SA}" \
  --role="roles/run.invoker"

# --- 8. Report ---
SERVICE_URL="$(gcloud run services describe "${CONSOLE_SERVICE}" \
  --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')"
SERVICE_REVISION="$(gcloud run services describe "${CONSOLE_SERVICE}" \
  --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.latestReadyRevisionName)')"
JOB_REVISION="$(gcloud run jobs describe "${TRIBUNAL_JOB}" \
  --project="${PROJECT_ID}" --region="${REGION}" --format='value(metadata.generation)')"

log "Deploy complete."
echo "image:            ${IMAGE}"
echo "console URL:       ${SERVICE_URL}"
echo "console revision:  ${SERVICE_REVISION}"
echo "tribunal job:       ${TRIBUNAL_JOB} (generation ${JOB_REVISION})"
