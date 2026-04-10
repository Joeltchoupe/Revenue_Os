"""
delivery/approvals_api.py
Revenue OS — Approval transaction engine

Standalone module for processing approvals triggered by:
  - Token URL click (Slack/Email button)
  - Lovable UI (via web_app_api.py → events table)
  - n8n event polling

This is the SINGLE place where approval state transitions happen.
Everything else (Slack, Email, UI) delegates here.

State machine:
  pending → approved   (founder clicks approve)
  pending → rejected   (founder clicks reject)
  pending → snoozed    (founder clicks snooze)
  pending → expired    (cron after expires_at)
  approved → (execution_status) running → succeeded | failed
"""

import hashlib
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import uuid4

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python-services'))
from utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────

TERMINAL_STATUSES     = {"approved", "rejected", "expired"}
AUTO_EXECUTABLE_TYPES = {"slack_notify", "create_crm_task", "log_crm_note"}


# ── Core transaction ──────────────────────────────────────

class ApprovalError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        self.code        = code
        self.message     = message
        self.http_status = http_status
        super().__init__(message)


def process_approval(
    approval_id: str,
    new_status:  str,            # approved | rejected | snoozed
    tenant_id:   Optional[str],  # None = token-based (no auth context)
    token:       Optional[str],  # One-time token from Slack/Email
    reason:      Optional[str] = None,
    snooze_hours: int = 24
) -> dict:
    """
    Atomic approval state transition.

    Resolution priority:
    1. Token (from Slack/Email button click) — no auth required
    2. tenant_id (from authenticated UI call)

    Returns: {"status": new_status, "approval_id": str, "idempotent": bool}
    Raises: ApprovalError with code + http_status
    """
    sb  = get_supabase()
    now = datetime.now(timezone.utc)

    # ── Fetch approval ────────────────────────────────────
    if token:
        res = sb.table("approvals").select("*").eq("token", token).limit(1).execute()
        if not res.data:
            raise ApprovalError("NOT_FOUND", "Token not found or already used", 404)
        approval = res.data[0]
        # Validate tenant claim if also provided
        if tenant_id and approval["tenant_id"] != tenant_id:
            raise ApprovalError("UNAUTHORIZED", "Tenant mismatch", 403)
    elif tenant_id:
        res = sb.table("approvals").select("*")\
            .eq("id", approval_id).eq("tenant_id", tenant_id).limit(1).execute()
        if not res.data:
            raise ApprovalError("NOT_FOUND", "Approval not found", 404)
        approval = res.data[0]
    else:
        raise ApprovalError("UNAUTHORIZED", "No auth context provided", 401)

    tid = approval["tenant_id"]

    # ── Idempotency: already in target state ──────────────
    if approval["status"] == new_status:
        return {"status": new_status, "approval_id": approval["id"], "idempotent": True}

    # ── Terminal state guard ──────────────────────────────
    if approval["status"] in TERMINAL_STATUSES:
        raise ApprovalError(
            "ALREADY_ACTIONED",
            f"Approval is already {approval['status']} — cannot change",
            409
        )

    # ── Expiry check ──────────────────────────────────────
    expires_at = datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00"))
    if expires_at < now:
        # Auto-expire in DB
        sb.table("approvals").update({"status": "expired"}).eq("id", approval["id"]).execute()
        raise ApprovalError("APPROVAL_EXPIRED", "This approval link has expired", 410)

    # ── Idempotency key (prevent double execution) ─────────
    idempotency_key = hashlib.sha256(f"{approval['id']}:{new_status}".encode()).hexdigest()[:32]

    # ── Build update ──────────────────────────────────────
    update = {
        "status":          new_status,
        "actioned_at":     now.isoformat(),
        "idempotency_key": idempotency_key
    }

    if new_status == "snoozed":
        snooze_until = (now + timedelta(hours=snooze_hours)).isoformat()
        update["snooze_until"] = snooze_until

    # ── Write state transition ─────────────────────────────
    sb.table("approvals").update(update).eq("id", approval["id"]).execute()

    # ── Update parent recommendation ──────────────────────
    rec_id = approval.get("recommendation_id")
    if rec_id:
        rec_update = {"status": new_status, "updated_at": now.isoformat()}
        if new_status == "snoozed":
            rec_update["snooze_until"] = update["snooze_until"]
            rec_update["delivery_status"] = "snoozed"
        # Set first_acted_at only once
        sb.table("recommendations").update(rec_update)\
            .eq("id", rec_id).execute()
        sb.table("recommendations").update({"first_acted_at": now.isoformat()})\
            .eq("id", rec_id).is_("first_acted_at", None).execute()

    # ── Write event for n8n execution ─────────────────────
    if new_status == "approved":
        _emit_execution_event(sb, approval, tid, reason, idempotency_key)

    # ── Audit ──────────────────────────────────────────────
    _write_audit(sb, tid, f"approval_{new_status}", "approval", approval["id"], {
        "action_type": approval.get("action_type"),
        "reason":      reason,
        "channel":     approval.get("channel"),
        "token_used":  bool(token)
    })

    # ── Metrics ───────────────────────────────────────────
    _increment_metrics(sb, tid, new_status)

    logger.info(f"[approvals] {approval['id']} → {new_status} (tenant={tid})")
    return {"status": new_status, "approval_id": approval["id"], "idempotent": False}


