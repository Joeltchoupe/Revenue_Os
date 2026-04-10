"""
delivery/web_app_api.py
Revenue OS — Delivery API v1

Versioned, tenant-aware, entitlement-aware facade for Lovable UI.
This is the ONLY layer Lovable talks to for write operations.
Lovable may also listen to Supabase Realtime for read feed (hybrid model).

Endpoints:
  GET  /v1/system/state
  GET  /v1/feed            — alerts + opportunities (paginated)
  GET  /v1/approvals       — pending approvals
  GET  /v1/briefs          — brief history
  GET  /v1/treasury/status — latest snapshot
  POST /v1/approvals/{id}/approve
  POST /v1/approvals/{id}/reject
  POST /v1/approvals/{id}/snooze
  POST /v1/recommendations/{id}/view   — marks first_viewed_at
"""

import os
import logging
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from uuid import UUID

from fastapi import FastAPI, HTTPException, Depends, Header, Query, Path
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python-services'))
from utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")

API_VERSION = "v1"
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "https://yourdomain.com").split(",")


# ── App setup ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Revenue OS Delivery API starting")
    yield
    logger.info("Revenue OS Delivery API stopping")


app = FastAPI(
    title="Revenue OS — Delivery API",
    version=API_VERSION,
    lifespan=lifespan,
    docs_url="/v1/docs",
    redoc_url=None
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-Tenant-Id"],
)


# ── Auth + tenant resolution ──────────────────────────────

class TenantContext:
    def __init__(self, tenant_id: str, execution_mode: str, entitlements: dict):
        self.tenant_id      = tenant_id
        self.execution_mode = execution_mode
        self.entitlements   = entitlements


async def resolve_tenant(
    authorization: str = Header(None),
    x_tenant_id:   str = Header(None)
) -> TenantContext:
    """
    Resolve tenant from Supabase JWT.
    JWT contains: sub (user_id), tenant_id (custom claim).
    In development, x_tenant_id header is accepted if no JWT.
    """
    sb = get_supabase()

    if not authorization and not x_tenant_id:
        raise HTTPException(status_code=401, detail="Authorization required")

    tenant_id = None

    # Production: validate JWT via Supabase
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        try:
            user = sb.auth.get_user(token)
            if not user or not user.user:
                raise HTTPException(status_code=401, detail="Invalid token")
            # tenant_id is a custom claim — set via Supabase Edge Function on login
            claims = user.user.user_metadata or {}
            tenant_id = claims.get("tenant_id") or x_tenant_id
        except Exception as e:
            logger.warning(f"JWT validation failed: {e}")
            raise HTTPException(status_code=401, detail="Token validation failed")

    # Development fallback (disable in production via env flag)
    if not tenant_id and x_tenant_id and os.environ.get("ALLOW_HEADER_AUTH") == "true":
        tenant_id = x_tenant_id

    if not tenant_id:
        raise HTTPException(status_code=401, detail="Could not resolve tenant")

    # Load config + entitlements
    try:
        config_res = sb.table("tenant_configs").select("*").eq("tenant_id", tenant_id).single().execute()
        config = config_res.data or {}

        lic_res = sb.table("licenses").select("status,entitlements,expires_at").eq("tenant_id", tenant_id).eq("status", "active").limit(1).execute()
        license = (lic_res.data or [{}])[0]

        # Check license validity
        expires_at = license.get("expires_at")
        if expires_at and datetime.fromisoformat(expires_at.replace("Z", "+00:00")) < datetime.now(timezone.utc):
            raise HTTPException(status_code=403, detail="License expired")

        entitlements = license.get("entitlements") or {}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Tenant resolution DB error: {e}")
        raise HTTPException(status_code=500, detail="Tenant resolution failed")

    return TenantContext(
        tenant_id=tenant_id,
        execution_mode=config.get("execution_mode", "approval"),
        entitlements=entitlements
    )


# ── Pydantic models ───────────────────────────────────────

