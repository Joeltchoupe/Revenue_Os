"""
delivery/slack_digest.py
Revenue OS — Slack delivery engine

Handles all Slack output:
  - Alert (CRITICAL/WARNING): immediate push
  - Opportunity: batched or immediate depending on priority
  - Approval request: interactive buttons with approve/reject/snooze URLs
  - Brief: weekly structured digest

Features:
  - Deduplication via delivery_log (never double-send same signal)
  - Quiet hours enforcement (per tenant timezone)
  - Fallback to email if Slack fails on CRITICAL
  - delivery_log write for every attempt
  - Quota enforcement (max_alerts_per_day)
"""

import os
import json
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from uuid import uuid4
import pytz

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python-services'))
from utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)

WEBHOOK_BASE = os.environ.get("WEBHOOK_URL", "")


# ── Main entry point ──────────────────────────────────────

def deliver_recommendation(
    tenant_id:      str,
    recommendation: dict,
    tenant_config:  dict,
    base_url:       str = ""
) -> dict:
    """
    Master delivery function. Called by n8n after every recommendation insert.
    Handles: deduplication, quiet hours, quota, channel selection, fallback.

    Returns: {"delivered": bool, "channel": str, "status": str, "error": str|None}
    """
    sb            = get_supabase()
    rec_id        = recommendation.get("id")
    correlation_id = recommendation.get("correlation_id") or str(uuid4())
    priority      = recommendation.get("priority", "MEDIUM")

    # ── 1. Deduplication ─────────────────────────────────
    already_delivered = _already_delivered(tenant_id, rec_id, "slack")
    if already_delivered:
        logger.info(f"[slack] Skipping duplicate delivery for rec {rec_id}")
        return {"delivered": False, "channel": "slack", "status": "suppressed", "error": None}

    # ── 2. Quiet hours (CRITICAL bypasses) ───────────────
    if priority != "CRITICAL":
        if _in_quiet_hours(tenant_config):
            logger.info(f"[slack] Quiet hours — suppressing rec {rec_id}")
            _write_delivery_log(tenant_id, correlation_id, rec_id, "recommendation", "slack", "suppressed", error="quiet hours")
            return {"delivered": False, "channel": "slack", "status": "suppressed", "error": "quiet hours"}

    # ── 3. Quota check ────────────────────────────────────
    max_alerts = tenant_config.get("max_alerts_per_day", 10)
    quota_ok = _check_quota(tenant_id, "alerts_sent", max_alerts)
    if not quota_ok and priority not in ("CRITICAL",):
        logger.info(f"[slack] Daily quota reached for {tenant_id}")
        _write_delivery_log(tenant_id, correlation_id, rec_id, "recommendation", "slack", "suppressed", error="quota exceeded")
        return {"delivered": False, "channel": "slack", "status": "suppressed", "error": "daily quota reached"}

    # ── 4. Format message ─────────────────────────────────
    webhook_url = tenant_config.get("slack_webhook_url", "")
    if not webhook_url:
        logger.warning(f"[slack] No webhook configured for tenant {tenant_id}")
        _write_delivery_log(tenant_id, correlation_id, rec_id, "recommendation", "slack", "failed", error="no webhook configured")
        return {"delivered": False, "channel": "slack", "status": "failed", "error": "no webhook configured"}

    rec_type = recommendation.get("rec_type", "")
    message  = _format_message(recommendation, tenant_config, base_url)

    # ── 5. Send ───────────────────────────────────────────
    ok, error = _send_to_slack(webhook_url, message)

    status = "sent" if ok else "failed"
    _write_delivery_log(tenant_id, correlation_id, rec_id, "recommendation", "slack", status, error=error)

    # ── 6. Update recommendation delivery_status ──────────
    if ok:
        sb.table("recommendations").update({
            "delivery_status":   "delivered",
            "last_delivered_at": datetime.now(timezone.utc).isoformat(),
            "delivery_channel":  "slack",
            "delivery_attempts": (recommendation.get("delivery_attempts") or 0) + 1
        }).eq("id", rec_id).execute()
        _increment_product_metric(tenant_id, "signals_delivered")

    else:
        attempts = (recommendation.get("delivery_attempts") or 0) + 1
        sb.table("recommendations").update({
            "delivery_status":   "failed" if attempts >= 3 else "pending",
            "delivery_attempts": attempts
        }).eq("id", rec_id).execute()
        _increment_product_metric(tenant_id, "delivery_failures")

        # ── 7. Fallback to email on CRITICAL ──────────────
        if priority == "CRITICAL" and tenant_config.get("delivery_fallback") == "email":
            logger.warning(f"[slack] CRITICAL delivery failed — triggering email fallback for {rec_id}")
            _write_delivery_log(tenant_id, correlation_id, rec_id, "recommendation", "email", "fallback_used")
            return {"delivered": False, "channel": "slack", "status": "failed_fallback_email", "error": error}

    return {"delivered": ok, "channel": "slack", "status": status, "error": error}


