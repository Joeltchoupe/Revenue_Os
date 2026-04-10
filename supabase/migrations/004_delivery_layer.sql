-- ============================================================
-- 004_delivery_layer.sql
-- Revenue OS — Delivery layer, state machines, idempotency,
-- canonical objects, observability. Run after 003_rls_policies.sql
-- ============================================================

-- ────────────────────────────────────────────
-- 1. CANONICAL OBJECT: RECOMMENDATIONS (update)
-- Add delivery, idempotency, tracing fields
-- ────────────────────────────────────────────

ALTER TABLE recommendations
  ADD COLUMN IF NOT EXISTS correlation_id     TEXT,         -- traces signal → delivery → outcome
  ADD COLUMN IF NOT EXISTS idempotency_key    TEXT UNIQUE,  -- prevents duplicate inserts on retry
  ADD COLUMN IF NOT EXISTS delivery_status    TEXT NOT NULL DEFAULT 'pending',
  -- pending | delivered | failed | suppressed | snoozed
  ADD COLUMN IF NOT EXISTS last_delivered_at  TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS delivery_channel   TEXT,         -- slack | email | ui | none
  ADD COLUMN IF NOT EXISTS delivery_attempts  INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS snooze_until       TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS why_recommended    TEXT,         -- human-readable rationale (max 200 chars)
  ADD COLUMN IF NOT EXISTS estimated_impact   TEXT,         -- "saves ~$800/mo" or "+1.2 months runway"
  ADD COLUMN IF NOT EXISTS first_viewed_at    TIMESTAMPTZ,  -- when UI/Slack first opened it
  ADD COLUMN IF NOT EXISTS first_acted_at     TIMESTAMPTZ;  -- when first approval/rejection happened

