-- ============================================================
-- 002_memory.sql
-- Revenue OS — Vector memory (pgvector)
-- Run AFTER 001_core_schema.sql
-- ============================================================

-- pgvector already enabled in 001, but safe to repeat
CREATE EXTENSION IF NOT EXISTS vector;

-- ────────────────────────────────────────────
-- MEMORY CHUNKS
-- Stores successful patterns for RAG retrieval.
-- Dimensions: 1536 (text-embedding-3-small)
-- ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS memory_chunks (
  id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  content     TEXT NOT NULL,
  embedding   vector(1536),
  metadata    JSONB DEFAULT '{}',
  -- metadata shape:
  -- {
  --   "type":         "successful_email" | "deal_unblock" | "brief_pattern",
  --   "agent":        "02_hot_lead_engagement",
  --   "outcome_score": 85,
  --   "rec_id":       "uuid"
  -- }
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_memory_chunks_tenant ON memory_chunks(tenant_id);

-- HNSW index for fast similarity search
-- Parameters tuned for accuracy/speed balance at < 500K vectors per tenant
CREATE INDEX idx_memory_chunks_embedding
  ON memory_chunks
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- ────────────────────────────────────────────
-- SIMILARITY SEARCH FUNCTION
-- Called by Python service or n8n HTTP node
-- ────────────────────────────────────────────

CREATE OR REPLACE FUNCTION search_memory(
  p_tenant_id   UUID,
  p_embedding   vector(1536),
  p_filter_type TEXT DEFAULT NULL,    -- filter by metadata.type if provided
  p_limit       INT  DEFAULT 3
)
RETURNS TABLE (
  id          UUID,
  content     TEXT,
  metadata    JSONB,
  similarity  FLOAT
)
LANGUAGE plpgsql
AS $$
BEGIN
  RETURN QUERY
  SELECT
    mc.id,
    mc.content,
    mc.metadata,
    1 - (mc.embedding <=> p_embedding) AS similarity
  FROM memory_chunks mc
  WHERE mc.tenant_id = p_tenant_id
    AND (p_filter_type IS NULL OR mc.metadata->>'type' = p_filter_type)
    AND mc.embedding IS NOT NULL
  ORDER BY mc.embedding <=> p_embedding
  LIMIT p_limit;
END;
$$;

-- ────────────────────────────────────────────
-- EMBED & STORE HELPER
-- Convenience view for the feedback loop
-- ────────────────────────────────────────────

CREATE OR REPLACE VIEW high_outcome_recommendations AS
SELECT
  r.id,
  r.tenant_id,
  r.agent,
  r.rec_type,
  r.data,
  r.outcome_score,
  r.outcome_note,
  r.created_at
FROM recommendations r
WHERE r.outcome_score >= 70
  AND r.outcome_score IS NOT NULL;
