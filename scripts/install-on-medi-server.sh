#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Seecript — install onto an existing chronic-medication-assistant server.
#
# Run this AS ROOT on the server, e.g.:
#   sudo bash scripts/install-on-medi-server.sh
#
# Confirmed infrastructure (see docs/INFRA.md, verified 2026-05-04):
#   - Server   : Aliyun Lightswitch (Hong Kong, AS45102)  → public IP 47.239.58.145
#   - OS       : Ubuntu 22.04 LTS (inferred from nginx/1.18.0)
#   - DNS      : aliyun (dns1/dns2.hichina.com) — main zone zlhu.asia
#   - Existing : nginx 1.18.0 + certbot already serving https://zlhu.asia (medi static site)
#   - Free port: 5001 internal (medi static site has no backend)
#
# What this script does:
#   1. Validates prerequisites
#   2. Creates the seecript system user + /opt/seecript directory
#   3. Clones (or pulls) the repo into /opt/seecript
#   4. Bootstraps server/venv and installs Python deps
#   5. Writes /opt/seecript/server/.env (interactive prompt for Keys)
#   6. Installs systemd unit `seecript-server` and starts it
#   7. Installs nginx site (HTTP only); reminds you to run certbot for HTTPS
#   8. Smoke-tests /api/health
#
# What you MUST do AFTER this script (2 manual steps):
#   a. Point DNS A record for $DOMAIN to this server's public IP (TTL 600s)
#   b. Run:  sudo certbot --nginx -d $DOMAIN
#   c. (Volcengine console) ensure the "录音文件识别 - 大模型极速版 (volc.bigasr.auc_turbo)"
#      resource is activated for your account — NOT the standard auc resource!
#      The Key in .env must be associated with the turbo resource.
#
# NOTE: PUBLIC_BASE_URL is **no longer required**. 极速版 accepts base64 inline so we
#       don't need to expose temp audio files publicly.
# ---------------------------------------------------------------------------
# ---- CRLF self-heal ----
# Files uploaded from Windows often have CRLF line endings, which break bash with
# `$'\r': command not found`. If we detect CR in our own source, strip them across the
# whole project and re-exec self.
if grep -q $'\r' "$0" 2>/dev/null; then
  echo "[bootstrap] CRLF detected, normalizing line endings to LF..."
  find "$(dirname "$(readlink -f "$0")")/.." -type f \( -name '*.sh' -o -name '*.py' -o -name '*.service' -o -name '*.conf*' -o -name '*.example' \) -print0 \
    | xargs -0 sed -i 's/\r$//' 2>/dev/null || true
  exec bash "$0" "$@"
fi

set -Eeuo pipefail

