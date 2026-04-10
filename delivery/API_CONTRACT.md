# Revenue OS — Delivery API Contract v1

**Base URL:** `https://your-api.up.railway.app`  
**Version:** `v1` (all endpoints prefixed `/v1/`)  
**Auth:** Supabase JWT via `Authorization: Bearer <token>` header  
**Tenant resolution:** JWT → `tenant_id` claim → RLS enforcement  

---

## Stability Guarantees

| Status | Meaning |
|--------|---------|
| **stable** | Will not change in v1. Breaking changes → v2. |
| **beta** | May change with notice. |

---

## Endpoints

### GET /health
`stable` — No auth required.

**Response:**
```json
{ "status": "ok", "version": "v1" }
```

---

### GET /v1/system/state
`stable` — Lightweight header state for UI badge/sidebar.

**Response:** `SystemState` object
```json
{
  "tenant_id": "uuid",
  "pending_approvals": 3,
  "critical_alerts": 1,
  "connector_health": {
    "crm": "healthy",
    "bank": "degraded",
    "slack": "healthy"
  },
  "execution_mode": "approval",
  "last_signal_at": "2025-01-15T08:00:00Z",
  "weekly_action_rate": 0.72
}
```

---

### GET /v1/feed
`stable` — Unified alert + opportunity feed.

**Query params:**
- `page` (int, default 1)
- `limit` (int, default 20, max 100)
- `domain` (string, optional): TREASURY | PIPELINE | LEADS | SPEND | SYSTEM
- `level` (string, optional): CRITICAL | HIGH | MEDIUM | LOW

**Response:** Paginated list of `Alert | Opportunity` objects
```json
{
  "data": [
    {
      "id": "uuid",
      "correlation_id": "string",
      "type": "Alert | Opportunity",
      "domain": "TREASURY",
      "level": "WARNING",
      "title": "Runway at 4.5 months",
      "body": "...",
      "why_recommended": "...",
      "estimated_impact": "saves ~$800/mo",
      "entity": { "type": "deal", "id": "uuid", "name": "...", "value": 12000 },
      "status": "pending",
      "delivery_status": "delivered",
      "snooze_until": null,
      "created_at": "2025-01-15T08:00:00Z",
      "first_viewed_at": null,
      "first_acted_at": null
    }
  ],
  "meta": { "total": 42, "page": 1, "limit": 20, "has_more": true }
}
```

**Exclusions:** Snoozed items (unless snooze expired), rejected, expired.

---

### GET /v1/approvals
`stable` — Pending approvals with action context.

**Query params:** `page`, `limit` (max 50)

**Response:** Paginated list of `ApprovalItem` objects
```json
{
  "data": [
    {
      "id": "uuid",
      "recommendation_id": "uuid",
      "correlation_id": "string",
      "action_type": "send_email",
      "action_label": "Send re-engagement email to Alice",
      "payload_preview": "Subject: Quick question...\n\nHey Alice...",
      "status": "pending",
      "execution_status": "not_started",
      "expires_at": "2025-01-17T08:00:00Z",
      "snooze_until": null,
      "channel": "slack",
      "created_at": "2025-01-15T08:00:00Z"
    }
  ],
  "meta": { "total": 3, "page": 1, "limit": 20, "has_more": false }
}
```

---

### POST /v1/approvals/{id}/approve
`stable` — Approve an action. **Idempotent.**

**Body:** `{ "reason": "optional string" }`

**Success 200:**
```json
{ "status": "approved", "actioned_at": "2025-01-15T09:00:00Z" }
```

**Error responses:**
| Code | HTTP | Meaning |
|------|------|---------|
| `NOT_FOUND` | 404 | Approval not found or wrong tenant |
| `ALREADY_ACTIONED` | 409 | Already approved/rejected |
| `APPROVAL_EXPIRED` | 410 | Token expired |

---

### POST /v1/approvals/{id}/reject
`stable` — Reject an action.

**Body:** `{ "reason": "optional" }`

**Success 200:** `{ "status": "rejected", "actioned_at": "..." }`

---

### POST /v1/approvals/{id}/snooze
`stable` — Snooze — re-surfaces after N hours.

**Body:** `{ "hours": 24 }` (1–168)

**Success 200:** `{ "status": "snoozed", "snooze_until": "2025-01-16T09:00:00Z" }`

---

### POST /v1/recommendations/{id}/view
`stable` — Mark a recommendation as first viewed (time-to-view metric).
Idempotent — only sets `first_viewed_at` once.

**Success 200:** `{ "ok": true }`

---

### GET /v1/treasury/status
`stable` — Latest treasury snapshot.

**Response:** `TreasuryStatus` object
```json
{
  "tenant_id": "uuid",
  "cash": 145000,
  "burn_rate": 18500,
  "projected_revenue": 32000,
  "runway_months": 7.2,
  "alert_level": "HEALTHY",
  "safe_budget": 8500,
  "currency": "USD",
  "data_freshness": "live",
  "warnings": [],
  "calculated_at": "2025-01-15T08:00:00Z"
}
```

**404** if no snapshot exists yet.

---

### GET /v1/briefs
`stable` — Brief history (most recent first).

**Query params:** `limit` (default 10, max 52)

**Response:**
```json
{
  "data": [
    {
      "id": "uuid",
      "brief_text": "...",
      "week_start": "2025-01-13",
      "key_metrics": {
        "runway_months": 7.2,
        "alert_level": "HEALTHY",
        "new_leads": 8,
        "stuck_deals": 2,
        "rec_critical": 0,
        "rec_high": 3
      },
      "delivery_status": "delivered",
      "delivered_at": "2025-01-13T09:00:00Z",
      "first_viewed_at": "2025-01-13T09:47:00Z",
      "created_at": "2025-01-13T09:00:00Z"
    }
  ]
}
```

---

## Backward Compatibility Rules

1. **Never remove a field** from a response — add new fields only
2. **Never change a field type** — new fields use new names
3. **Status enum additions** are non-breaking
4. **New endpoints** are always additive (v1 stable)
5. **Breaking changes** → bump to `/v2/` with a 90-day deprecation notice for v1

---

## Rate Limits

| Endpoint class | Limit |
|----------------|-------|
| Read (GET) | 300 req/min per tenant |
| Approvals (POST) | 60 req/min per tenant |
| Feed | 120 req/min per tenant |

Rate limit exceeded → **429** with `Retry-After` header.

---

## Error Format (all errors)

```json
{
  "error": "Human-readable message",
  "code": "MACHINE_READABLE_CODE",
  "details": {}
}
```

Error codes: `NOT_FOUND` | `UNAUTHORIZED` | `FORBIDDEN` | `ALREADY_ACTIONED` | `APPROVAL_EXPIRED` | `QUOTA_EXCEEDED` | `VALIDATION_ERROR` | `INTERNAL_ERROR`