class ApprovalActionRequest(BaseModel):
    reason: Optional[str] = None

class SnoozeRequest(BaseModel):
    hours: int = Field(default=24, ge=1, le=168)  # 1h–7d


# ── Health ────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "version": API_VERSION}


# ── System State ──────────────────────────────────────────

@app.get(f"/{API_VERSION}/system/state")
async def get_system_state(ctx: TenantContext = Depends(resolve_tenant)):
    """Lightweight header state for UI: pending approvals count, alert level, connector health."""
    sb = get_supabase()
    tid = ctx.tenant_id

    try:
        # Pending approvals
        pending_res = sb.table("approvals")\
            .select("id", count="exact")\
            .eq("tenant_id", tid)\
            .eq("status", "pending")\
            .gt("expires_at", datetime.now(timezone.utc).isoformat())\
            .execute()
        pending_count = pending_res.count or 0

        # Critical alerts
        critical_res = sb.table("recommendations")\
            .select("id", count="exact")\
            .eq("tenant_id", tid)\
            .eq("priority", "CRITICAL")\
            .eq("status", "pending")\
            .execute()
        critical_count = critical_res.count or 0

        # Connector health
        health_res = sb.table("connector_health")\
            .select("connector,status")\
            .eq("tenant_id", tid)\
            .execute()
        connector_health = {r["connector"]: r["status"] for r in (health_res.data or [])}

        # Config
        config_res = sb.table("tenant_configs").select("execution_mode").eq("tenant_id", tid).single().execute()
        exec_mode = (config_res.data or {}).get("execution_mode", "approval")

        # Last signal
        last_sig_res = sb.table("recommendations")\
            .select("created_at")\
            .eq("tenant_id", tid)\
            .order("created_at", desc=True)\
            .limit(1)\
            .execute()
        last_signal_at = (last_sig_res.data or [{}])[0].get("created_at")

        # Weekly action rate
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
        metrics_res = sb.table("product_metrics")\
            .select("approvals_sent,approvals_actioned")\
            .eq("tenant_id", tid)\
            .gte("metric_date", week_ago)\
            .execute()
        sent    = sum(r.get("approvals_sent", 0) for r in (metrics_res.data or []))
        actioned = sum(r.get("approvals_actioned", 0) for r in (metrics_res.data or []))
        action_rate = round(actioned / sent, 2) if sent > 0 else None

        return {
            "tenant_id":          tid,
            "pending_approvals":  pending_count,
            "critical_alerts":    critical_count,
            "connector_health":   connector_health,
            "execution_mode":     exec_mode,
            "last_signal_at":     last_signal_at,
            "weekly_action_rate": action_rate
        }

    except Exception as e:
        logger.error(f"system_state failed for {tid}: {e}")
        raise HTTPException(status_code=500, detail="Failed to load system state")


# ── Signal Feed ───────────────────────────────────────────