# Colors for readability when running interactively.
C_OK=$'\e[32m'; C_WARN=$'\e[33m'; C_ERR=$'\e[31m'; C_INFO=$'\e[36m'; C_RESET=$'\e[0m'
log()  { echo "${C_INFO}[$(date '+%H:%M:%S')]${C_RESET} $*"; }
ok()   { echo "${C_OK}[ OK ]${C_RESET} $*"; }
warn() { echo "${C_WARN}[WARN]${C_RESET} $*"; }
die()  { echo "${C_ERR}[FAIL]${C_RESET} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 0. Pre-flight
# ---------------------------------------------------------------------------
[[ $EUID -eq 0 ]] || die "Must be run as root (use: sudo bash $0)"

if ! command -v lsb_release >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y -qq lsb-release
fi
DISTRO_ID=$(lsb_release -is)
DISTRO_REL=$(lsb_release -rs)
[[ "$DISTRO_ID" == "Ubuntu" ]] || warn "Tested on Ubuntu only; you have $DISTRO_ID $DISTRO_REL — proceed with care."

# ---------------------------------------------------------------------------
# 1. Tunables (override via env)
# ---------------------------------------------------------------------------
PROJECT_DIR="${PROJECT_DIR:-/opt/seecript}"
RUN_USER="${RUN_USER:-seecript}"
BACKEND_PORT="${BACKEND_PORT:-5001}"
SERVICE_NAME="${SERVICE_NAME:-seecript-server}"
REPO_URL="${REPO_URL:-}"
BRANCH="${BRANCH:-main}"
DOMAIN="${DOMAIN:-}"

DEFAULT_DOMAIN="seecript.zlhu.asia"
if [[ -z "$DOMAIN" ]]; then
  read -rp "${C_INFO}Seecript 域名 [默认: ${DEFAULT_DOMAIN}]：${C_RESET} " DOMAIN
  DOMAIN="${DOMAIN:-$DEFAULT_DOMAIN}"
fi
[[ -n "$DOMAIN" ]] || die "DOMAIN cannot be empty"
[[ "$DOMAIN" =~ ^[a-zA-Z0-9.-]+$ ]] || die "DOMAIN '$DOMAIN' looks invalid"

if [[ -z "$REPO_URL" ]]; then
  read -rp "${C_INFO}Git 仓库 URL（用于 git clone；若已存在 $PROJECT_DIR 则可留空跳过）：${C_RESET} " REPO_URL || true
fi

# ---------------------------------------------------------------------------
# 2. Apt deps
# ---------------------------------------------------------------------------
log "Installing apt dependencies (idempotent)…"
apt-get update -qq
apt-get install -y -qq \
  git curl ca-certificates ufw \
  python3 python3-venv python3-pip \
  nginx \
  certbot python3-certbot-nginx
ok "apt deps installed"

# ---------------------------------------------------------------------------
# 3. User + directories
# ---------------------------------------------------------------------------
if id -u "$RUN_USER" >/dev/null 2>&1; then
  log "User '$RUN_USER' already exists, skipping creation"
else
  useradd -m -s /bin/bash "$RUN_USER"
  ok "Created user $RUN_USER"
fi

mkdir -p "$PROJECT_DIR"
chown -R "$RUN_USER:$RUN_USER" "$PROJECT_DIR"

# ---------------------------------------------------------------------------
# 4. Code
# ---------------------------------------------------------------------------
SENTINEL_FILE="$PROJECT_DIR/scripts/install-on-medi-server.sh"
if [[ -d "$PROJECT_DIR/.git" ]]; then
  log "Repo already exists (.git present), pulling latest…"
  sudo -u "$RUN_USER" -H bash -c "cd '$PROJECT_DIR' && git fetch --all --prune && git checkout '$BRANCH' && git pull --ff-only"
elif [[ -f "$SENTINEL_FILE" ]]; then
  # Code was uploaded via tar/scp/rsync (the "no-git" path). The fact that this very
  # script lives at $SENTINEL_FILE on disk is sufficient proof the project is present.
  log "Project files already present at $PROJECT_DIR (no .git, but core files OK) — skipping clone"
elif [[ -n "$REPO_URL" ]]; then
  log "Cloning $REPO_URL…"
  sudo -u "$RUN_USER" -H bash -c "git clone --branch '$BRANCH' '$REPO_URL' '$PROJECT_DIR/.tmp-clone' && shopt -s dotglob && mv '$PROJECT_DIR/.tmp-clone'/* '$PROJECT_DIR/' && rmdir '$PROJECT_DIR/.tmp-clone'"
  ok "Cloned"
else
  warn "No git repo URL provided, no .git found, and $SENTINEL_FILE missing."
  warn "Upload the project manually (e.g. via scripts/upload-to-server.ps1) and re-run this script."
  exit 1
fi

# ---------------------------------------------------------------------------
# 5. venv + pip
# ---------------------------------------------------------------------------
log "Bootstrapping python venv…"
sudo -u "$RUN_USER" -H bash <<EOF
set -e
cd "$PROJECT_DIR/server"
if [[ ! -x venv/bin/python ]]; then
  python3 -m venv venv
fi
venv/bin/pip install --upgrade pip --quiet
venv/bin/pip install -r requirements.txt --quiet
EOF
ok "venv ready, deps installed"

# ---------------------------------------------------------------------------
# 6. .env (interactive prompts for Keys; existing values kept)
# ---------------------------------------------------------------------------
ENV_FILE="$PROJECT_DIR/server/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$PROJECT_DIR/server/.env.example" "$ENV_FILE"
fi

prompt_secret() {
  local key="$1" prompt="$2" current
  current=$(grep -E "^${key}=" "$ENV_FILE" | head -n1 | sed -E "s/^${key}=//")
  if [[ -n "$current" ]]; then
    log "$key already set (length=${#current}); keeping. Edit $ENV_FILE manually to change."
    return
  fi
  echo
  read -rp "${C_INFO}${prompt}（留空跳过 → 走 mock）：${C_RESET} " val || true
  if [[ -n "$val" ]]; then
    sed -i -E "s|^${key}=.*$|${key}=${val}|" "$ENV_FILE"
  fi
}

# Force production-friendly defaults.
sed -i -E "s|^HOST=.*$|HOST=127.0.0.1|" "$ENV_FILE"
sed -i -E "s|^PORT=.*$|PORT=${BACKEND_PORT}|" "$ENV_FILE"
sed -i -E "s|^LLM_PROVIDER=.*$|LLM_PROVIDER=deepseek|" "$ENV_FILE"
sed -i -E "s|^ASR_PROVIDER=.*$|ASR_PROVIDER=doubao|" "$ENV_FILE"
sed -i -E "s|^CORS_ORIGINS=.*$|CORS_ORIGINS=https://${DOMAIN}|" "$ENV_FILE"
# Doubao 极速版资源 ID（必须，跟标准版不一样）
if grep -q "^DOUBAO_RESOURCE_ID=" "$ENV_FILE"; then
  sed -i -E "s|^DOUBAO_RESOURCE_ID=.*$|DOUBAO_RESOURCE_ID=volc.bigasr.auc_turbo|" "$ENV_FILE"
fi
# Drop legacy keys from older .env (PUBLIC_BASE_URL, ASR_POLL_*, DOUBAO_SUBMIT_URL, DOUBAO_QUERY_URL).
# 极速版完全不需要这些；留着只会让人困惑。
for legacy in PUBLIC_BASE_URL ASR_POLL_INTERVAL_SECONDS ASR_POLL_TIMEOUT_SECONDS DOUBAO_SUBMIT_URL DOUBAO_QUERY_URL; do
  sed -i -E "/^${legacy}=/d" "$ENV_FILE"
done

prompt_secret "DEEPSEEK_API_KEY" "DeepSeek API Key（sk-... 形式）"
prompt_secret "DOUBAO_API_KEY"   "火山豆包 API Key（控制台 → 语音技术 → 我的应用）"

chown "$RUN_USER:$RUN_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok ".env written and locked to mode 600"

# Create writable dirs for systemd's ReadWritePaths.
sudo -u "$RUN_USER" -H mkdir -p "$PROJECT_DIR/server/logs"

# ---------------------------------------------------------------------------
# 7. systemd
# ---------------------------------------------------------------------------
log "Installing systemd unit…"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}.service"
cp "$PROJECT_DIR/deploy/seecript-server.service" "$SERVICE_DST"
sed -i \
  -e "s|__PROJECT_DIR__|${PROJECT_DIR}|g" \
  -e "s|__RUN_USER__|${RUN_USER}|g" \
  "$SERVICE_DST"

