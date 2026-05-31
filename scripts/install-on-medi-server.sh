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
#   2. Installs Node 20 (NodeSource) — Ubuntu 22.04 ships Node 12 which is too old for Vite 8
#   3. Creates the seecript system user + /opt/seecript directory
#   4. Clones (or pulls) the repo into /opt/seecript
#   5. Bootstraps server/venv and installs Python deps
#   6. Builds the React frontend (npm ci + npm run build → web/dist)
#   7. Writes /opt/seecript/server/.env (interactive prompt for Keys)
#   8. Installs systemd unit `seecript-server` and starts it
#   9. Installs nginx site (HTTP only); reminds you to run certbot for HTTPS
#  10. Smoke-tests /api/health
#
# What you MUST do AFTER this script (2 manual steps):
#   a. Point DNS A record for $DOMAIN to this server's public IP (TTL 600s)
#   b. Run:  sudo certbot --nginx -d $DOMAIN
#
# 火山产品提醒：
#   • LLM / T2V — ARK (https://console.volcengine.com/ark) 控制台一份 Key 走 doubao_ark provider；
#     Seedance T2V 走独立计费可填 ARK_T2V_API_KEY，留空则复用 ARK_API_KEY。
#   • ASR        — 录音文件识别 2.0（标准资源 ID = volc.bigasr.auc），异步 submit+query；
#     必须配 PUBLIC_AUDIO_BASE_URL=https://${DOMAIN}，火山服务端会回拉 /uploads /samples 公网。
#     旧的极速版 (volc.bigasr.auc_turbo) 已弃用，本脚本默认按 2.0 标准版配置。
#   • TTS        — 火山语音合成（VOLC_TTS_APP_ID + VOLC_TTS_ACCESS_TOKEN）。
#     首次部署留 mock，等 TTS 应用申请到再手动编辑 .env 切到 volc。
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
  git curl ca-certificates ufw ffmpeg \
  python3 python3-venv python3-pip \
  nginx \
  certbot python3-certbot-nginx
ok "apt deps installed"

# ---------------------------------------------------------------------------
# 2b. Node.js 20 (NodeSource) — Vite 8 / React 18 需要 Node >= 18
# ---------------------------------------------------------------------------
# Ubuntu 22.04 的 apt nodejs 是 12.x，连 Vite 7 都跑不起来。直接装 NodeSource setup_20.x。
# 重复装是幂等的：脚本会检查现有 node 版本，> 18 跳过。
if command -v node >/dev/null 2>&1; then
  NODE_MAJOR=$(node -v | sed -E 's/^v([0-9]+).*/\1/')
else
  NODE_MAJOR=0
fi
if [[ "$NODE_MAJOR" -lt 18 ]]; then
  log "Installing Node.js 20 from NodeSource (current: v${NODE_MAJOR})…"
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null
  apt-get install -y -qq nodejs
  ok "Node.js $(node -v) installed"
else
  ok "Node.js $(node -v) already meets requirement"
fi

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
# gunicorn 没在 requirements.txt 里（dev 用 uvicorn 单进程），生产 systemd unit 强依赖。
venv/bin/pip install gunicorn --quiet
EOF
ok "venv ready, deps installed"

# ---------------------------------------------------------------------------
# 6. Frontend build (Vite → web/dist)
# ---------------------------------------------------------------------------
log "Building React frontend (npm ci + vite build)…"
sudo -u "$RUN_USER" -H bash <<EOF
set -e
cd "$PROJECT_DIR/web"
# npm ci 比 npm install 更确定性（严格按 lock 装），CI/部署首选。
npm ci --silent
npm run build
EOF
# nginx 以 www-data 运行，需要能读 web/dist。设为 seecript:www-data + 755 即可。
if getent group www-data >/dev/null 2>&1; then
  chown -R "$RUN_USER:www-data" "$PROJECT_DIR/web/dist"
  chmod -R g+rX "$PROJECT_DIR/web/dist"
fi
ok "Frontend built at $PROJECT_DIR/web/dist"