@app.get(f"/{API_VERSION}/feed")
async def get_feed(
    ctx:    TenantContext = Depends(resolve_tenant),
    page:   int = Query(default=1, ge=1),
    limit:  int = Query(default=20, ge=1, le=100),
    domain: Optional[str] = Query(default=None),
    level:  Optional[str] = Query(default=None),
):
    """
    Unified alert + opportunity feed. Sorted by priority then recency.
    Excludes snoozed items (unless snooze_until has passed).
    """
    sb = get_supabase()
    offset = (page - 1) * limit

    try:
        q = sb.table("recommendations")\
            .select("id,correlation_id,agent,rec_type,data,priority,status,delivery_status,why_recommended,estimated_impact,snooze_until,created_at,first_viewed_at,first_acted_at", count="exact")\
            .eq("tenant_id", ctx.tenant_id)\
            .not_.eq("status", "rejected")\
            .not_.eq("status", "expired")

        # Exclude snoozed items that haven't expired yet
        now_iso = datetime.now(timezone.utc).isoformat()
        q = q.or_(f"snooze_until.is.null,snooze_until.lte.{now_iso}")

        if domain:
            q = q.ilike("rec_type", f"%{domain.lower()}%")
        if level:
            q = q.eq("priority", level.upper())

        q = q.order("priority", desc=False)\
             .order("created_at", desc=True)\
             .range(offset, offset + limit - 1)

        res = q.execute()

        items = []
        for r in (res.data or []):
            data = r.get("data") or {}
            items.append({
                "id":               r["id"],
                "correlation_id":   r.get("correlation_id"),
                "type":             _classify_rec_type(r.get("rec_type", "")),
                "domain":           _rec_domain(r.get("rec_type", "")),
                "level":            r.get("priority", "MEDIUM"),
                "title":            data.get("title") or _auto_title(r),
                "body":             data.get("explanation") or data.get("action") or data.get("recommendation") or "",
                "why_recommended":  r.get("why_recommended"),
                "estimated_impact": r.get("estimated_impact"),
                "entity":           _extract_entity(data),
                "status":           r.get("status"),
                "delivery_status":  r.get("delivery_status"),
                "snooze_until":     r.get("snooze_until"),
                "created_at":       r.get("created_at"),
                "first_viewed_at":  r.get("first_viewed_at"),
                "first_acted_at":   r.get("first_acted_at")
            })

        return {
            "data": items,
            "meta": {
                "total":    res.count or 0,
                "page":     page,
                "limit":    limit,
                "has_more": (res.count or 0) > offset + limit
            }
        }

    except Exception as e:
        logger.error(f"feed failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to load feed")


# ── Approvals ─────────────────────────────────────────────

@app.get(f"/{API_VERSION}/approvals")
async def list_approvals(
    ctx:   TenantContext = Depends(resolve_tenant),
    page:  int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=50)
):
    """List pending approvals with action context."""
    sb  = get_supabase()
    offset = (page - 1) * limit
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        res = sb.table("approvals")\
            .select("id,recommendation_id,correlation_id,action_type,payload,status,execution_status,expires_at,snooze_until,channel,created_at", count="exact")\
            .eq("tenant_id", ctx.tenant_id)\
            .eq("status", "pending")\
            .gt("expires_at", now_iso)\
            .order("created_at", desc=True)\
            .range(offset, offset + limit - 1)\
            .execute()

        items = []
        for r in (res.data or []):
            payload = r.get("payload") or {}
            items.append({
                "id":                r["id"],
                "recommendation_id": r.get("recommendation_id"),
                "correlation_id":    r.get("correlation_id"),
                "action_type":       r.get("action_type"),
                "action_label":      _action_label(r.get("action_type"), payload),
                "payload_preview":   _payload_preview(r.get("action_type"), payload),
                "status":            r.get("status"),
                "execution_status":  r.get("execution_status"),
                "expires_at":        r.get("expires_at"),
                "snooze_until":      r.get("snooze_until"),
                "channel":           r.get("channel"),
                "created_at":        r.get("created_at")
            })

        return {
            "data": items,
            "meta": {
                "total":    res.count or 0,
                "page":     page,
                "limit":    limit,
                "has_more": (res.count or 0) > offset + limit
            }
        }
    except Exception as e:
        logger.error(f"list_approvals failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to load approvals")


@app.post(f"/{API_VERSION}/approvals/{{approval_id}}/approve")
async def approve_action(
    approval_id: UUID = Path(...),
    body:        ApprovalActionRequest = ApprovalActionRequest(),
    ctx:         TenantContext = Depends(resolve_tenant)
):
    """Approve an action. Idempotent — safe to call twice."""
    return await _act_on_approval(str(approval_id), "approved", ctx.tenant_id, body.reason)


