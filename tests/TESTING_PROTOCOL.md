# Revenue OS — Pre-Deployment Testing Protocol
# Failure Point Map → Error Handling → Fallback Logic → Production Checklist

---

## FAILURE POINT MAP

Every place the system can break, what happens, and what the fallback is.

---

### F1 — Supabase unreachable
**Where**: Every workflow, every Python service call  
**Symptom**: n8n nodes fail with connection error; Python service 500s  
**Impact**: TOTAL — nothing works without the DB  
**Error handling**: n8n error handler fires → Slack alert "Database unreachable"  
**Fallback**: None — Supabase is the single source of truth. Operators must restore connection.  
**Prevention**: Railway managed Postgres auto-restart; Supabase SLA 99.9%  

---

### F2 — Anthropic API unreachable or rate-limited
**Where**: Workflows 01, 02, 03, 04 (all Claude calls)  
**Symptom**: n8n LLM node times out or returns 429/500  
**Error handling**: n8n retries 2×; then routes to fallback  
**Fallback**: 
  - Email generation → send templated fallback email ("Hi {{name}}, I wanted to reconnect...") marked as `template_fallback` in DB
  - Deal analysis → insert recommendation with `analysis: "Manual review required — AI unavailable"`, flag with `needs_review: true`
  - Treasury explanation → send raw numbers without explanation ("Runway: 4.2 months | Cash: $45,000 | Burn: $10,700/mo")
  - Brief → deliver data table only, no synthesized bullets
**Client message**: "AI analysis temporarily unavailable. Raw data delivered. Will retry in 4 hours."  

---

### F3 — Python service unreachable
**Where**: All n8n HTTP calls to PYTHON_SERVICE_URL  
**Symptom**: HTTP timeout or 503  
**Error handling**: n8n code node checks `res.statusCode >= 400` and handles explicitly  
**Fallback**:
  - Treasury: use last known snapshot from Supabase, mark as `data_freshness: stale`
  - Lead scoring: fall back to raw DB score (set at creation), mark `score_method: cached`
  - Validation: skip validation, flag output as `validation_skipped: true`, still deliver
**Client message**: None — transparent fallback, just adds warning to data  

---

### F4 — CRM API returns 401 (invalid/expired token)
**Where**: Workflow 07 (CRM sync), Python CRM sync endpoint  
**Symptom**: `IntegrationError(retryable=False)`  
**Error handling**: Non-retryable — does not retry. Logs immediately.  
**Fallback**: Skip sync cycle. Recommendations continue using last-synced data from Supabase.  
**Client alert**: Slack "⚠️ CRM sync failed — token expired. Please reconnect your {{CRM_PROVIDER}} integration."  
**Note**: Data staleness grows. If CRM is down >3 days, dormant lead detection degrades.  

---

### F5 — CRM API rate limited (429)
**Where**: Same as F4  
**Symptom**: `IntegrationError(retryable=True)`  
**Error handling**: Retries 3× with exponential backoff (1m, 5m, 15m)  
**Fallback**: If all retries fail → skip this sync cycle, schedule retry at next cron  
**Client impact**: Max 24h data delay — negligible for this use case  

---

### F6 — Plaid / Bank API unavailable
**Where**: Workflow 03 treasury (Python service)  
**Symptom**: Connection error or Plaid error code in response body  
**Error handling**: Python catches IntegrationError, reads `retryable` flag  
**Fallback**: Use last known cash balance from `treasury_snapshots` table, mark `data_freshness: stale`  
**Client message**: "Treasury snapshot uses estimated balance (bank API temporarily unavailable)."  
**Risk**: If stale >7 days and cash situation changed dramatically → CRITICAL alert may not fire  

---

### F7 — LLM output validation fails
**Where**: After every Claude call (Python /validate/llm-output)  
**Symptom**: `valid: false` returned by validator  
**Error handling**: 
  - Email: do NOT send. Insert recommendation as `status: validation_failed`, notify on Slack with full draft for human review.
  - Treasury: do NOT send explanation. Deliver raw numbers only.
  - Deal analysis: insert with `needs_review: true` flag, no auto-action
  - Brief: deliver truncated brief with warning "Some sections could not be generated"
**Client message**: "AI output flagged for review. Raw data delivered. Please review the draft manually."  

---

### F8 — Approval token expired or already used
**Where**: Workflow 05 approval gate  
**Symptom**: Token not found or `status != pending`  
**Error handling**: Explicit DB check before any action. Returns 200 with HTML "Token expired" page.  
**Fallback**: Nothing executes. No error thrown.  
**User experience**: Founder sees a clear page explaining the link expired. They can re-approve from the dashboard.  

---

