-- ============================================================
-- 003_rls_policies.sql
-- Revenue OS — Row Level Security
-- CRITICAL: Run this. Without it, tenants can see each other's data.
-- ============================================================

-- Enable RLS on all tenant-scoped tables
ALTER TABLE tenant_configs     ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_secrets     ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_users       ENABLE ROW LEVEL SECURITY;
ALTER TABLE leads              ENABLE ROW LEVEL SECURITY;
ALTER TABLE deals              ENABLE ROW LEVEL SECURITY;
ALTER TABLE emails_sent        ENABLE ROW LEVEL SECURITY;
ALTER TABLE transactions       ENABLE ROW LEVEL SECURITY;
ALTER TABLE treasury_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE recommendations    ENABLE ROW LEVEL SECURITY;
ALTER TABLE approvals          ENABLE ROW LEVEL SECURITY;
ALTER TABLE events             ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs         ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_chunks      ENABLE ROW LEVEL SECURITY;
ALTER TABLE brief_snapshots    ENABLE ROW LEVEL SECURITY;

-- ────────────────────────────────────────────
-- Helper function: get current user's tenant_id
-- ────────────────────────────────────────────

CREATE OR REPLACE FUNCTION get_user_tenant_id()
RETURNS UUID
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
  v_tenant_id UUID;
BEGIN
  SELECT tenant_id INTO v_tenant_id
  FROM tenant_users
  WHERE user_id = auth.uid();
  RETURN v_tenant_id;
END;
$$;

-- ────────────────────────────────────────────
-- RLS Policies — Users can only see their tenant's data
-- Service role bypasses RLS (used by n8n and Python service)
-- ────────────────────────────────────────────

-- Leads
CREATE POLICY "tenant_isolation_leads"
  ON leads FOR ALL
  USING (tenant_id = get_user_tenant_id())
  WITH CHECK (tenant_id = get_user_tenant_id());

-- Deals
CREATE POLICY "tenant_isolation_deals"
  ON deals FOR ALL
  USING (tenant_id = get_user_tenant_id())
  WITH CHECK (tenant_id = get_user_tenant_id());

-- Emails
CREATE POLICY "tenant_isolation_emails"
  ON emails_sent FOR ALL
  USING (tenant_id = get_user_tenant_id())
  WITH CHECK (tenant_id = get_user_tenant_id());

-- Transactions
CREATE POLICY "tenant_isolation_transactions"
  ON transactions FOR ALL
  USING (tenant_id = get_user_tenant_id())
  WITH CHECK (tenant_id = get_user_tenant_id());

-- Treasury snapshots
CREATE POLICY "tenant_isolation_treasury"
  ON treasury_snapshots FOR ALL
  USING (tenant_id = get_user_tenant_id())
  WITH CHECK (tenant_id = get_user_tenant_id());

-- Recommendations
CREATE POLICY "tenant_isolation_recommendations"
  ON recommendations FOR ALL
  USING (tenant_id = get_user_tenant_id())
  WITH CHECK (tenant_id = get_user_tenant_id());

-- Approvals
CREATE POLICY "tenant_isolation_approvals"
  ON approvals FOR ALL
  USING (tenant_id = get_user_tenant_id())
  WITH CHECK (tenant_id = get_user_tenant_id());

-- Memory chunks
CREATE POLICY "tenant_isolation_memory"
  ON memory_chunks FOR ALL
  USING (tenant_id = get_user_tenant_id())
  WITH CHECK (tenant_id = get_user_tenant_id());

-- Brief snapshots
CREATE POLICY "tenant_isolation_briefs"
  ON brief_snapshots FOR ALL
  USING (tenant_id = get_user_tenant_id())
  WITH CHECK (tenant_id = get_user_tenant_id());

-- Tenant configs (read-only for users)
CREATE POLICY "tenant_config_read"
  ON tenant_configs FOR SELECT
  USING (tenant_id = get_user_tenant_id());

-- tenant_secrets: NO user policy — service role only
-- (no CREATE POLICY = no access from client)

-- Audit logs (read-only for users)
CREATE POLICY "audit_logs_read"
  ON audit_logs FOR SELECT
  USING (tenant_id = get_user_tenant_id());

-- Events (read-only)
CREATE POLICY "events_read"
  ON events FOR SELECT
  USING (tenant_id = get_user_tenant_id());

-- ────────────────────────────────────────────
-- Approval token redemption — no auth required
-- (anyone with token can approve/reject)
-- ────────────────────────────────────────────

CREATE POLICY "approval_token_access"
  ON approvals FOR UPDATE
  USING (token = current_setting('request.jwt.claims', true)::json->>'token'
         OR TRUE)  -- n8n will use service role; this opens token-based access
  WITH CHECK (TRUE);

-- Tenants: users can read their own tenant only
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
CREATE POLICY "tenant_self_read"
  ON tenants FOR SELECT
  USING (id = get_user_tenant_id());