# We use uvicorn directly in the unit file (no gunicorn) for simplicity. Override:
# -> But the default unit uses gunicorn; we keep that. Ensure gunicorn is installed.
sudo -u "$RUN_USER" -H "$PROJECT_DIR/server/venv/bin/pip" install gunicorn --quiet || true

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 2
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
  systemctl status "$SERVICE_NAME" --no-pager | tail -n 30
  die "systemd unit failed to start. Check journalctl -u $SERVICE_NAME -n 100"
fi
ok "$SERVICE_NAME is active"

# ---------------------------------------------------------------------------
# 7b. sudoers — allow $RUN_USER to restart its own service without a password
# ---------------------------------------------------------------------------
# Why: scripts/deploy.sh runs as $RUN_USER (seecript, NOT root) and ends with
#   `sudo systemctl restart seecript-server`. Without this rule deploy.sh
#   blocks waiting for a password that nobody types, and CI/cron deployments
#   silently hang. We scope the NOPASSWD whitelist to ONLY the three subcommands
#   the deploy script actually needs (restart / reload / status), and ONLY for
#   our own systemd unit — never a blanket sudo grant.
log "Installing sudoers rule for ${RUN_USER} (deploy.sh needs passwordless restart)…"
SUDOERS_FILE="/etc/sudoers.d/${RUN_USER}"
cat > "${SUDOERS_FILE}" <<EOF
# Managed by Seecript install-on-medi-server.sh — do not edit by hand.
# Allows the ${RUN_USER} user to restart/reload/status its own service so
# scripts/deploy.sh works without prompting for a password.
${RUN_USER} ALL=(root) NOPASSWD: /bin/systemctl restart ${SERVICE_NAME}, /bin/systemctl reload ${SERVICE_NAME}, /bin/systemctl status ${SERVICE_NAME}, /usr/bin/systemctl restart ${SERVICE_NAME}, /usr/bin/systemctl reload ${SERVICE_NAME}, /usr/bin/systemctl status ${SERVICE_NAME}
EOF
chmod 440 "${SUDOERS_FILE}"

