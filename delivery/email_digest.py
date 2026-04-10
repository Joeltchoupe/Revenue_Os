"""
delivery/email_digest.py
Revenue OS — Email delivery engine

Handles:
  - Weekly brief email (board memo style)
  - CRITICAL treasury/pipeline alerts
  - Fallback when Slack fails

Features:
  - HTML templates versioned in code (no external template service needed)
  - Deduplication via delivery_log
  - Quiet hours (skipped for CRITICAL)
  - delivery_log write on every attempt
"""

import os
import logging
import requests
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python-services'))
from utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)

TEMPLATE_VERSION = "v1"


# ── Main entry points ─────────────────────────────────────

def deliver_brief_email(
    tenant_id:     str,
    brief:         dict,
    tenant_config: dict,
    base_url:      str = ""
) -> dict:
    """Send weekly brief as a styled HTML email."""
    if not tenant_config.get("email_digest_enabled", True):
        return {"delivered": False, "status": "suppressed", "error": "email digest disabled"}

    to_email       = tenant_config.get("email_from_address") or tenant_config.get("founder_email", "")
    brief_id       = brief.get("id")
    correlation_id = brief.get("correlation_id") or str(uuid4())

    if not to_email:
        _write_delivery_log(tenant_id, correlation_id, brief_id, "brief", "email", "failed", "no recipient email configured")
        return {"delivered": False, "status": "failed", "error": "no recipient email configured"}

    if _already_delivered(tenant_id, brief_id, "email"):
        return {"delivered": False, "status": "suppressed"}

    subject = f"Revenue OS Weekly Brief — {brief.get('week_start', 'this week')}"
    html    = _render_brief_html(brief, tenant_config, base_url)
    text    = brief.get("brief_text", "Brief unavailable")

    ok, error = _send_email(tenant_config, to_email, subject, text, html)
    status = "sent" if ok else "failed"
    _write_delivery_log(tenant_id, correlation_id, brief_id, "brief", "email", status, error=error)

    if ok:
        get_supabase().table("brief_snapshots").update({
            "delivery_status":  "delivered",
            "delivery_channel": "email",
            "delivered_at":     datetime.now(timezone.utc).isoformat()
        }).eq("id", brief_id).execute()

    return {"delivered": ok, "status": status, "error": error}


def deliver_alert_email(
    tenant_id:     str,
    recommendation: dict,
    tenant_config: dict,
    base_url:      str = ""
) -> dict:
    """Send a CRITICAL/WARNING alert as email (primary or fallback channel)."""
    to_email       = tenant_config.get("founder_email") or tenant_config.get("email_from_address", "")
    rec_id         = recommendation.get("id")
    correlation_id = recommendation.get("correlation_id") or str(uuid4())
    priority       = recommendation.get("priority", "MEDIUM")

    if not to_email:
        return {"delivered": False, "status": "failed", "error": "no recipient email"}

    if _already_delivered(tenant_id, rec_id, "email"):
        return {"delivered": False, "status": "suppressed"}

    data    = recommendation.get("data") or {}
    domain  = _rec_domain(recommendation.get("rec_type", ""))
    emoji   = "🚨" if priority == "CRITICAL" else "⚠️"
    title   = data.get("title") or f"{domain} Alert"
    body    = data.get("explanation") or data.get("action") or data.get("recommendation") or ""
    impact  = recommendation.get("estimated_impact", "")

    subject = f"{emoji} [{priority}] Revenue OS — {title}"
    html    = _render_alert_html(title, body, impact, priority, base_url, rec_id)
    text    = f"{emoji} {title}\n\n{body}"
    if impact:
        text += f"\n\nEstimated impact: {impact}"

    ok, error = _send_email(tenant_config, to_email, subject, text, html)
    status = "sent" if ok else "failed"
    _write_delivery_log(tenant_id, correlation_id, rec_id, "recommendation", "email", status, error=error)

    if ok:
        get_supabase().table("recommendations").update({
            "delivery_status":   "delivered",
            "last_delivered_at": datetime.now(timezone.utc).isoformat(),
            "delivery_channel":  "email"
        }).eq("id", rec_id).execute()

    return {"delivered": ok, "status": status, "error": error}


# ── HTML Templates (versioned) ───────────────────────────