CREATE INDEX IF NOT EXISTS idx_recs_idempotency    ON recommendations(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_recs_delivery_status ON recommendations(tenant_id, delivery_status);
CREATE INDEX IF NOT EXISTS idx_recs_snooze          ON recommendations(tenant_id, snooze_until)
  WHERE snooze_until IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_recs_correlation     ON recommendations(correlation_id);

-- ────────────────────────────────────────────
-- 2. CANONICAL OBJECT: APPROVALS (update)
-- Full state machine + idempotency + audit
-- ────────────────────────────────────────────

ALTER TABLE approvals
  ADD COLUMN IF NOT EXISTS correlation_id     TEXT,
  ADD COLUMN IF NOT EXISTS idempotency_key    TEXT UNIQUE, -- approval_id + action_type
  ADD COLUMN IF NOT EXISTS snooze_until       TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS execution_status   TEXT DEFAULT 'not_started',
  -- not_started | running | succeeded | failed | skipped
  ADD COLUMN IF NOT EXISTS execution_error    TEXT,
  ADD COLUMN IF NOT EXISTS execution_attempts INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS outcome            JSONB DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS channel            TEXT;         -- slack | email | ui

-- Constraint: approved approvals can't expire retroactively
CREATE INDEX IF NOT EXISTS idx_approvals_correlation ON approvals(correlation_id);
CREATE INDEX IF NOT EXISTS idx_approvals_exec_status ON approvals(tenant_id, execution_status)
  WHERE execution_status IN ('not_started', 'running', 'failed');

-- ────────────────────────────────────────────
-- 3. CANONICAL OBJECT: TREASURY SNAPSHOTS (update)
-- ────────────────────────────────────────────

ALTER TABLE treasury_snapshots
  ADD COLUMN IF NOT EXISTS correlation_id   TEXT,
  ADD COLUMN IF NOT EXISTS delivery_status  TEXT NOT NULL DEFAULT 'pending',
  ADD COLUMN IF NOT EXISTS delivered_at     TIMESTAMPTZ;

-- ────────────────────────────────────────────
-- 4. CANONICAL OBJECT: BRIEF SNAPSHOTS (update)
-- ────────────────────────────────────────────

ALTER TABLE brief_snapshots
  ADD COLUMN IF NOT EXISTS correlation_id      TEXT,
  ADD COLUMN IF NOT EXISTS delivery_status     TEXT NOT NULL DEFAULT 'pending',
  ADD COLUMN IF NOT EXISTS delivery_channel    TEXT,
  ADD COLUMN IF NOT EXISTS first_viewed_at     TIMESTAMPTZ;

-- ────────────────────────────────────────────
-- 5. DELIVERY LOG
-- All delivery attempts — channel, result, retry info
-- ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS delivery_log (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  correlation_id  TEXT NOT NULL,
  object_type     TEXT NOT NULL,   -- recommendation | approval | treasury | brief
  object_id       UUID,
  channel         TEXT NOT NULL,   -- slack | email | ui
  status          TEXT NOT NULL,   -- sent | failed | suppressed | fallback_used
  provider        TEXT,            -- resend | slack-webhook | supabase-realtime
  error_detail    TEXT,
  attempt_number  INT NOT NULL DEFAULT 1,
  sent_at         TIMESTAMPTZ DEFAULT NOW(),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_delivery_log_correlation ON delivery_log(correlation_id);
CREATE INDEX idx_delivery_log_tenant      ON delivery_log(tenant_id, created_at DESC);
CREATE INDEX idx_delivery_log_status      ON delivery_log(tenant_id, status, created_at DESC);

-- ────────────────────────────────────────────
-- 6. TENANT INSTANCES (multi-tenant n8n fleet)
-- Tracks the n8n instance for each tenant
-- ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tenant_instances (
  id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE UNIQUE,
  n8n_base_url      TEXT NOT NULL,
  n8n_status        TEXT NOT NULL DEFAULT 'provisioning',
  -- provisioning | active | suspended | error
  n8n_version       TEXT,
  last_heartbeat_at TIMESTAMPTZ,
  connector_health  JSONB DEFAULT '{}',  -- {"crm": "healthy", "bank": "degraded"}
  provisioned_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at        TIMESTAMPTZ DEFAULT NOW()
);

-- ────────────────────────────────────────────
-- 7. LICENSES
-- ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS licenses (
  id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  key_hash     TEXT NOT NULL UNIQUE,  -- SHA-256 of the license key
  plan         TEXT NOT NULL DEFAULT 'light',  -- light | pro | enterprise
  status       TEXT NOT NULL DEFAULT 'active', -- active | suspended | expired | trial
  expires_at   TIMESTAMPTZ,
  entitlements JSONB DEFAULT '{
    "max_users": 3,
    "max_emails_day": 50,
    "modules": ["revenue", "treasury", "executive"],
    "support_level": "async"
  }',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_licenses_tenant ON licenses(tenant_id);
CREATE INDEX idx_licenses_status ON licenses(status, expires_at);

-- ────────────────────────────────────────────
-- 8. TENANT CONFIGS — extended delivery fields
-- ────────────────────────────────────────────

ALTER TABLE tenant_configs
  ADD COLUMN IF NOT EXISTS slack_channel_alerts  TEXT DEFAULT '#revos-alerts',
  ADD COLUMN IF NOT EXISTS slack_channel_briefs  TEXT DEFAULT '#revos-weekly',
  ADD COLUMN IF NOT EXISTS email_digest_enabled  BOOLEAN DEFAULT true,
  ADD COLUMN IF NOT EXISTS quiet_hours_start     INT DEFAULT 22,  -- 22:00 local
  ADD COLUMN IF NOT EXISTS quiet_hours_end       INT DEFAULT 7,   -- 07:00 local
  ADD COLUMN IF NOT EXISTS delivery_fallback     TEXT DEFAULT 'email',
  -- what to do if primary channel fails: email | log_only | none
  ADD COLUMN IF NOT EXISTS max_alerts_per_day    INT DEFAULT 10,
  ADD COLUMN IF NOT EXISTS snooze_default_hours  INT DEFAULT 24;

-- ────────────────────────────────────────────
-- 9. PRODUCT OBSERVABILITY METRICS
-- One row per tenant per day — lightweight
-- ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS product_metrics (
  id                    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id             UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  metric_date           DATE NOT NULL DEFAULT CURRENT_DATE,
  -- Delivery
  signals_generated     INT NOT NULL DEFAULT 0,
  signals_delivered     INT NOT NULL DEFAULT 0,
  delivery_failures     INT NOT NULL DEFAULT 0,
  -- Approval
  approvals_sent        INT NOT NULL DEFAULT 0,
  approvals_actioned    INT NOT NULL DEFAULT 0,  -- approved + rejected + snoozed
  approvals_approved    INT NOT NULL DEFAULT 0,
  approvals_rejected    INT NOT NULL DEFAULT 0,
  -- Time-to-value (seconds)
  avg_time_to_delivery  NUMERIC,   -- signal created → first delivery
  avg_time_to_action    NUMERIC,   -- signal created → first approval action
  -- System health
  workflow_errors       INT NOT NULL DEFAULT 0,
  connector_failures    JSONB DEFAULT '{}',  -- {"crm": 2, "bank": 0}
  llm_calls             INT NOT NULL DEFAULT 0,
  -- first-ever timestamps (for cohort analysis)
  first_signal_at       TIMESTAMPTZ,
  first_action_at       TIMESTAMPTZ,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(tenant_id, metric_date)
);

CREATE INDEX idx_product_metrics_tenant_date ON product_metrics(tenant_id, metric_date DESC);

-- Helper: upsert today's metrics atomically
CREATE OR REPLACE FUNCTION increment_metric(
  p_tenant_id UUID,
  p_field     TEXT,
  p_delta     INT DEFAULT 1
) RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO product_metrics (tenant_id, metric_date)
  VALUES (p_tenant_id, CURRENT_DATE)
  ON CONFLICT (tenant_id, metric_date) DO NOTHING;

  EXECUTE format(
    'UPDATE product_metrics SET %I = %I + $1, updated_at = NOW() WHERE tenant_id = $2 AND metric_date = CURRENT_DATE',
    p_field, p_field
  ) USING p_delta, p_tenant_id;
END;
$$;

-- ────────────────────────────────────────────
-- 10. DAILY QUOTA COUNTERS (fast, lightweight)
-- ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS daily_quotas (
  tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  quota_date      DATE NOT NULL DEFAULT CURRENT_DATE,
  emails_sent     INT NOT NULL DEFAULT 0,
  alerts_sent     INT NOT NULL DEFAULT 0,
  approvals_open  INT NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, quota_date)
);

CREATE OR REPLACE FUNCTION check_and_increment_quota(
  p_tenant_id UUID,
  p_field     TEXT,
  p_limit     INT
) RETURNS BOOLEAN LANGUAGE plpgsql AS $$
DECLARE
  v_current INT;
BEGIN
  INSERT INTO daily_quotas (tenant_id, quota_date)
  VALUES (p_tenant_id, CURRENT_DATE)
  ON CONFLICT (tenant_id, quota_date) DO NOTHING;

  EXECUTE format('SELECT %I FROM daily_quotas WHERE tenant_id = $1 AND quota_date = CURRENT_DATE', p_field)
  INTO v_current USING p_tenant_id;

  IF v_current >= p_limit THEN
    RETURN FALSE;
  END IF;

  EXECUTE format(
    'UPDATE daily_quotas SET %I = %I + 1 WHERE tenant_id = $1 AND quota_date = CURRENT_DATE',
    p_field, p_field
  ) USING p_tenant_id;

  RETURN TRUE;
END;
$$;

-- ────────────────────────────────────────────
-- 11. CONNECTOR HEALTH TABLE
-- Written by n8n heartbeat workflow
-- ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS connector_health (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  connector     TEXT NOT NULL,   -- crm | bank | email | slack | llm_claude | llm_openai
  status        TEXT NOT NULL,   -- healthy | degraded | down | unconfigured
  latency_ms    INT,
  error_detail  TEXT,
  checked_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(tenant_id, connector)
);

-- RLS
ALTER TABLE delivery_log        ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_instances    ENABLE ROW LEVEL SECURITY;
ALTER TABLE licenses            ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_metrics     ENABLE ROW LEVEL SECURITY;
ALTER TABLE daily_quotas        ENABLE ROW LEVEL SECURITY;
ALTER TABLE connector_health    ENABLE ROW LEVEL SECURITY;

CREATE POLICY "tenant_isolation_delivery_log"
  ON delivery_log FOR ALL USING (tenant_id = get_user_tenant_id());
CREATE POLICY "tenant_isolation_product_metrics"
  ON product_metrics FOR ALL USING (tenant_id = get_user_tenant_id());
CREATE POLICY "tenant_isolation_daily_quotas"
  ON daily_quotas FOR ALL USING (tenant_id = get_user_tenant_id());
CREATE POLICY "tenant_isolation_connector_health"
  ON connector_health FOR ALL USING (tenant_id = get_user_tenant_id());
-- Licenses: read-only for users
CREATE POLICY "tenant_license_read"
  ON licenses FOR SELECT USING (tenant_id = get_user_tenant_id());
-- Instances: read-only for users
CREATE POLICY "tenant_instance_read"
  ON tenant_instances FOR SELECT USING (tenant_id = get_user_tenant_id());
