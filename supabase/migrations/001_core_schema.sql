-- ============================================================
-- 001_core_schema.sql
-- Revenue OS — Core tables
-- Run: psql $DATABASE_URL -f 001_core_schema.sql
-- ============================================================

-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ────────────────────────────────────────────
-- TENANTS
-- ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tenants (
  id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  name         TEXT NOT NULL,
  slug         TEXT UNIQUE NOT NULL,
  plan         TEXT NOT NULL DEFAULT 'light',  -- light | pro | enterprise
  status       TEXT NOT NULL DEFAULT 'active', -- active | suspended | churned
  timezone     TEXT NOT NULL DEFAULT 'UTC',
  currency     TEXT NOT NULL DEFAULT 'USD',
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tenant_configs (
  id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id                UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  execution_mode           TEXT NOT NULL DEFAULT 'approval',  -- approval | auto
  max_emails_day           INT NOT NULL DEFAULT 50,
  runway_warning_months    NUMERIC NOT NULL DEFAULT 6,
  runway_critical_months   NUMERIC NOT NULL DEFAULT 3,
  safety_buffer_months     NUMERIC NOT NULL DEFAULT 2,
  crm_provider             TEXT DEFAULT '',  -- hubspot | pipedrive | zoho | salesforce
  bank_provider            TEXT DEFAULT 'manual',  -- plaid | stripe | manual
  icp_industries           TEXT[] DEFAULT ARRAY['saas','software','tech'],
  lead_hot_threshold       INT NOT NULL DEFAULT 80,
  lead_warm_threshold      INT NOT NULL DEFAULT 60,
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(tenant_id)
);

-- Encrypted secrets — values stored encrypted
CREATE TABLE IF NOT EXISTS tenant_secrets (
  id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  key         TEXT NOT NULL,   -- e.g. 'hubspot_api_key', 'plaid_access_token'
  value       TEXT NOT NULL,   -- encrypted value
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(tenant_id, key)
);

-- ────────────────────────────────────────────
-- USERS
-- ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tenant_users (
  id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  user_id     UUID NOT NULL,   -- Supabase auth.users.id
  role        TEXT NOT NULL DEFAULT 'founder',  -- founder | ops | viewer
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(tenant_id, user_id)
);

-- ────────────────────────────────────────────
-- REVENUE — Leads
-- ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS leads (
  id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  crm_id            TEXT,
  name              TEXT NOT NULL DEFAULT '',
  email             TEXT NOT NULL DEFAULT '',
  company           TEXT DEFAULT '',
  role              TEXT DEFAULT '',
  phone             TEXT DEFAULT '',
  industry          TEXT DEFAULT '',
  company_size      INT,
  notes             TEXT DEFAULT '',
  source            TEXT DEFAULT '',
  score             INT DEFAULT 0,
  routing           TEXT DEFAULT 'cold',  -- hot | warm | cold
  status            TEXT DEFAULT 'new',   -- new | contacted | replied | converted | lost
  enriched_data     JSONB DEFAULT '{}',
  last_activity_at  TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(tenant_id, crm_id),
  UNIQUE(tenant_id, email)
);

CREATE INDEX idx_leads_tenant_routing   ON leads(tenant_id, routing);
CREATE INDEX idx_leads_tenant_status    ON leads(tenant_id, status);
CREATE INDEX idx_leads_last_activity    ON leads(tenant_id, last_activity_at);

-- ────────────────────────────────────────────
-- REVENUE — Deals
-- ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS deals (
  id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  crm_id            TEXT,
  name              TEXT NOT NULL DEFAULT '',
  stage             TEXT DEFAULT '',
  amount            NUMERIC DEFAULT 0,
  probability       NUMERIC DEFAULT 0,
  close_date        DATE,
  owner_email       TEXT DEFAULT '',
  status            TEXT DEFAULT 'open',  -- open | won | lost
  last_activity_at  TIMESTAMPTZ,
  stage_entered_at  TIMESTAMPTZ,
  days_in_stage     INT GENERATED ALWAYS AS (
    EXTRACT(DAY FROM NOW() - COALESCE(stage_entered_at, created_at))::INT
  ) STORED,
  raw               JSONB DEFAULT '{}',
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(tenant_id, crm_id)
);

CREATE INDEX idx_deals_tenant_status    ON deals(tenant_id, status);
CREATE INDEX idx_deals_tenant_stage     ON deals(tenant_id, stage);
CREATE INDEX idx_deals_days_in_stage    ON deals(tenant_id, days_in_stage);

-- ────────────────────────────────────────────
-- REVENUE — Emails
-- ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS emails_sent (
  id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  lead_id     UUID REFERENCES leads(id),
  to_email    TEXT NOT NULL,
  subject     TEXT NOT NULL,
  body        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'draft', -- draft | sent | failed
  draft_id    TEXT UNIQUE,
  thread_id   TEXT,   -- Gmail thread ID for reply tracking
  sent_at     TIMESTAMPTZ,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_emails_tenant_status ON emails_sent(tenant_id, status);
CREATE INDEX idx_emails_sent_at       ON emails_sent(tenant_id, sent_at);

-- ────────────────────────────────────────────
-- TREASURY
-- ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS transactions (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  external_id   TEXT,   -- Plaid/Stripe transaction ID
  date          DATE NOT NULL,
  amount        NUMERIC NOT NULL,   -- negative = expense, positive = income
  description   TEXT DEFAULT '',
  category      TEXT DEFAULT 'other',
  source        TEXT DEFAULT 'manual',  -- plaid | stripe | manual
  raw           JSONB DEFAULT '{}',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(tenant_id, external_id)
);

CREATE INDEX idx_transactions_tenant_date     ON transactions(tenant_id, date);
CREATE INDEX idx_transactions_tenant_category ON transactions(tenant_id, category);

CREATE TABLE IF NOT EXISTS treasury_snapshots (
  id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  cash              NUMERIC NOT NULL DEFAULT 0,
  burn_rate         NUMERIC NOT NULL DEFAULT 0,
  projected_revenue NUMERIC NOT NULL DEFAULT 0,
  runway_months     NUMERIC NOT NULL DEFAULT 0,
  alert_level       TEXT NOT NULL DEFAULT 'HEALTHY',
  safe_budget       NUMERIC NOT NULL DEFAULT 0,
  currency          TEXT NOT NULL DEFAULT 'USD',
  data_freshness    TEXT DEFAULT 'live',
  warnings          TEXT[] DEFAULT ARRAY[]::TEXT[],
  calculated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_treasury_snapshots_tenant ON treasury_snapshots(tenant_id, calculated_at DESC);

-- ────────────────────────────────────────────
-- CORE OS — Recommendations & Approvals
-- ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS recommendations (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  agent           TEXT NOT NULL,       -- which workflow generated this
  rec_type        TEXT NOT NULL,       -- dormant_lead | stuck_deal | cash_alert | ...
  data            JSONB NOT NULL DEFAULT '{}',
  priority        TEXT NOT NULL DEFAULT 'MEDIUM',  -- CRITICAL | HIGH | MEDIUM | LOW
  status          TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected | expired | executed
  outcome_score   INT,     -- 0-100, set by feedback loop after 30 days
  outcome_note    TEXT,
  outcome_measured_at TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_recs_tenant_status   ON recommendations(tenant_id, status);
CREATE INDEX idx_recs_tenant_priority ON recommendations(tenant_id, priority);
CREATE INDEX idx_recs_created_at      ON recommendations(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS approvals (
  id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  recommendation_id UUID REFERENCES recommendations(id),
  action_type       TEXT NOT NULL,    -- send_email | update_crm | slack_notify | ...
  payload           JSONB NOT NULL DEFAULT '{}',
  token             TEXT UNIQUE NOT NULL DEFAULT gen_random_uuid()::TEXT,
  status            TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | expired
  expires_at        TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '48 hours',
  actioned_at       TIMESTAMPTZ,
  actioned_by       UUID,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_approvals_token      ON approvals(token);
CREATE INDEX idx_approvals_tenant     ON approvals(tenant_id, status);

-- ────────────────────────────────────────────
-- CORE OS — Events & Audit
-- ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS events (
  id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  event_type  TEXT NOT NULL,
  source      TEXT DEFAULT '',
  payload     JSONB DEFAULT '{}',
  processed   BOOLEAN DEFAULT FALSE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_events_tenant_processed ON events(tenant_id, processed, created_at);

CREATE TABLE IF NOT EXISTS audit_logs (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  actor         TEXT DEFAULT 'system',
  action        TEXT NOT NULL,
  resource_type TEXT,
  resource_id   TEXT,
  data          JSONB DEFAULT '{}',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_tenant ON audit_logs(tenant_id, created_at DESC);

-- ────────────────────────────────────────────
-- BRIEF SNAPSHOTS
-- ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS brief_snapshots (
  id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  brief_text  TEXT NOT NULL,
  raw_context JSONB DEFAULT '{}',
  week_start  DATE NOT NULL,
  delivered_at TIMESTAMPTZ,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
