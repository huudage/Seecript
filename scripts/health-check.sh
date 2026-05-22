#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Seecript — production smoke test.
#
# Usage:
#   bash scripts/health-check.sh https://seecript.zlhu.asia
#   bash scripts/health-check.sh                 # defaults to http://127.0.0.1:5001
#
# Verifies:
#   ① /api/health returns 200 + healthy
#   ② 4 LLM endpoints return 200 (uses real DeepSeek if ENV says so — ¥0.005 cost)
#   ③ ASR endpoint round-trip with a tiny silent mp3 (uses real Doubao if configured —
#      will return 20000003 silent-audio error, which is treated as "API path is OK")
# ---------------------------------------------------------------------------
set -Eeuo pipefail

BASE_URL="${1:-http://127.0.0.1:5001}"
BASE_URL="${BASE_URL%/}"
SAMPLE_AUDIO="${2:-}"
# Optional second arg: path to a real mp3/m4a/wav file (≤ 25 MB, preferably 5-30s).
# If absent, the ASR step only verifies the endpoint is reachable (it submits an
# obviously-invalid byte sequence and accepts a 4xx as "API path OK").

C_OK=$'\e[32m'; C_FAIL=$'\e[31m'; C_INFO=$'\e[36m'; C_RESET=$'\e[0m'
ok()   { echo "${C_OK}[ OK ]${C_RESET} $*"; }
fail() { echo "${C_FAIL}[FAIL]${C_RESET} $*" >&2; FAILED=$((FAILED+1)); }
info() { echo "${C_INFO}[INFO]${C_RESET} $*"; }

FAILED=0

# ---- ① Health ----
info "GET ${BASE_URL}/api/health"
HEALTH=$(curl -fsS --max-time 10 "${BASE_URL}/api/health" || true)
if [[ -z "$HEALTH" ]]; then
  fail "health endpoint unreachable"
else
  echo "       $HEALTH"
  if echo "$HEALTH" | grep -q '"status":"healthy"'; then
    ok "health"
  else
    fail "health body unexpected"
  fi
fi

# ---- ② 4 LLM endpoints ----
test_llm() {
  local name="$1" path="$2" body="$3"
  local resp http_code
  resp=$(mktemp)
  http_code=$(curl -s -o "$resp" -w "%{http_code}" --max-time 90 \
    -X POST "${BASE_URL}${path}" \
    -H "Content-Type: application/json" \
    -d "$body" || echo "000")
  size=$(wc -c < "$resp")
  if [[ "$http_code" == "200" ]]; then
    ok "$name (http=${http_code}, ${size}B)"
  else
    fail "$name http=${http_code} body=$(head -c 200 "$resp")"
  fi
  rm -f "$resp"
}

info "Testing 4 LLM endpoints…"
test_llm "persona"  "/api/persona/generate"  '{"background":"PM 8y","interests":"home","resources":"6h/week"}'
test_llm "skeleton" "/api/skeleton/extract"  '{"transcript":"Hello world this is a test transcript content for skeleton extraction module."}'
test_llm "seo"      "/api/seo/titles"        '{"script":"Hello world this is a sample script for SEO testing.","platform":"douyin"}'
test_llm "comments" "/api/comments/classify" '{"raw_text":"line1 hi\nline2 spam"}'

# ---- ③ ASR ----
info "Testing /api/asr/transcribe…"
RESP=$(mktemp)
if [[ -n "$SAMPLE_AUDIO" && -f "$SAMPLE_AUDIO" ]]; then
  # Real round-trip with user-supplied audio.
  size_mb=$(du -m "$SAMPLE_AUDIO" | cut -f1)
  info "  using $SAMPLE_AUDIO (${size_mb} MB) — full round-trip, may take 30-180s"
  HTTP=$(curl -s -o "$RESP" -w "%{http_code}" --max-time 240 \
    -X POST "${BASE_URL}/api/asr/transcribe" \
    -F "file=@${SAMPLE_AUDIO}" || echo "000")
  if [[ "$HTTP" == "200" ]]; then
    PROVIDER=$(grep -oE '"provider":"[^"]+"' "$RESP" | head -1)
    ELAPSED=$(grep -oE '"elapsed_ms":[0-9]+' "$RESP" | head -1)
    PREVIEW=$(grep -oE '"transcript":"[^"]{0,80}' "$RESP" | head -1)
    ok "asr full round-trip (${PROVIDER}, ${ELAPSED})"
    info "  transcript preview: ${PREVIEW}…"
  else
    fail "asr http=${HTTP} body=$(head -c 300 "$RESP")"
  fi
else
  # Reachability-only check: submit a 1-byte buffer and accept any 4xx as "API path OK".
  info "  no sample audio provided — running endpoint-reachability test only"
  info "  (pass a real mp3 as 2nd arg to validate Doubao end-to-end)"
  HTTP=$(curl -s -o "$RESP" -w "%{http_code}" --max-time 30 \
    -X POST "${BASE_URL}/api/asr/transcribe" \
    -F 'file=@/dev/null;filename=probe.mp3;type=audio/mpeg' || echo "000")
  case "$HTTP" in
    200) ok "asr endpoint reachable (returned 200 — likely mock provider)" ;;
    400|413|415|422) ok "asr endpoint reachable (returned ${HTTP}, expected for empty file)" ;;
    502) ok "asr endpoint reachable (502 from upstream Doubao on empty audio — API path OK)" ;;
    *)   fail "asr endpoint unexpected http=${HTTP} body=$(head -c 200 "$RESP")" ;;
  esac
fi
rm -f "$RESP"

# ---- Verdict ----
echo
if [[ $FAILED -eq 0 ]]; then
  echo "${C_OK}========== ALL CHECKS PASSED ==========${C_RESET}"
  exit 0
else
  echo "${C_FAIL}========== ${FAILED} CHECK(S) FAILED ==========${C_RESET}"
  exit 1
fi
