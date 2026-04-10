#!/usr/bin/env bash
# =============================================================
# ops/provision_tenant.sh
# Revenue OS — Tenant provisioning script
#
# Usage:
#   ./provision_tenant.sh \
#     --name "Acme Corp" \
#     --slug "acme" \
#     --email "founder@acme.com" \
#     --plan "light" \
#     --timezone "Africa/Douala" \
#     --currency "XAF"
#
# Prerequisites:
#   - Supabase CLI installed and logged in
#   - Docker + Docker Compose installed
#   - .env.ops file with SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, DOMAIN
# =============================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Load ops env
if [[ -f "$SCRIPT_DIR/.env.ops" ]]; then
  source "$SCRIPT_DIR/.env.ops"
fi

# ── Parse args ────────────────────────────────────────────
TENANT_NAME=""
TENANT_SLUG=""
FOUNDER_EMAIL=""
PLAN="light"
TIMEZONE="UTC"
CURRENCY="USD"
N8N_PORT=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --name)     TENANT_NAME="$2";    shift 2 ;;
    --slug)     TENANT_SLUG="$2";    shift 2 ;;
    --email)    FOUNDER_EMAIL="$2";  shift 2 ;;
    --plan)     PLAN="$2";           shift 2 ;;
    --timezone) TIMEZONE="$2";       shift 2 ;;
    --currency) CURRENCY="$2";       shift 2 ;;
    --port)     N8N_PORT="$2";       shift 2 ;;
    --dry-run)  DRY_RUN=true;        shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# ── Validate required args ────────────────────────────────
for var in TENANT_NAME TENANT_SLUG FOUNDER_EMAIL; do
  if [[ -z "${!var}" ]]; then
    echo "ERROR: --${var,,} is required"
    exit 1
  fi
done

for var in SUPABASE_URL SUPABASE_SERVICE_ROLE_KEY DOMAIN; do
  if [[ -z "${!var:-}" ]]; then
    echo "ERROR: $var must be set in .env.ops"
    exit 1
  fi
done

# ── Auto-assign port if not given ─────────────────────────
if [[ -z "$N8N_PORT" ]]; then
  # Find next available port starting from 5700
  N8N_PORT=5700
  while lsof -i ":$N8N_PORT" > /dev/null 2>&1; do
    N8N_PORT=$((N8N_PORT + 1))
  done
fi

N8N_URL="https://${TENANT_SLUG}.n8n.${DOMAIN}"
TENANT_DIR="$ROOT_DIR/tenants/$TENANT_SLUG"

echo ""
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║  Revenue OS — Tenant Provisioning                         ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""
echo "  Tenant:   $TENANT_NAME ($TENANT_SLUG)"
echo "  Email:    $FOUNDER_EMAIL"
echo "  Plan:     $PLAN"
echo "  Timezone: $TIMEZONE"
echo "  Currency: $CURRENCY"
echo "  n8n URL:  $N8N_URL"
echo "  Port:     $N8N_PORT"
echo ""

if [[ "$DRY_RUN" == "true" ]]; then
  echo "[DRY RUN] Would provision tenant. Exiting."
  exit 0
fi

# ── Step 1: Create tenant in Supabase ────────────────────

echo "→ [1/8] Creating tenant in Supabase..."

TENANT_ID=$(curl -s -X POST "$SUPABASE_URL/rest/v1/tenants" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Content-Type: application/json" \
  -H "Prefer: return=representation" \
  -d "{\"name\": \"$TENANT_NAME\", \"slug\": \"$TENANT_SLUG\", \"plan\": \"$PLAN\", \"status\": \"active\", \"timezone\": \"$TIMEZONE\", \"currency\": \"$CURRENCY\"}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['id'])" 2>/dev/null || echo "")

if [[ -z "$TENANT_ID" ]]; then
  echo "ERROR: Failed to create tenant. Check if slug '$TENANT_SLUG' is already taken."
  exit 1
fi

echo "   Tenant ID: $TENANT_ID"

# ── Step 2: Insert tenant config ─────────────────────────

echo "→ [2/8] Inserting default tenant config..."

curl -s -X POST "$SUPABASE_URL/rest/v1/tenant_configs" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"tenant_id\": \"$TENANT_ID\",
    \"execution_mode\": \"approval\",
    \"max_emails_day\": 50,
    \"runway_warning_months\": 6,
    \"runway_critical_months\": 3,
    \"safety_buffer_months\": 2,
    \"founder_email\": \"$FOUNDER_EMAIL\"
  }" > /dev/null