def _render_brief_html(brief: dict, tenant_config: dict, base_url: str) -> str:
    meta       = brief.get("raw_context") or {}
    t          = meta.get("treasury", {})
    ls         = meta.get("lead_stats", {})
    ds         = meta.get("deal_stats", {})
    currency   = t.get("currency", "USD")
    week_start = brief.get("week_start", "")
    brief_text = brief.get("brief_text", "").replace("\n", "<br>")

    fmt        = lambda n: f"{float(n or 0):,.0f}"
    alert_color = {"CRITICAL": "#dc2626", "WARNING": "#d97706", "HEALTHY": "#16a34a"}.get(t.get("alert_level", ""), "#6b7280")
    today_str  = datetime.now(timezone.utc).strftime("%B %d, %Y")
    brief_url  = f"{base_url}/briefs/{brief.get('id', '')}"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Revenue OS Weekly Brief</title></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f9fafb;padding:24px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1);">

  <!-- Header -->
  <tr><td style="background:#1a1a2e;padding:24px 32px;">
    <p style="margin:0;color:#ffffff;font-size:11px;letter-spacing:2px;text-transform:uppercase;opacity:0.6;">REVENUE OS</p>
    <h1 style="margin:4px 0 0;color:#ffffff;font-size:22px;font-weight:700;">Weekly Brief</h1>
    <p style="margin:4px 0 0;color:#9ca3af;font-size:13px;">{today_str} · Week of {week_start}</p>
  </td></tr>

  <!-- Key Metrics -->
  <tr><td style="padding:24px 32px 0;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td style="padding:12px;background:#f3f4f6;border-radius:6px;text-align:center;width:25%;">
          <p style="margin:0;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:1px;">Runway</p>
          <p style="margin:4px 0 0;font-size:22px;font-weight:700;color:{alert_color};">{t.get('runway_months','?')}mo</p>
        </td>
        <td width="8"></td>
        <td style="padding:12px;background:#f3f4f6;border-radius:6px;text-align:center;width:25%;">
          <p style="margin:0;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:1px;">Cash</p>
          <p style="margin:4px 0 0;font-size:18px;font-weight:700;color:#1a1a2e;">{currency} {fmt(t.get('cash'))}</p>
        </td>
        <td width="8"></td>
        <td style="padding:12px;background:#f3f4f6;border-radius:6px;text-align:center;width:25%;">
          <p style="margin:0;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:1px;">New Leads</p>
          <p style="margin:4px 0 0;font-size:22px;font-weight:700;color:#1a1a2e;">{ls.get('total_leads',0)}</p>
        </td>
        <td width="8"></td>
        <td style="padding:12px;background:#f3f4f6;border-radius:6px;text-align:center;width:25%;">
          <p style="margin:0;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:1px;">Stuck Deals</p>
          <p style="margin:4px 0 0;font-size:22px;font-weight:700;color:#1a1a2e;">{ds.get('stuck_count',0)}</p>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- Brief text -->
  <tr><td style="padding:24px 32px;">
    <h2 style="margin:0 0 12px;font-size:16px;color:#1a1a2e;font-weight:700;border-bottom:2px solid #e5e7eb;padding-bottom:8px;">
      This Week's Priorities
    </h2>
    <div style="font-size:14px;line-height:1.7;color:#374151;">{brief_text}</div>
  </td></tr>

  <!-- CTA -->
  <tr><td style="padding:0 32px 24px;text-align:center;">
    <a href="{brief_url}" style="display:inline-block;background:#1a1a2e;color:#ffffff;padding:12px 28px;border-radius:6px;text-decoration:none;font-size:14px;font-weight:600;">
      Open in Revenue OS →
    </a>
  </td></tr>

  <!-- Footer -->
  <tr><td style="background:#f9fafb;padding:16px 32px;border-top:1px solid #e5e7eb;">
    <p style="margin:0;font-size:11px;color:#9ca3af;text-align:center;">
      Revenue OS · Template {TEMPLATE_VERSION} · You receive this because you're a Revenue OS operator.
    </p>
  </td></tr>