### F9 — Slack webhook unreachable
**Where**: Every workflow's final delivery node  
**Symptom**: HTTP error from Slack  
**Error handling**: `SlackMessaging.send()` returns `False` without raising  
**Fallback**: All outputs are already in Supabase before Slack delivery. Lovable dashboard still shows everything.  
**Client impact**: No Slack notification — but Lovable dashboard is unaffected.  

---

### F10 — Embedding generation fails (OpenAI)
**Where**: Workflow 06 feedback loop  
**Symptom**: OpenAI API error or unexpected shape  
**Error handling**: `parse_embedding` node returns `{ _embedding_failed: true }` and stops gracefully  
**Fallback**: Outcome score is saved to DB (useful for analytics). RAG pattern is simply not stored for this cycle.  
**Impact**: Minimal — one less example in memory. System degrades gradually, not catastrophically.  

---

### F11 — LLM hallucinates numbers in treasury explanation
**Where**: After Claude treasury explanation  
**Symptom**: Validator detects number >50% different from calculated snapshot  
**Error handling**: `valid: false`, insert as `validation_failed`  
**Fallback**: Deliver raw numbers without explanation  
**Why this matters**: A CFO telling a founder "you have 25 months of runway" when actual is 4.5 months is catastrophic.  

---

### F12 — Database RLS misconfiguration (tenant data leak)
**Where**: Any Supabase query  
**Symptom**: Query returns another tenant's data  
**Error handling**: RLS policies in 003_rls_policies.sql prevent this at DB level  
**Defense layers**:
  1. RLS policy: `tenant_id = get_user_tenant_id()`
  2. Application layer: every query includes `WHERE tenant_id = $TENANT_ID`
  3. Pinecone: namespace per tenant (`client_{tenant_id}`)
**Testing required**: Run `tests/test_tenancy.py` before each deploy  

---

### F13 — n8n instance crashes mid-execution
**Where**: Any running workflow  
**Symptom**: Partial execution — some DB writes happened, some didn't  
**Error handling**: n8n persists execution state. On restart, in-progress executions are marked failed.  
**Fallback**: Error handler fires → Slack alert. Next cron cycle re-runs detection/sync.  
**Idempotency**: All DB inserts use `ON CONFLICT DO NOTHING` or `UPSERT` — safe to re-run.  

---

## PRE-DEPLOYMENT TEST PROTOCOL

Run these before deploying to any client. Mark each ✅ before go-live.

### TIER 1 — Unit Tests (automated, run in CI)

```bash
cd python-services
pip install -r requirements.txt pytest
pytest tests/unit/ -v
```

Must pass 100%:
- [ ] test_treasury.py — all 10 treasury tests
- [ ] test_validators.py — all 15 validator tests

---

### TIER 2 — Integration Tests (requires .env.test with sandbox credentials)

```bash
pytest tests/integration/ -v
```

Must pass:
- [ ] CRM factory returns correct connector class
- [ ] HubSpot mocked 200 returns normalized NormalizedLead
- [ ] HubSpot mocked 401 raises non-retryable IntegrationError
- [ ] HubSpot mocked 429 retries exactly MAX_RETRIES times
- [ ] Plaid mocked success returns correct total_cash
- [ ] Plaid INVALID_ACCESS_TOKEN is non-retryable
- [ ] ManualBank returns zero balance without error
- [ ] Slack unconfigured returns False (never raises)
- [ ] Slack 500 returns False (never raises)

---

### TIER 3 — Workflow Smoke Tests (manual, in n8n staging)

Before activating workflows on a new tenant:

**T3.1 — Treasury Snapshot**
1. Trigger workflow 03 manually with test tenant
2. Verify: snapshot inserted in `treasury_snapshots`
3. Verify: `alert_level` is HEALTHY/WARNING/CRITICAL (never null)
4. Verify: `data_freshness` field is set
5. If HEALTHY: confirm no Slack message sent
6. Simulate CRITICAL: set `runway_critical_months` to 999 in config → confirm Slack fires
7. Simulate Python service down: set PYTHON_SERVICE_URL to invalid → confirm fallback uses last snapshot

**T3.2 — Lead Detection**
1. Insert a test lead in `leads` table with `last_activity_at = NOW() - 40 days, status = 'new'`
2. Trigger workflow 01 manually
3. Verify: lead appears in output of "Merge & Deduplicate Leads" node
4. Verify: score is computed (not null)
5. Verify: recommendation inserted in `recommendations` table
6. Verify: if score >= 60, Claude email draft exists in recommendation data
7. Verify: email validation result is logged (valid or invalid)
8. Verify: Slack message sent with draft

**T3.3 — Pipeline Detection**
1. Insert a test deal with `days_in_stage = 15, status = 'open'`
2. Trigger workflow 02 manually
3. Verify: deal detected as stuck
4. Verify: Claude unblock recommendation generated and validated
5. Verify: recommendation inserted in `recommendations`
6. Verify: Slack pipeline alert contains deal name