# ---------------------------------------------------------------------------
# 7. .env (interactive prompts for Keys; existing values kept)
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
    # 用 `|` 当 sed 分隔避免 Key 里有 `/` 的歧义；同时 \\& 转义保留 & 字面量。
    local esc
    esc=$(printf '%s' "$val" | sed -e 's/[\\&|]/\\&/g')
    sed -i -E "s|^${key}=.*$|${key}=${esc}|" "$ENV_FILE"
  fi
}

# ---- 强制生产侧默认值 ----
sed -i -E "s|^HOST=.*$|HOST=127.0.0.1|" "$ENV_FILE"
sed -i -E "s|^PORT=.*$|PORT=${BACKEND_PORT}|" "$ENV_FILE"
sed -i -E "s|^CORS_ORIGINS=.*$|CORS_ORIGINS=https://${DOMAIN}|" "$ENV_FILE"

# Provider：LLM/T2V 都走 doubao_ark；ASR 走 doubao 2.0；TTS 暂留 mock（待 VOLC_TTS_* 申请后再切）。
sed -i -E "s|^LLM_PROVIDER=.*$|LLM_PROVIDER=doubao_ark|" "$ENV_FILE"
sed -i -E "s|^T2V_PROVIDER=.*$|T2V_PROVIDER=doubao_ark|" "$ENV_FILE"
sed -i -E "s|^ASR_PROVIDER=.*$|ASR_PROVIDER=doubao|" "$ENV_FILE"

# ASR 2.0 标准资源 ID（旧极速版 volc.bigasr.auc_turbo 已弃用，2.0 必须用 volc.bigasr.auc）。
if grep -q "^DOUBAO_RESOURCE_ID=" "$ENV_FILE"; then
  sed -i -E "s|^DOUBAO_RESOURCE_ID=.*$|DOUBAO_RESOURCE_ID=volc.bigasr.auc|" "$ENV_FILE"
fi

# ASR 2.0 必填：火山服务端回拉音频的公网 base URL（不带尾斜线）。
# nginx 已经反代 /uploads + /samples 给 FastAPI，HTTPS 由 certbot 接管。
if grep -q "^PUBLIC_AUDIO_BASE_URL=" "$ENV_FILE"; then
  sed -i -E "s|^PUBLIC_AUDIO_BASE_URL=.*$|PUBLIC_AUDIO_BASE_URL=https://${DOMAIN}|" "$ENV_FILE"
else
  printf '\nPUBLIC_AUDIO_BASE_URL=https://%s\n' "${DOMAIN}" >> "$ENV_FILE"
fi

# 清掉真正废弃的字段：PUBLIC_BASE_URL（v0.1 命名）。
# 保留 DOUBAO_SUBMIT_URL / DOUBAO_QUERY_URL / ASR_POLL_* —— 2.0 异步流程要用。
sed -i -E '/^PUBLIC_BASE_URL=/d' "$ENV_FILE"

# ---- 交互式收 Key ----
# Volcengine 4 个产品 Key 互相独立，按需收：
#   ARK_API_KEY      → LLM (doubao-seed-2-0-lite) + 缺省 T2V Key
#   ARK_T2V_API_KEY  → 仅 Seedance 走独立计费时填；为空则 T2V client 回落 ARK_API_KEY
#   DOUBAO_API_KEY   → 录音文件识别 2.0（与 ARK 是不同的应用）
#   VOLC_TTS_*       → 语音合成（脚本不在这里收，留待用户申请后手动 vi .env 写入）
prompt_secret "ARK_API_KEY"     "火山方舟 ARK API Key（LLM + T2V 默认共用，console.volcengine.com/ark）"
prompt_secret "ARK_T2V_API_KEY" "Seedance T2V 独立计费 Key（无独立 Key 留空，自动复用上面那把）"
prompt_secret "DOUBAO_API_KEY"  "豆包 ASR Key（控制台 → 语音技术 → 录音文件识别 2.0）"

chown "$RUN_USER:$RUN_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok ".env written and locked to mode 600"