</table>
</td></tr></table>
</body></html>"""


def _render_alert_html(title: str, body: str, impact: str, priority: str, base_url: str, rec_id: str) -> str:
    color    = {"CRITICAL": "#dc2626", "WARNING": "#d97706"}.get(priority, "#6b7280")
    emoji    = "🚨" if priority == "CRITICAL" else "⚠️"
    body_html = body.replace("\n", "<br>")
    impact_row = f'<tr><td style="padding:12px 32px;background:#fef3c7;"><p style="margin:0;font-size:13px;color:#92400e;"><strong>Estimated impact:</strong> {impact}</p></td></tr>' if impact else ""
    action_url = f"{base_url}/feed"

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px 0;background:#f9fafb;font-family:Arial,sans-serif;">
<table width="600" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:8px;overflow:hidden;margin:0 auto;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
  <tr><td style="background:{color};padding:20px 32px;">
    <h1 style="margin:0;color:#fff;font-size:18px;">{emoji} {priority} — Revenue OS</h1>
  </td></tr>
  <tr><td style="padding:24px 32px;">
    <h2 style="margin:0 0 12px;font-size:16px;color:#1a1a2e;">{title}</h2>
    <div style="font-size:14px;line-height:1.7;color:#374151;">{body_html}</div>
  </td></tr>
  {impact_row}
  <tr><td style="padding:16px 32px 24px;text-align:center;">
    <a href="{action_url}" style="display:inline-block;background:#1a1a2e;color:#fff;padding:11px 24px;border-radius:6px;text-decoration:none;font-size:14px;font-weight:600;">
      View in Revenue OS →
    </a>
  </td></tr>
  <tr><td style="background:#f9fafb;padding:12px 32px;border-top:1px solid #e5e7eb;">
    <p style="margin:0;font-size:11px;color:#9ca3af;text-align:center;">Template {TEMPLATE_VERSION} · rec:{rec_id[:8]}</p>
  </td></tr>
</table>
</body></html>"""


# ── Sending layer ─────────────────────────────────────────

def _send_email(tenant_config: dict, to: str, subject: str, text: str, html: str) -> tuple[bool, Optional[str]]:
    """Route to Resend. Returns (success, error_detail)."""
    provider = tenant_config.get("email_provider", "resend")
    if provider == "resend":
        return _send_via_resend(tenant_config, to, subject, text, html)
    return False, f"Unknown email provider: {provider}"


def _send_via_resend(config: dict, to: str, subject: str, text: str, html: str) -> tuple[bool, Optional[str]]:
    api_key    = config.get("resend_api_key", "")
    from_email = config.get("email_from_address", "noreply@example.com")
    from_name  = config.get("email_from_name", "Revenue OS")

    if not api_key:
        return False, "Resend API key not configured"

    payload = {
        "from":    f"{from_name} <{from_email}>",
        "to":      [to],
        "subject": subject,
        "text":    text,
        "html":    html
    }

    for attempt in range(3):
        try:
            r = requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=15
            )
            if r.status_code == 200:
                return True, None
            if r.status_code == 401:
                return False, "Resend: Invalid API key (401)"
            if r.status_code == 429:
                import time; time.sleep(2 ** attempt)
                continue
            return False, f"Resend: HTTP {r.status_code} — {r.text[:200]}"
        except requests.exceptions.Timeout:
            if attempt == 2:
                return False, "Resend: timeout after 3 attempts"
        except Exception as e:
            return False, str(e)
    return False, "Resend: all retries exhausted"


# ── Helpers (shared with slack_digest) ───────────────────

def _already_delivered(tenant_id: str, object_id: str, channel: str) -> bool:
    try:
        res = get_supabase().table("delivery_log")\
            .select("id").eq("tenant_id", tenant_id)\
            .eq("object_id", object_id).eq("channel", channel)\
            .eq("status", "sent").limit(1).execute()
        return bool(res.data)
    except Exception:
        return False


def _write_delivery_log(tenant_id, correlation_id, object_id, object_type, channel, status, error=None, attempt=1):
    try:
        get_supabase().table("delivery_log").insert({
            "tenant_id": tenant_id, "correlation_id": correlation_id,
            "object_type": object_type, "object_id": object_id,
            "channel": channel, "status": status,
            "provider": "resend" if channel == "email" else channel,
            "error_detail": error, "attempt_number": attempt
        }).execute()
    except Exception as e:
        logger.warning(f"delivery_log write failed: {e}")


def _rec_domain(rec_type: str) -> str:
    mapping = {"dormant_lead":"LEADS","post_demo":"LEADS","next_best_action":"LEADS",
               "stuck_deal_unblock":"PIPELINE","cash_alert":"TREASURY","zombie_spend":"SPEND"}
    return mapping.get(rec_type, "SYSTEM")