**T3.4 — Approval Gate**
1. Insert a test approval in `approvals` table with known token
2. GET `/webhook/revos/approve?token=THAT_TOKEN`
3. Verify: approval row updated to `status = approved`
4. Verify: audit log entry created
5. Verify: HTML response shows "Approved" page
6. GET same token again
7. Verify: HTML shows "Invalid or Expired" — second use blocked

**T3.5 — Expired Token**
1. Insert approval with `expires_at = NOW() - 1 hour`
2. GET approve URL
3. Verify: HTML shows "Invalid or Expired" — no DB update

**T3.6 — Error Handler**
1. Introduce intentional error in any workflow (bad SQL query)
2. Trigger the workflow
3. Verify: error handler fires
4. Verify: Slack error alert received
5. Verify: error logged in `audit_logs` with `action = workflow_error`

---

### TIER 4 — Edge Case / Adversarial Tests

Run these manually once before first client onboarding:

**T4.1 — Empty CRM**
- New client with 0 leads and 0 deals in CRM
- Expected: workflows run without error, log "No leads/deals found", no crash

**T4.2 — Negative bank balance**
- Manually set treasury snapshot with `cash = -5000`
- Expected: warning added, runway calculated, CRITICAL alert fired, no crash

**T4.3 — LLM returns empty response**
- Temporarily set max_tokens to 1 → Claude returns truncated/empty
- Expected: validator catches it, email not sent, Slack notified with raw data

**T4.4 — Unicode / special characters in lead data**
- Insert lead with: `name = "Björn Müller"`, `company = "Société Générale"`, `notes = "Budget: 15.000€"`
- Expected: no encoding errors, scoring works, email generated

**T4.5 — Very large deal amount**
- Insert deal with `amount = 9999999`
- Expected: no float overflow, priority correctly set to HIGH, Slack message formats correctly

**T4.6 — Tenant isolation**
- Create 2 test tenants: tenant_A and tenant_B
- Insert lead for tenant_A
- Run detection workflow with TENANT_ID = tenant_B
- Expected: tenant_B workflow finds 0 leads — tenant_A data not visible

**T4.7 — Approval from wrong tenant**
- Create approval for tenant_A
- Attempt approval with tenant_B credentials (via Lovable dashboard)
- Expected: RLS policy blocks read — 404 or empty result

---

### TIER 5 — Load / Throughput Checks

Before activating for a client with >500 leads:

```python
# Run this to estimate processing time
leads_to_process = 500
claude_calls_per_lead = 1
claude_latency_seconds = 3
total_minutes = (leads_to_process * claude_calls_per_lead * claude_latency_seconds) / 60
print(f"Estimated processing time: {total_minutes:.0f} minutes")
# At 500 leads: ~25 minutes — acceptable for nightly cron
# At 5000 leads: split into batches of 50, run 10 parallel cycles
```

Thresholds:
- < 100 leads: single run, no batching needed
- 100-500 leads: limit workflow to top 50 by score per run
- > 500 leads: implement batching in detect node (add OFFSET/LIMIT cycling)

---

## PRODUCTION READINESS CHECKLIST

Before activating any workflow on a live client:

**Infrastructure**
- [ ] Supabase project created, all 3 migrations applied
- [ ] RLS policies verified (test_tenancy passed)
- [ ] Railway services deployed (n8n + Python)
- [ ] All environment variables set in Railway dashboard
- [ ] WEBHOOK_URL matches Railway public URL exactly
- [ ] SERVICE_SECRET_KEY is set and matches between n8n and Python

**Credentials**
- [ ] ANTHROPIC_API_KEY valid and has API access
- [ ] OPENAI_API_KEY valid and has embeddings access
- [ ] Slack webhook URL tested (manual POST returns 200)
- [ ] CRM API key tested via `/health/integrations/{tenant_id}`
- [ ] Bank API in sandbox mode (never go live without testing sandbox first)

**Workflows**
- [ ] Error handler (00) imported and ACTIVE first
- [ ] All 7 workflows imported in order
- [ ] Credentials mapped in each workflow node
- [ ] All workflows activated (green toggle)
- [ ] EXECUTION_MODE=approval confirmed (never start with auto)

**First Run**
- [ ] Manually trigger treasury snapshot — verify output in Supabase
- [ ] Manually trigger lead detection — verify recommendation created
- [ ] Confirm Slack delivers test message
- [ ] Confirm Lovable dashboard shows recommendations
- [ ] Set a calendar reminder to check at 24h and 72h

**Client Handoff**
- [ ] Client has Slack workspace with webhook configured
- [ ] Client understands approval flow (show demo)
- [ ] Client knows dashboard URL
- [ ] Alert escalation path documented (what happens at 3am on CRITICAL)