def deliver_approval_request(
    tenant_id:      str,
    approval:       dict,
    recommendation: dict,
    tenant_config:  dict,
    base_url:       str = ""
) -> dict:
    """Send an approval request to Slack with Approve/Reject/Snooze buttons."""
    webhook_url = tenant_config.get("slack_webhook_url", "")
    if not webhook_url:
        return {"delivered": False, "status": "failed", "error": "no webhook"}

    approval_id    = approval.get("id")
    correlation_id = recommendation.get("correlation_id") or str(uuid4())

    # Deduplication
    if _already_delivered(tenant_id, approval_id, "slack"):
        return {"delivered": False, "status": "suppressed"}

    approve_url = f"{base_url}/v1/approvals/{approval_id}/approve"
    reject_url  = f"{base_url}/v1/approvals/{approval_id}/reject"
    snooze_url  = f"{base_url}/v1/approvals/{approval_id}/snooze"

    data = recommendation.get("data") or {}
    action_type = approval.get("action_type", "action")
    payload     = approval.get("payload") or {}
    estimated_impact = recommendation.get("estimated_impact", "")

    preview = _payload_preview_slack(action_type, payload)

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"⚡ Action Required — {_action_label(action_type)}"}
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{recommendation.get('why_recommended', 'Recommended action detected')}*\n{preview}"}
        }
    ]

    if estimated_impact:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"📊 Estimated impact: {estimated_impact}"}]
        })

    blocks.append({
        "type": "actions",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve"}, "style": "primary",  "url": approve_url},
            {"type": "button", "text": {"type": "plain_text", "text": "❌ Reject"},  "style": "danger",   "url": reject_url},
            {"type": "button", "text": {"type": "plain_text", "text": "⏰ Snooze 24h"}, "url": snooze_url}
        ]
    })

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"Expires in 48h · `{approval_id[:8]}` · <{base_url}/approvals|Open queue>"}]
    })

    ok, error = _send_to_slack(webhook_url, {"blocks": blocks, "text": f"Action required: {_action_label(action_type)}"})
    status = "sent" if ok else "failed"
    _write_delivery_log(tenant_id, correlation_id, approval_id, "approval", "slack", status, error=error)

    if ok:
        get_supabase().table("approvals").update({"channel": "slack"}).eq("id", approval_id).execute()
        _increment_product_metric(tenant_id, "approvals_sent")

    return {"delivered": ok, "status": status, "error": error}


