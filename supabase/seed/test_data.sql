-- ============================================================
-- seed/test_data.sql
-- Test data for local dev and staging. DO NOT run on production.
-- Run: psql $DATABASE_URL -f seed/test_data.sql
-- ============================================================

-- Insert test tenant
INSERT INTO tenants (id, name, slug, plan, status, timezone, currency)
VALUES (
  'a0000000-0000-0000-0000-000000000001',
  'Acme SaaS (Test)',
  'acme-test',
  'light',
  'active',
  'Africa/Douala',
  'USD'
) ON CONFLICT (slug) DO NOTHING;

-- Insert tenant config
INSERT INTO tenant_configs (tenant_id, execution_mode, max_emails_day, runway_warning_months, runway_critical_months, crm_provider, bank_provider)
VALUES (
  'a0000000-0000-0000-0000-000000000001',
  'approval',
  50,
  6,
  3,
  'hubspot',
  'manual'
) ON CONFLICT (tenant_id) DO NOTHING;

-- Insert test leads — mix of dormant, post-demo, hot, cold
INSERT INTO leads (tenant_id, crm_id, name, email, company, role, industry, company_size, notes, score, routing, status, last_activity_at, created_at)
VALUES
  ('a0000000-0000-0000-0000-000000000001', 'hs_001', 'Alice Martin', 'alice@techcorp.io', 'TechCorp', 'CEO', 'SaaS', 25, 'Interested in automation, budget $12k, ASAP', 90, 'hot', 'new', NOW() - INTERVAL '35 days', NOW() - INTERVAL '40 days'),
  ('a0000000-0000-0000-0000-000000000001', 'hs_002', 'Bob Kamga', 'bob@logistics.cm', 'LogiCam', 'Directeur', 'Logistics', 15, 'Demo done, liked the product, no follow-up', 65, 'warm', 'new', NOW() - INTERVAL '16 days', NOW() - INTERVAL '20 days'),
  ('a0000000-0000-0000-0000-000000000001', 'hs_003', 'Chloé Bernard', 'chloe@dtc-brand.fr', 'FrenchDTC', 'Founder', 'DTC', 8, 'Interested, budget unclear', 55, 'warm', 'new', NOW() - INTERVAL '10 days', NOW() - INTERVAL '12 days'),
  ('a0000000-0000-0000-0000-000000000001', 'hs_004', 'David Osei', 'david@coldlead.gh', 'ColdCo', 'Sales Rep', 'Retail', 3, '', 15, 'cold', 'new', NOW() - INTERVAL '5 days', NOW() - INTERVAL '5 days')
ON CONFLICT (tenant_id, crm_id) DO NOTHING;

-- Insert test deals
INSERT INTO deals (tenant_id, crm_id, name, stage, amount, probability, close_date, status, stage_entered_at, last_activity_at)
VALUES
  ('a0000000-0000-0000-0000-000000000001', 'deal_001', 'TechCorp Automation', 'proposal_sent', 12000, 60, NOW() + INTERVAL '15 days', 'open', NOW() - INTERVAL '18 days', NOW() - INTERVAL '18 days'),
  ('a0000000-0000-0000-0000-000000000001', 'deal_002', 'LogiCam Q3 Deal', 'demo_done', 8500, 40, NOW() + INTERVAL '30 days', 'open', NOW() - INTERVAL '9 days', NOW() - INTERVAL '9 days'),
  ('a0000000-0000-0000-0000-000000000001', 'deal_003', 'FrenchDTC Pilot', 'negotiation', 5000, 80, NOW() + INTERVAL '7 days', 'open', NOW() - INTERVAL '22 days', NOW() - INTERVAL '22 days')
ON CONFLICT (tenant_id, crm_id) DO NOTHING;

-- Insert test transactions (last 90 days)
INSERT INTO transactions (tenant_id, external_id, date, amount, description, category, source)
SELECT
  'a0000000-0000-0000-0000-000000000001',
  'test_tx_' || generate_series,
  (NOW() - INTERVAL '1 day' * generate_series)::DATE,
  CASE WHEN generate_series % 3 = 0 THEN 3500 ELSE -2800 END,
  CASE WHEN generate_series % 3 = 0 THEN 'Customer payment' ELSE 'Operational expense' END,
  CASE WHEN generate_series % 3 = 0 THEN 'revenue' ELSE 'other' END,
  'manual'
FROM generate_series(1, 60)
ON CONFLICT (tenant_id, external_id) DO NOTHING;

-- Insert a test memory chunk (simulates past successful email)
INSERT INTO memory_chunks (tenant_id, content, metadata)
VALUES (
  'a0000000-0000-0000-0000-000000000001',
  'Email sent to SaaS CEO re-engagement after demo. Subject: Quick question about your sales cycle. Outcome: Reply received (100% score)',
  '{"type": "successful_email", "agent": "01_detect_score_email", "outcome_score": 100, "subject": "Quick question about your sales cycle"}'
) ON CONFLICT DO NOTHING;