# visudo --check returns non-zero on any syntax error → fail loudly rather than
# leaving a broken sudoers file behind.
if ! visudo -cf "${SUDOERS_FILE}" >/dev/null; then
  rm -f "${SUDOERS_FILE}"
  die "generated sudoers file failed visudo --check; removed to avoid breaking sudo."
fi

# Smoke-test: $RUN_USER must now be able to call sudo -n (non-interactive).
if ! sudo -u "${RUN_USER}" sudo -n systemctl status "${SERVICE_NAME}" >/dev/null 2>&1; then
  warn "${RUN_USER} still cannot run sudo -n systemctl — sudoers rule may not have taken effect."
else
  ok "${RUN_USER} can now restart ${SERVICE_NAME} without a password"
fi

# ---------------------------------------------------------------------------
# 8. nginx site (HTTP only; certbot will add 443 block)
# ---------------------------------------------------------------------------
log "Installing nginx site…"
NGINX_AVAIL="/etc/nginx/sites-available/seecript.conf"
NGINX_LINK="/etc/nginx/sites-enabled/seecript.conf"

# Backup any existing copy with date stamp (per project rule B).
if [[ -f "$NGINX_AVAIL" ]]; then
  cp "$NGINX_AVAIL" "${NGINX_AVAIL}.$(date +%F).bak"
  log "Backed up existing nginx config to ${NGINX_AVAIL}.$(date +%F).bak"
fi

cp "$PROJECT_DIR/deploy/nginx.conf.example" "$NGINX_AVAIL"
sed -i \
  -e "s|__DOMAIN__|${DOMAIN}|g" \
  -e "s|__FRONTEND_DIR__|${PROJECT_DIR}|g" \
  -e "s|__BACKEND_PORT__|${BACKEND_PORT}|g" \
  "$NGINX_AVAIL"

ln -sf "$NGINX_AVAIL" "$NGINX_LINK"
nginx -t || die "nginx -t failed; revert with: sudo rm $NGINX_LINK"
systemctl reload nginx
ok "nginx config installed"

# ---------------------------------------------------------------------------
# 9. Health check
# ---------------------------------------------------------------------------
log "Smoke-testing the local backend…"
sleep 1
HEALTH_BODY=$(curl -fsS "http://127.0.0.1:${BACKEND_PORT}/api/health" || true)
if [[ -z "$HEALTH_BODY" ]]; then
  die "Health check failed; journalctl -u $SERVICE_NAME -n 100"
fi
echo "$HEALTH_BODY"
ok "Backend healthy"

# ---------------------------------------------------------------------------
# 10. Final instructions
# ---------------------------------------------------------------------------
cat <<EOF

${C_OK}========================================================================${C_RESET}
${C_OK}  Seecript install scaffold complete${C_RESET}
${C_OK}========================================================================${C_RESET}

Status:
  • backend      : ${C_OK}running on 127.0.0.1:${BACKEND_PORT}${C_RESET}
  • nginx site   : ${C_OK}installed (HTTP)${C_RESET}
  • systemd unit : ${SERVICE_NAME}
  • config file  : ${ENV_FILE} (chmod 600, owner ${RUN_USER})

${C_WARN}2 manual steps remaining${C_RESET}:

  ${C_WARN}①${C_RESET} DNS — go to https://dns.console.aliyun.com  → 域名 zlhu.asia → 添加记录:
        Type=A   Host=${DOMAIN%%.*}   Value=47.239.58.145   TTL=600
       Verify:  dig +short ${DOMAIN}    (expected: 47.239.58.145)

  ${C_WARN}②${C_RESET} HTTPS — issue Let's Encrypt cert (browsers need it for ffmpeg.wasm SharedArrayBuffer):
       sudo certbot --nginx -d ${DOMAIN}
       (choose option 2 to redirect HTTP→HTTPS)

${C_INFO}Then verify everything end-to-end${C_RESET}:
       bash ${PROJECT_DIR}/scripts/health-check.sh https://${DOMAIN}

Volcengine console reminder (CRITICAL!):
  Make sure the resource ${C_INFO}录音文件识别 - 大模型极速版 (volc.bigasr.auc_turbo)${C_RESET}
  is activated at ${C_INFO}https://console.volcengine.com/speech/app${C_RESET}.
  极速版 ≠ 标准版！如果只开通了标准版，API 调用会返回 45000001 (参数无效).
EOF