@app.post(f"/{API_VERSION}/approvals/{{approval_id}}/reject")
async def reject_action(
    approval_id: UUID = Path(...),
    body:        ApprovalActionRequest = ApprovalActionRequest(),
    ctx:         TenantContext = Depends(resolve_tenant)
):
    """Reject an action."""
    return await _act_on_approval(str(approval_id), "rejected", ctx.tenant_id, body.reason)


@app.post(f"/{API_VERSION}/approvals/{{approval_id}}/snooze")
async def snooze_action(
    approval_id: UUID = Path(...),
    body:        SnoozeRequest = SnoozeRequest(),
    ctx:         TenantContext = Depends(resolve_tenant)
):
    """Snooze an approval — re-surfaces after snooze_until."""
    sb = get_supabase()
    snooze_until = (datetime.now(timezone.utc) + timedelta(hours=body.hours)).isoformat()

    try:
        # Fetch and validate
        res = sb.table("approvals").select("*")\
            .eq("id", str(approval_id))\
            .eq("tenant_id", ctx.tenant_id)\
            .single().execute()

        if not res.data:
            raise HTTPException(status_code=404, code="NOT_FOUND", detail="Approval not found")

        ap = res.data
        if ap["status"] not in ("pending",):
            raise HTTPException(status_code=409, detail={"code": "ALREADY_ACTIONED", "error": f"Approval is already {ap['status']}"})

        sb.table("approvals").update({
            "status": "snoozed",
            "snooze_until": snooze_until
        }).eq("id", str(approval_id)).execute()

        # Also snooze the parent recommendation
        if ap.get("recommendation_id"):
            sb.table("recommendations").update({
                "snooze_until": snooze_until,
                "delivery_status": "snoozed"
            }).eq("id", ap["recommendation_id"]).execute()

        _write_audit(ctx.tenant_id, "approval_snoozed", "approval", str(approval_id), {"snooze_until": snooze_until})

        return {"status": "snoozed", "snooze_until": snooze_until}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"snooze failed: {e}")
        raise HTTPException(status_code=500, detail="Snooze failed")


# ── Recommendations — view tracking ──────────────────────

@app.post(f"/{API_VERSION}/recommendations/{{rec_id}}/view")
async def mark_viewed(
    rec_id: UUID = Path(...),
    ctx:    TenantContext = Depends(resolve_tenant)
):
    """Mark recommendation as first viewed (for time-to-first-view metric)."""
    sb = get_supabase()
    try:
        # Only set if not already set (first view only)
        sb.table("recommendations")\
            .update({"first_viewed_at": datetime.now(timezone.utc).isoformat()})\
            .eq("id", str(rec_id))\
            .eq("tenant_id", ctx.tenant_id)\
            .is_("first_viewed_at", None)\
            .execute()
        return {"ok": True}
    except Exception as e:
        logger.warning(f"mark_viewed failed: {e}")
        return {"ok": False}


# ── Treasury ──────────────────────────────────────────────

@app.get(f"/{API_VERSION}/treasury/status")
async def get_treasury_status(ctx: TenantContext = Depends(resolve_tenant)):
    """Latest treasury snapshot — safe read model."""
    sb = get_supabase()
    try:
        res = sb.table("treasury_snapshots")\
            .select("*")\
            .eq("tenant_id", ctx.tenant_id)\
            .order("calculated_at", desc=True)\
            .limit(1)\
            .execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="No treasury snapshot found")
        snap = res.data[0]
        # Return clean model — never raw DB row
        return {
            "tenant_id":         snap["tenant_id"],
            "correlation_id":    snap.get("correlation_id"),
            "cash":              snap["cash"],
            "burn_rate":         snap["burn_rate"],
            "projected_revenue": snap["projected_revenue"],
            "runway_months":     snap["runway_months"],
            "alert_level":       snap["alert_level"],
            "safe_budget":       snap["safe_budget"],
            "currency":          snap["currency"],
            "data_freshness":    snap.get("data_freshness", "unknown"),
            "warnings":          snap.get("warnings") or [],
            "calculated_at":     snap["calculated_at"]
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"treasury_status failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to load treasury status")


