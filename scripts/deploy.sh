#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Seecript — production deploy / upgrade script
#
# Mirrors the workflow used by chronic-medication-assistant:
#   1. Snapshot the current code (commit hash) for rollback
#   2. git pull
#   3. pip install -r requirements.txt (inside venv)
#   4. systemctl restart seecript-server
#   5. Health check; on failure, roll back to the snapshot
#
# Run as the project user (e.g. `seecript`), NOT as root:
#   ssh seecript@server
#   cd /opt/seecript
#   bash scripts/deploy.sh
#
# If the server repo has diverged from origin (e.g. past `git am` / hotfix), plain
# `git pull --ff-only` will fail. Either merge/rebase locally and push, or on the
# server sync to remote explicitly:
#   GIT_UPDATE_MODE=reset-tracking bash scripts/deploy.sh
# This runs `git fetch` then `git reset --hard origin/<current-branch>` after
# snapshotting the previous commit for rollback.
# ---------------------------------------------------------------------------
set -Eeuo pipefail

# ---- Config (override via env if needed) ----
PROJECT_DIR="${PROJECT_DIR:-/opt/seecript}"
SERVICE_NAME="${SERVICE_NAME:-seecript-server}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:5001/api/health}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-30}"
LOG_FILE="${LOG_FILE:-${PROJECT_DIR}/var/logs/deploy.log}"
# ff-only (default) | reset-tracking — see header comments.
GIT_UPDATE_MODE="${GIT_UPDATE_MODE:-ff-only}"

# ---- Helpers ----
ts()  { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" | tee -a "${LOG_FILE}"; }
die() { log "ERROR: $*"; exit 1; }

# Ensure log dir exists.
mkdir -p "$(dirname "${LOG_FILE}")"

# ---- Pre-flight ----
log "===== Seecript deploy started ====="
cd "${PROJECT_DIR}" || die "PROJECT_DIR not found: ${PROJECT_DIR}"

if [[ "${EUID}" -eq 0 ]]; then
  die "do NOT run deploy.sh as root; switch to the project user (e.g. seecript)."
fi

if [[ ! -d server/venv ]]; then
  die "venv missing at server/venv — run initial deploy steps first (see DEPLOYMENT.md)."
fi

# ---- 1. Snapshot for rollback ----
PREV_COMMIT="$(git rev-parse HEAD)"
log "previous commit: ${PREV_COMMIT}"

# ---- 2. git update ----
log "git fetch + update (mode=${GIT_UPDATE_MODE})..."
git fetch --all --prune
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
case "${GIT_UPDATE_MODE}" in
  ff-only)
    git pull --ff-only
    ;;
  reset-tracking)
    git reset --hard "origin/${CURRENT_BRANCH}"
    ;;
  *)
    die "unknown GIT_UPDATE_MODE=${GIT_UPDATE_MODE} (use ff-only or reset-tracking)"
    ;;
esac

NEW_COMMIT="$(git rev-parse HEAD)"
if [[ "${PREV_COMMIT}" == "${NEW_COMMIT}" ]]; then
  log "no new commits — restarting service anyway to pick up any local edits"
fi
log "new commit: ${NEW_COMMIT}"

# ---- 3. Dependency install (inside venv) ----
log "pip install requirements..."
# shellcheck disable=SC1091
source server/venv/bin/activate
pip install --upgrade pip >> "${LOG_FILE}" 2>&1
pip install -r server/requirements.txt >> "${LOG_FILE}" 2>&1
deactivate

# ---- 4. Restart service ----
log "systemctl restart ${SERVICE_NAME}..."
sudo systemctl restart "${SERVICE_NAME}"

# ---- 5. Health check ----
log "waiting for health (${HEALTH_TIMEOUT_SECONDS}s timeout)..."
deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECONDS ))
healthy=false
while (( $(date +%s) < deadline )); do
  if curl -fsS --max-time 5 "${HEALTH_URL}" | grep -q '"status":"healthy"'; then
    healthy=true
    break
  fi
  sleep 2
done

if ! ${healthy}; then
  log "HEALTH CHECK FAILED — rolling back to ${PREV_COMMIT}"
  git reset --hard "${PREV_COMMIT}"
  source server/venv/bin/activate
  pip install -r server/requirements.txt >> "${LOG_FILE}" 2>&1
  deactivate
  sudo systemctl restart "${SERVICE_NAME}"
  die "rollback complete; check journalctl -u ${SERVICE_NAME} -n 200 for details."
fi

log "deploy SUCCESS — version $(curl -fsS "${HEALTH_URL}" || echo '?')"

# ---- 6. Provider sanity (warn-only; doesn't fail the deploy) ----
# Read .env without sourcing it (avoid polluting our shell with secrets).
ENV_FILE="${PROJECT_DIR}/server/.env"
if [[ -f "${ENV_FILE}" ]]; then
  if grep -qE '^LLM_PROVIDER=deepseek' "${ENV_FILE}"; then
    if ! grep -qE '^DEEPSEEK_API_KEY=sk-' "${ENV_FILE}"; then
      log "WARN: LLM_PROVIDER=deepseek but DEEPSEEK_API_KEY does not start with sk-"
    fi
  fi
  if grep -qE '^ASR_PROVIDER=doubao' "${ENV_FILE}"; then
    if ! grep -qE '^DOUBAO_API_KEY=[a-f0-9-]{30,}' "${ENV_FILE}"; then
      log "WARN: ASR_PROVIDER=doubao but DOUBAO_API_KEY missing/short"
    fi
    if ! grep -qE '^DOUBAO_RESOURCE_ID=volc\.bigasr\.auc_turbo' "${ENV_FILE}"; then
      log "WARN: ASR_PROVIDER=doubao but DOUBAO_RESOURCE_ID is not volc.bigasr.auc_turbo (极速版专用)"
    fi
  fi
fi

log "===== Seecript deploy finished ====="
