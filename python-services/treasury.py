"""
treasury.py
Pure deterministic financial calculations.
NO LLM calls here. Ever.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TreasurySnapshot:
    tenant_id: str
    cash: float
    burn_rate: float
    projected_revenue: float
    runway_months: float
    alert_level: str          # HEALTHY | WARNING | CRITICAL
    safe_budget: float
    currency: str
    calculated_at: str
    data_freshness: str       # "live" | "stale" | "manual"
    warnings: list            # non-fatal data quality issues


def calculate_treasury(
    tenant_id: str,
    bank_balance: float,
    transactions: list,
    pipeline_deals: list,
    tenant_config: dict,
    data_freshness: str = "live"
) -> TreasurySnapshot:
    """
    Core treasury engine.
    
    Args:
        transactions: list of NormalizedTransaction
        pipeline_deals: list of dicts {amount, probability, close_date}
        tenant_config: {runway_warning_months, runway_critical_months, safety_buffer_months, currency}
    """
    warnings = []
    currency = tenant_config.get("currency", "USD")
    now = datetime.now(timezone.utc)

    # ── Validate inputs ───────────────────────────────────────────
    if bank_balance < 0:
        warnings.append("Bank balance is negative — data may be stale or include unsettled transactions")

    if not transactions:
        warnings.append("No transaction data — burn rate estimated from snapshot only")

    # ── Burn rate (trailing 30 days) ──────────────────────────────
    cutoff_30 = now - timedelta(days=30)
    recent = [
        t for t in transactions
        if _parse_date(t.date if hasattr(t, 'date') else t.get('date','')) >= cutoff_30
    ]

    expenses = sum(
        abs(float(t.amount if hasattr(t, 'amount') else t.get('amount', 0)))
        for t in recent
        if float(t.amount if hasattr(t, 'amount') else t.get('amount', 0)) < 0
    )
    revenue_realized = sum(
        float(t.amount if hasattr(t, 'amount') else t.get('amount', 0))
        for t in recent
        if float(t.amount if hasattr(t, 'amount') else t.get('amount', 0)) > 0
        and _get_category(t) not in ("transfer", "refund")
    )

    burn_rate = max(expenses - revenue_realized, 0)

    if burn_rate == 0:
        warnings.append("Burn rate calculated as 0 — no expense transactions found in last 30 days")

    # ── Projected revenue (pipeline, next 30 days) ────────────────
    cutoff_future = now + timedelta(days=30)
    projected = 0.0
    for deal in pipeline_deals:
        try:
            amount = float(deal.get("amount", 0))
            prob   = float(deal.get("probability", 0)) / 100
            close  = deal.get("close_date") or deal.get("closedate")
            if close and _parse_date(close) <= cutoff_future:
                projected += amount * prob
        except (ValueError, TypeError) as e:
            warnings.append(f"Skipped deal with bad data: {e}")
            continue

    # ── Runway ────────────────────────────────────────────────────
    if burn_rate > 0:
        runway = (bank_balance + projected) / burn_rate
    else:
        runway = 99.0  # effectively infinite — but flag it
        if bank_balance > 0:
            warnings.append("Runway set to 99 months — burn rate is 0 (check data)")

    # Sanity check — flag implausible values
    if runway > 120:
        warnings.append(f"Runway of {runway:.1f} months seems implausible — verify data")

    # ── Safe budget (growth spending ceiling) ─────────────────────
    safety_months  = float(tenant_config.get("safety_buffer_months", 2))
    safety_buffer  = burn_rate * safety_months
    safe_budget    = max(0.0, bank_balance + projected - safety_buffer - burn_rate)

    # ── Alert level ───────────────────────────────────────────────
    warning_threshold  = float(tenant_config.get("runway_warning_months", 6))
    critical_threshold = float(tenant_config.get("runway_critical_months", 3))

    if runway < critical_threshold:
        alert_level = "CRITICAL"
    elif runway < warning_threshold:
        alert_level = "WARNING"
    else:
        alert_level = "HEALTHY"

    snapshot = TreasurySnapshot(
        tenant_id        = tenant_id,
        cash             = round(bank_balance, 2),
        burn_rate        = round(burn_rate, 2),
        projected_revenue = round(projected, 2),
        runway_months    = round(min(runway, 99.0), 1),
        alert_level      = alert_level,
        safe_budget      = round(safe_budget, 2),
        currency         = currency,
        calculated_at    = now.isoformat(),
        data_freshness   = data_freshness,
        warnings         = warnings
    )

    logger.info(
        f"[{tenant_id}] Treasury: cash={bank_balance} burn={burn_rate:.0f} "
        f"runway={snapshot.runway_months}mo alert={alert_level}"
    )
    return snapshot


def score_lead(lead: dict, config: dict) -> dict:
    """
    Deterministic lead scoring.
    Returns {score: int, routing: str, breakdown: dict}
    """
    score = 0
    breakdown = {}

    # Company size (30 pts)
    size = lead.get("company_size") or 0
    if size >= 50:
        pts = 30
    elif size >= 10:
        pts = 20
    elif size >= 3:
        pts = 10
    else:
        pts = 0
    score += pts
    breakdown["company_size"] = pts

    # Role (10 pts)
    role = (lead.get("role") or "").upper()
    decision_makers = ["CEO", "FOUNDER", "CO-FOUNDER", "CTO", "COO", "CFO",
                       "VP", "DIRECTOR", "DAF", "DG", "PRESIDENT", "HEAD OF"]
    role_pts = 10 if any(d in role for d in decision_makers) else 0
    score += role_pts
    breakdown["role"] = role_pts

    # Budget (40 pts)
    notes = (lead.get("notes") or "").lower()
    budget_pts = 0
    import re
    budget_match = re.search(r'[\$€£]?\s*(\d[\d,\.]+)', notes)
    if budget_match:
        try:
            amount = float(budget_match.group(1).replace(",", ""))
            if amount >= 10000:
                budget_pts = 40
            elif amount >= 5000:
                budget_pts = 30
            elif amount >= 1000:
                budget_pts = 15
        except ValueError:
            pass
    score += budget_pts
    breakdown["budget"] = budget_pts

    # ICP industry (0 pts — informational only)
    icp = config.get("icp_industries", ["saas", "software", "tech", "fintech", "ecommerce", "dtc"])
    industry = (lead.get("industry") or lead.get("company_industry") or "").lower()
    breakdown["icp_match"] = any(i in industry for i in icp)

    # Urgency (20 pts)
    urgency_keywords = ["asap", "urgent", "this week", "this month", "immediately",
                        "dès que", "rapidement", "maintenant", "cette semaine"]
    urgency_pts = 20 if any(k in notes for k in urgency_keywords) else 0
    score += urgency_pts
    breakdown["urgency"] = urgency_pts

    final_score = min(score, 100)
    routing = "hot" if final_score >= 80 else "warm" if final_score >= 60 else "cold"

    return {"score": final_score, "routing": routing, "breakdown": breakdown}


# ── Helpers ───────────────────────────────────────────────────────

def _parse_date(val) -> datetime:
    """Parse date string to datetime. Returns epoch on failure."""
    if isinstance(val, datetime):
        return val.replace(tzinfo=timezone.utc) if val.tzinfo is None else val
    if not val:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            dt = datetime.strptime(str(val)[:26], fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _get_category(t) -> str:
    if hasattr(t, 'category'):
        return t.category or ""
    return t.get("category", "")