# ── Briefs ────────────────────────────────────────────────

@app.get(f"/{API_VERSION}/briefs")
async def list_briefs(
    ctx:   TenantContext = Depends(resolve_tenant),
    limit: int = Query(default=10, ge=1, le=52)
):
    """Return brief history."""
    sb = get_supabase()
    try:
        res = sb.table("brief_snapshots")\
            .select("id,correlation_id,brief_text,week_start,delivery_status,delivered_at,first_viewed_at,raw_context,created_at")\
            .eq("tenant_id", ctx.tenant_id)\
            .order("week_start", desc=True)\
            .limit(limit)\
            .execute()

        return {
            "data": [
                {
                    "id":              r["id"],
                    "correlation_id":  r.get("correlation_id"),
                    "brief_text":      r["brief_text"],
                    "week_start":      r["week_start"],
                    "key_metrics":     _extract_brief_metrics(r.get("raw_context") or {}),
                    "delivery_status": r.get("delivery_status"),
                    "delivered_at":    r.get("delivered_at"),
                    "first_viewed_at": r.get("first_viewed_at"),
                    "created_at":      r["created_at"]
                }
                for r in (res.data or [])
            ]
        }
    except Exception as e:
        logger.error(f"list_briefs failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to load briefs")


# ── Private helpers ───────────────────────────────────────

async def _act_on_approval(approval_id: str, new_status: str, tenant_id: str, reason: Optional[str]) -> dict:
    """
    Transaction: validate → update status → audit → trigger execution event.
    Idempotent: if already in target status, returns success silently.
    """
    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()

    try:
        res = sb.table("approvals").select("*")\
            .eq("id", approval_id)\
            .eq("tenant_id", tenant_id)\
            .single().execute()

        if not res.data:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "error": "Approval not found"})

        ap = res.data

        # Idempotency: already in target state
        if ap["status"] == new_status:
            return {"status": new_status, "idempotent": True}

        # Terminal states — cannot change
        if ap["status"] in ("approved", "rejected"):
            raise HTTPException(status_code=409, detail={"code": "ALREADY_ACTIONED", "error": f"Approval already {ap['status']}"})

        # Expiry check
        expires_at = datetime.fromisoformat(ap["expires_at"].replace("Z", "+00:00"))
        if expires_at < datetime.now(timezone.utc):
            raise HTTPException(status_code=410, detail={"code": "APPROVAL_EXPIRED", "error": "Approval link has expired"})

        # Idempotency key check — prevent double execution
        idem_key = f"{approval_id}:{new_status}"
        idem_hash = hashlib.sha256(idem_key.encode()).hexdigest()[:32]

        # Update approval
        sb.table("approvals").update({
            "status":      new_status,
            "actioned_at": now
        }).eq("id", approval_id).execute()

        # Update recommendation status + first_acted_at
        if ap.get("recommendation_id"):
            sb.table("recommendations").update({
                "status":        new_status,
                "first_acted_at": now,
                "updated_at":    now
            }).eq("id", ap["recommendation_id"])\
              .is_("first_acted_at", None)\
              .execute()

        # Write approval event (n8n polls this for execution)
        sb.table("events").insert({
            "tenant_id":    tenant_id,
            "event_type":   f"approval.{new_status}",
            "source":       "delivery_api",
            "payload": {
                "approval_id":       approval_id,
                "recommendation_id": ap.get("recommendation_id"),
                "action_type":       ap.get("action_type"),
                "payload":           ap.get("payload"),
                "reason":            reason,
                "idempotency_hash":  idem_hash
            },
            "processed": False
        }).execute()

        # Audit
        _write_audit(tenant_id, f"approval_{new_status}", "approval", approval_id, {
            "action_type": ap.get("action_type"),
            "reason":      reason
        })

        # Increment metrics
        sb.rpc("increment_metric", {"p_tenant_id": tenant_id, "p_field": "approvals_actioned"}).execute()
        if new_status == "approved":
            sb.rpc("increment_metric", {"p_tenant_id": tenant_id, "p_field": "approvals_approved"}).execute()
        elif new_status == "rejected":
            sb.rpc("increment_metric", {"p_tenant_id": tenant_id, "p_field": "approvals_rejected"}).execute()

        return {"status": new_status, "actioned_at": now}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"act_on_approval failed [{approval_id}]: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Action failed — please try again")