def deliver_brief(
    tenant_id:     str,
    brief:         dict,
    tenant_config: dict,
    base_url:      str = ""
) -> dict:
    """Deliver weekly brief to Slack."""
    webhook_url = tenant_config.get("slack_webhook_url", "")
    if not webhook_url:
        return {"delivered": False, "status": "failed", "error": "no webhook"}

    brief_id       = brief.get("id")
    correlation_id = brief.get("correlation_id") or str(uuid4())

    if _already_delivered(tenant_id, brief_id, "slack"):
        return {"delivered": False, "status": "suppressed"}

    meta       = brief.get("raw_context") or {}
    t          = meta.get("treasury", {})
    ls         = meta.get("lead_stats", {})
    cnt        = meta.get("rec_counts", {})
    currency   = t.get("currency", "USD")
    alert_emoji = {"CRITICAL": "🚨", "WARNING": "⚠️", "HEALTHY": "✅"}.get(t.get("alert_level", ""), "📊")
    week_start = brief.get("week_start", "this week")
    today_str  = datetime.now(timezone.utc).strftime("%A %d %b %Y")

    header_text = f"🧠 Weekly Revenue OS Brief — {today_str}"
    stats_text  = (
        f"{alert_emoji} Runway: *{t.get('runway_months','?')} months* ({t.get('alert_level','?')})  "
        f"·  Leads: *{ls.get('total_leads',0)}* ({ls.get('hot_leads',0)} hot)  "
        f"·  Critical items: {cnt.get('critical',0)}"
    )

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text}},
        {"type": "section", "text": {"type": "mrkdwn", "text": stats_text}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": brief.get("brief_text", "_Brief unavailable_")}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{base_url}/briefs/{brief_id}|View full brief> · Week of {week_start}"}]}
    ]

    ok, error = _send_to_slack(webhook_url, {"blocks": blocks, "text": header_text})
    status = "sent" if ok else "failed"
    _write_delivery_log(tenant_id, correlation_id, brief_id, "brief", "slack", status, error=error)

    if ok:
        get_supabase().table("brief_snapshots").update({
            "delivery_status": "delivered",
            "delivery_channel": "slack",
            "delivered_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", brief_id).execute()

    return {"delivered": ok, "status": status, "error": error}


# ── Internal helpers ──────────────────────────────────────

def _send_to_slack(webhook_url: str, payload: dict) -> tuple[bool, Optional[str]]:
    """Send to Slack webhook. Returns (success, error_detail)."""
    for attempt in range(3):
        try:
            r = requests.post(webhook_url, json=payload, timeout=10)
            if r.status_code == 200 and r.text == "ok":
                return True, None
            if r.status_code == 429:
                import time; time.sleep(2 ** attempt)
                continue
            return False, f"HTTP {r.status_code}: {r.text[:100]}"
        except requests.exceptions.Timeout:
            if attempt == 2:
                return False, "Slack timeout after 3 attempts"
        except Exception as e:
            return False, str(e)
    return False, "All retries exhausted"


def _already_delivered(tenant_id: str, object_id: str, channel: str) -> bool:
    """Check delivery_log for existing successful delivery of this object on this channel."""
    try:
        res = get_supabase().table("delivery_log")\
            .select("id")\
            .eq("tenant_id", tenant_id)\
            .eq("object_id", object_id)\
            .eq("channel", channel)\
            .eq("status", "sent")\
            .limit(1)\
            .execute()
        return bool(res.data)
    except Exception as e:
        logger.warning(f"dedup check failed: {e}")
        return False


def _in_quiet_hours(tenant_config: dict) -> bool:
    """Returns True if current time (in tenant timezone) is within quiet hours."""
    tz_name = tenant_config.get("timezone", "UTC")
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.UTC

    now_local = datetime.now(tz)
    current_hour = now_local.hour
    start = tenant_config.get("quiet_hours_start", 22)
    end   = tenant_config.get("quiet_hours_end",   7)

    if start > end:  # spans midnight (e.g. 22–7)
        return current_hour >= start or current_hour < end
    return start <= current_hour < end


def _check_quota(tenant_id: str, field: str, limit: int) -> bool:
    """Check and increment daily quota. Returns True if within limit."""
    try:
        res = get_supabase().rpc("check_and_increment_quota", {
            "p_tenant_id": tenant_id,
            "p_field":     field,
            "p_limit":     limit
        }).execute()
        return bool(res.data)
    except Exception as e:
        logger.warning(f"quota check failed: {e}")
        return True  # fail open — don't block delivery on quota error


def _write_delivery_log(
    tenant_id: str, correlation_id: str, object_id: Optional[str],
    object_type: str, channel: str, status: str,
    error: Optional[str] = None, attempt: int = 1
):
    try:
        get_supabase().table("delivery_log").insert({
            "tenant_id":      tenant_id,
            "correlation_id": correlation_id,
            "object_type":    object_type,
            "object_id":      object_id,
            "channel":        channel,
            "status":         status,
            "provider":       "slack-webhook",
            "error_detail":   error,
            "attempt_number": attempt
        }).execute()
    except Exception as e:
        logger.warning(f"delivery_log write failed: {e}")


def _increment_product_metric(tenant_id: str, field: str):
    try:
        get_supabase().rpc("increment_metric", {"p_tenant_id": tenant_id, "p_field": field}).execute()
    except Exception as e:
        logger.warning(f"metric increment failed: {e}")


def _format_message(recommendation: dict, tenant_config: dict, base_url: str) -> dict:
    """Format a recommendation as a Slack message dict."""
    priority = recommendation.get("priority", "MEDIUM")
    data     = recommendation.get("data") or {}
    emoji    = {"CRITICAL": "🚨", "HIGH": "⚠️", "MEDIUM": "📌", "LOW": "ℹ️"}.get(priority, "📌")
    domain   = _rec_domain(recommendation.get("rec_type", ""))
    title    = data.get("title") or _auto_title(recommendation)
    body     = data.get("explanation") or data.get("action") or data.get("recommendation") or ""
    impact   = recommendation.get("estimated_impact", "")

    text = f"{emoji} *{domain} — {title}*\n{body}"
    if impact:
        text += f"\n_Impact: {impact}_"

    return {"text": text}


def _payload_preview_slack(action_type: str, payload: dict) -> str:
    if action_type == "send_email":
        return f"*Email to:* {payload.get('to_email','?')}\n*Subject:* {payload.get('subject','?')[:80]}\n```{payload.get('body','')[:300]}```"
    return payload.get("description", str(payload)[:300])


def _action_label(action_type: str) -> str:
    return {
        "send_email":       "Send re-engagement email",
        "create_crm_task":  "Create CRM task",
        "log_crm_note":     "Log CRM note",
        "slack_notify":     "Send Slack notification",
        "update_deal_stage":"Update deal stage"
    }.get(action_type, action_type)


def _rec_domain(rec_type: str) -> str:
    mapping = {
        "dormant_lead":"LEADS","post_demo":"LEADS","next_best_action":"LEADS",
        "stuck_deal_unblock":"PIPELINE","cash_alert":"TREASURY","zombie_spend":"SPEND"
    }
    return mapping.get(rec_type, "SYSTEM")


def _auto_title(rec: dict) -> str:
    return {
        "dormant_lead":"Dormant lead","post_demo":"No demo follow-up",
        "stuck_deal_unblock":"Deal stuck","cash_alert":"Cash alert"
    }.get(rec.get("rec_type",""), "Signal detected")