# ── Step 3: Generate license ──────────────────────────────

echo "→ [3/8] Generating license..."

LICENSE_KEY="revos_$(openssl rand -hex 16)"
LICENSE_HASH=$(echo -n "$LICENSE_KEY" | sha256sum | cut -d' ' -f1)
EXPIRES_AT=$(date -d '+1 year' --utc +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -v+1y -u +%Y-%m-%dT%H:%M:%SZ)

curl -s -X POST "$SUPABASE_URL/rest/v1/licenses" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"tenant_id\": \"$TENANT_ID\",
    \"key_hash\": \"$LICENSE_HASH\",
    \"plan\": \"$PLAN\",
    \"status\": \"active\",
    \"expires_at\": \"$EXPIRES_AT\",
    \"entitlements\": {\"max_users\": 3, \"max_emails_day\": 50, \"modules\": [\"revenue\",\"treasury\",\"executive\"]}
  }" > /dev/null

echo "   License key: $LICENSE_KEY (store securely)"

# ── Step 4: Generate n8n encryption key ──────────────────

echo "→ [4/8] Generating n8n encryption key..."
N8N_ENCRYPTION_KEY=$(openssl rand -hex 32)
N8N_ADMIN_PASSWORD=$(openssl rand -base64 16 | tr -d '=+/')

# ── Step 5: Create tenant directory + docker-compose ─────

echo "→ [5/8] Creating tenant directory..."
mkdir -p "$TENANT_DIR"

cat > "$TENANT_DIR/docker-compose.yml" << COMPOSE
version: '3.8'
services:
  n8n:
    image: n8nio/n8n:latest
    container_name: n8n_${TENANT_SLUG}
    restart: unless-stopped
    ports:
      - "${N8N_PORT}:5678"
    environment:
      - N8N_BASIC_AUTH_ACTIVE=true
      - N8N_BASIC_AUTH_USER=admin
      - N8N_BASIC_AUTH_PASSWORD=${N8N_ADMIN_PASSWORD}
      - N8N_ENCRYPTION_KEY=${N8N_ENCRYPTION_KEY}
      - N8N_HOST=0.0.0.0
      - N8N_PORT=5678
      - WEBHOOK_URL=${N8N_URL}
      - N8N_TIMEZONE=${TIMEZONE}
      - N8N_LOG_LEVEL=info
      - TENANT_ID=${TENANT_ID}
      - LICENSE_TOKEN=${LICENSE_KEY}
      - EXECUTION_MODE=approval
      - SUPABASE_URL=${SUPABASE_URL}
      - SUPABASE_SERVICE_ROLE_KEY=${SUPABASE_SERVICE_ROLE_KEY}
      - PYTHON_SERVICE_URL=${PYTHON_SERVICE_URL:-}
      - SERVICE_SECRET_KEY=${SERVICE_SECRET_KEY:-}
      - SLACK_WEBHOOK_URL=
      - ANTHROPIC_API_KEY=
      - OPENAI_API_KEY=
    volumes:
      - n8n_data_${TENANT_SLUG}:/home/node/.n8n
      - ${ROOT_DIR}/prompts:/data/prompts:ro
    networks:
      - revos_${TENANT_SLUG}

volumes:
  n8n_data_${TENANT_SLUG}:

networks:
  revos_${TENANT_SLUG}:
COMPOSE

# ── Step 6: Launch n8n container ─────────────────────────

echo "→ [6/8] Starting n8n container..."
cd "$TENANT_DIR"
docker-compose up -d

echo "   Waiting for n8n to be ready..."
for i in {1..30}; do
  if curl -s "http://localhost:$N8N_PORT/healthz" > /dev/null 2>&1; then
    echo "   n8n is ready."
    break
  fi
  sleep 2
done

# ── Step 7: Import workflows ──────────────────────────────

echo "→ [7/8] Importing n8n workflows..."

N8N_BASE="http://admin:${N8N_ADMIN_PASSWORD}@localhost:${N8N_PORT}"
WORKFLOW_DIR="$ROOT_DIR/n8n-workflows"

# Import in correct order
WORKFLOW_ORDER=(
  "core/00_error_handler.json"
  "revenue/01_detect_score_email.json"
  "revenue/02_pipeline_nba_unblock.json"
  "treasury/03_treasury_monitor.json"
  "executive/04_weekly_brief.json"
  "core/05_approval_gate.json"
  "core/06_feedback_rag.json"
  "core/07_crm_sync.json"
  "core/08_execution_poller.json"
  "core/09_license_heartbeat_retry.json"
  "supervisor.json"
)

for wf in "${WORKFLOW_ORDER[@]}"; do
  WF_PATH="$WORKFLOW_DIR/$wf"
  if [[ -f "$WF_PATH" ]]; then
    WF_NAME=$(python3 -c "import json; d=json.load(open('$WF_PATH')); print(d.get('name','?'))" 2>/dev/null || echo "$wf")
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
      -X POST "$N8N_BASE/api/v1/workflows/import" \
      -H "Content-Type: application/json" \
      -d @"$WF_PATH")
    echo "   Imported: $WF_NAME (HTTP $HTTP_CODE)"
  else
    echo "   SKIP (not found): $wf"
  fi
done

# ── Step 8: Register instance in Supabase ─────────────────

echo "→ [8/8] Registering instance in Supabase..."

curl -s -X POST "$SUPABASE_URL/rest/v1/tenant_instances" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"tenant_id\": \"$TENANT_ID\",
    \"n8n_base_url\": \"$N8N_URL\",
    \"n8n_status\": \"provisioning\"
  }" > /dev/null

# ── Summary ───────────────────────────────────────────────

SUMMARY_FILE="$TENANT_DIR/TENANT_SETUP.md"
cat > "$SUMMARY_FILE" << SUMMARY
# Tenant Setup — ${TENANT_NAME}

**Tenant ID:** \`${TENANT_ID}\`
**Slug:** \`${TENANT_SLUG}\`
**n8n URL:** ${N8N_URL}
**n8n Admin:** admin / ${N8N_ADMIN_PASSWORD}
**License Key:** ${LICENSE_KEY}
**Provisioned:** $(date --utc +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)

## Next Steps for Client

1. Open ${N8N_URL} and log in
2. Run the "Onboarding Setup" workflow to connect integrations
3. Verify first scan runs within 10 minutes
4. Check Lovable dashboard at your domain

## Credentials to Store Securely
- License key: ${LICENSE_KEY}
- n8n admin password: ${N8N_ADMIN_PASSWORD}
- n8n encryption key: ${N8N_ENCRYPTION_KEY}
SUMMARY

echo ""
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║  ✅ Tenant Provisioned Successfully                        ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""
echo "  Tenant ID:       $TENANT_ID"
echo "  n8n URL:         $N8N_URL (port $N8N_PORT locally)"
echo "  n8n Admin:       admin / $N8N_ADMIN_PASSWORD"
echo "  License Key:     $LICENSE_KEY"
echo "  Setup summary:   $SUMMARY_FILE"
echo ""
echo "  → Next: Configure client integrations via n8n onboarding workflow"
echo ""