def _write_audit(tenant_id: str, action: str, resource_type: str, resource_id: str, data: dict):
    try:
        get_supabase().table("audit_logs").insert({
            "tenant_id":     tenant_id,
            "actor":         "founder",
            "action":        action,
            "resource_type": resource_type,
            "resource_id":   resource_id,
            "data":          data
        }).execute()
    except Exception as e:
        logger.warning(f"Audit log write failed: {e}")


def _classify_rec_type(rec_type: str) -> str:
    alerts = {"cash_alert", "runway_warning", "system_error"}
    if rec_type in alerts or "alert" in rec_type:
        return "Alert"
    if "approval" in rec_type:
        return "Approval"
    return "Opportunity"


def _rec_domain(rec_type: str) -> str:
    mapping = {
        "dormant_lead": "LEADS", "post_demo": "LEADS", "next_best_action": "LEADS",
        "stuck_deal_unblock": "PIPELINE", "pipeline_stagnation": "PIPELINE",
        "cash_alert": "TREASURY", "runway_warning": "TREASURY",
        "zombie_spend": "SPEND", "ad_optimization": "SPEND"
    }
    return mapping.get(rec_type, "SYSTEM")


def _auto_title(rec: dict) -> str:
    titles = {
        "dormant_lead":       "Dormant lead detected",
        "post_demo":          "No follow-up after demo",
        "stuck_deal_unblock": "Deal stuck in pipeline",
        "cash_alert":         "Treasury alert",
        "next_best_action":   "Recommended action"
    }
    return titles.get(rec.get("rec_type", ""), "Signal")


def _extract_entity(data: dict) -> Optional[dict]:
    if "lead_id" in data or "email" in data:
        return {"type": "lead", "id": data.get("lead_id"), "name": data.get("name", ""), "company": data.get("company", "")}
    if "deal_id" in data:
        return {"type": "deal", "id": data.get("deal_id"), "name": data.get("deal_name", ""), "value": data.get("deal_amount")}
    return None


def _action_label(action_type: str, payload: dict) -> str:
    labels = {
        "send_email":       f"Send email to {payload.get('to_email', 'contact')}",
        "create_crm_task":  "Create CRM task",
        "log_crm_note":     "Log CRM note",
        "slack_notify":     "Send Slack notification",
        "update_deal_stage":"Update deal stage"
    }
    return labels.get(action_type, action_type)


def _payload_preview(action_type: str, payload: dict) -> str:
    """Safe, PII-limited preview of what will be executed."""
    if action_type == "send_email":
        subject = payload.get("subject", "")[:80]
        body    = payload.get("body", "")[:200]
        return f"Subject: {subject}\n\n{body}..."
    if action_type in ("create_crm_task", "log_crm_note"):
        return payload.get("note", payload.get("description", ""))[:300]
    return str(payload)[:300]


def _extract_brief_metrics(raw_context: dict) -> dict:
    t  = raw_context.get("treasury", {})
    ls = raw_context.get("lead_stats", {})
    ds = raw_context.get("deal_stats", {})
    cnt = raw_context.get("rec_counts", {})
    return {
        "runway_months": t.get("runway_months"),
        "alert_level":   t.get("alert_level"),
        "new_leads":     ls.get("total_leads"),
        "stuck_deals":   ds.get("stuck_count"),
        "rec_critical":  cnt.get("critical"),
        "rec_high":      cnt.get("high")
    }