# 创建 systemd ReadWritePaths 列出的所有写入目录（FastAPI 自己也会按需 mkdir，
# 提前建好避免首次启动 race）。
sudo -u "$RUN_USER" -H mkdir -p \
  "$PROJECT_DIR/server/logs" \
  "$PROJECT_DIR/server/var/uploads" \
  "$PROJECT_DIR/server/var/outputs" \
  "$PROJECT_DIR/server/var/assets" \
  "$PROJECT_DIR/server/var/voiceovers" \
  "$PROJECT_DIR/server/var/projects" \
  "$PROJECT_DIR/server/var/aigc_cache"

# ---------------------------------------------------------------------------
# 8. systemd
# ---------------------------------------------------------------------------
log "Installing systemd unit…"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}.service"
cp "$PROJECT_DIR/deploy/seecript-server.service" "$SERVICE_DST"
sed -i \
  -e "s|__PROJECT_DIR__|${PROJECT_DIR}|g" \
  -e "s|__RUN_USER__|${RUN_USER}|g" \
  "$SERVICE_DST"

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
# 8b. sudoers — allow $RUN_USER to restart its own service without a password
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
# 9. nginx site (HTTP only; certbot will add 443 block)
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
# 10. Health check
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
# 11. Final instructions
# ---------------------------------------------------------------------------
cat <<EOF

${C_OK}========================================================================${C_RESET}
${C_OK}  Seecript install scaffold complete${C_RESET}
${C_OK}========================================================================${C_RESET}

Status:
  • backend      : ${C_OK}running on 127.0.0.1:${BACKEND_PORT}${C_RESET}
  • frontend     : ${C_OK}built at ${PROJECT_DIR}/web/dist${C_RESET}
  • nginx site   : ${C_OK}installed (HTTP)${C_RESET}
  • systemd unit : ${SERVICE_NAME}
  • config file  : ${ENV_FILE} (chmod 600, owner ${RUN_USER})

${C_WARN}2 manual steps remaining${C_RESET}:

  ${C_WARN}①${C_RESET} DNS — go to https://dns.console.aliyun.com  → 域名 zlhu.asia → 添加记录:
        Type=A   Host=${DOMAIN%%.*}   Value=<your server public IP>   TTL=600
       Verify:  dig +short ${DOMAIN}

  ${C_WARN}②${C_RESET} HTTPS — issue Let's Encrypt cert (browsers need it for ffmpeg.wasm SharedArrayBuffer,
       AND 火山 ASR 2.0 强制要求音频 URL 是 https://)：
       sudo certbot --nginx -d ${DOMAIN}
       (choose option 2 to redirect HTTP→HTTPS)

${C_INFO}Then verify everything end-to-end${C_RESET}:
       bash ${PROJECT_DIR}/scripts/health-check.sh https://${DOMAIN}

${C_INFO}TTS（暂走 mock）启用步骤${C_RESET}：
  1. 控制台申请 https://console.volcengine.com/speech/service/8
  2. 拿到 App ID 与 Access Token
  3. sudo -u ${RUN_USER} vi ${ENV_FILE}：
       TTS_PROVIDER=volc
       VOLC_TTS_APP_ID=<app id>
       VOLC_TTS_ACCESS_TOKEN=<access token>
  4. sudo systemctl restart ${SERVICE_NAME}

${C_INFO}火山方舟资源开通核对（部署前确认！）${C_RESET}：
  • LLM           — ARK 控制台『模型推理 → 在线推理点』开通 doubao-seed-2-0-lite
  • T2V           — ARK 控制台『视频生成 → Seedance 2.0』开通 doubao-seedance-2-0-fast-260128
  • ASR           — 语音技术 → 录音文件识别 2.0（资源 ID = ${C_INFO}volc.bigasr.auc${C_RESET}，不是极速版！）
  • Domain        — 给录音文件识别 2.0 应用的【域名白名单】里加 ${DOMAIN}
                    （否则火山服务端拉 https://${DOMAIN}/uploads/... 会被拒）
EOF