def expire_stale_approvals(tenant_id: Optional[str] = None) -> int:
    """
    Mark past-expiry pending approvals as expired.
    Run by cron (n8n schedule or Supabase Edge Function).
    Returns count of expired approvals.
    """
    sb  = get_supabase()
    now = datetime.now(timezone.utc).isoformat()

    q = sb.table("approvals").update({"status": "expired"})\
        .eq("status", "pending").lt("expires_at", now)

    if tenant_id:
        q = q.eq("tenant_id", tenant_id)

    res = q.execute()
    count = len(res.data or [])
    if count > 0:
        logger.info(f"[approvals] Expired {count} stale approvals")
    return count


def get_pending_summary(tenant_id: str) -> dict:
    """Return summary of pending approvals for UI badge."""
    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    try:
        res = sb.table("approvals").select("id,action_type,expires_at", count="exact")\
            .eq("tenant_id", tenant_id)\
            .eq("status", "pending")\
            .gt("expires_at", now)\
            .execute()
        return {
            "count":      res.count or 0,
            "oldest_at":  min((r["expires_at"] for r in (res.data or [])), default=None),
            "action_types": list({r["action_type"] for r in (res.data or [])})
        }
    except Exception as e:
        logger.warning(f"get_pending_summary failed: {e}")
        return {"count": 0}


# ── Helpers ───────────────────────────────────────────────

def _emit_execution_event(sb, approval: dict, tenant_id: str, reason: Optional[str], idempotency_key: str):
    """Write event that n8n polls for execution."""
    try:
        sb.table("events").insert({
            "tenant_id":  tenant_id,
            "event_type": "approval.approved",
            "source":     "approvals_api",
            "payload": {
                "approval_id":       approval["id"],
                "recommendation_id": approval.get("recommendation_id"),
                "action_type":       approval.get("action_type"),
                "payload":           approval.get("payload"),
                "reason":            reason,
                "idempotency_key":   idempotency_key,
                "correlation_id":    approval.get("correlation_id")
            },
            "processed": False
        }).execute()
    except Exception as e:
        logger.error(f"Failed to emit execution event: {e}")
        # Don't re-raise — the approval state is already updated


def _write_audit(sb, tenant_id: str, action: str, resource_type: str, resource_id: str, data: dict):
    try:
        sb.table("audit_logs").insert({
            "tenant_id": tenant_id, "actor": "founder",
            "action": action, "resource_type": resource_type,
            "resource_id": resource_id, "data": data
        }).execute()
    except Exception as e:
        logger.warning(f"audit_log failed: {e}")


def _increment_metrics(sb, tenant_id: str, new_status: str):
    fields = {
        "approved": ["approvals_actioned", "approvals_approved"],
        "rejected": ["approvals_actioned", "approvals_rejected"],
        "snoozed":  ["approvals_actioned"]
    }
    for field in fields.get(new_status, []):
        try:
            sb.rpc("increment_metric", {"p_tenant_id": tenant_id, "p_field": field}).execute()
        except Exception as e:
            logger.warning(f"metric increment failed [{field}]: {e}")
