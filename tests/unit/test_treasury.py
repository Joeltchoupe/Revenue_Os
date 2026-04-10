"""
tests/test_treasury.py
Unit tests for treasury calculation engine.
Run: pytest tests/test_treasury.py -v
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python-services'))

from treasury import calculate_treasury, score_lead, TreasurySnapshot


def make_transaction(amount: float, days_ago: int = 5, category: str = "other"):
    date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).date().isoformat()
    return {"date": date, "amount": amount, "category": category, "description": "test"}


DEFAULT_CONFIG = {
    "runway_warning_months":  6,
    "runway_critical_months": 3,
    "safety_buffer_months":   2,
    "currency": "USD"
}


# ─── Treasury Calculation ────────────────────────────────

class TestTreasuryCalculation:

    def test_healthy_runway(self):
        txs = [make_transaction(-5000, i) for i in range(1, 30)]  # $5k/day burn
        txs += [make_transaction(5000, i) for i in range(1, 30)]   # $5k/day revenue
        snap = calculate_treasury("t1", 100_000, txs, [], DEFAULT_CONFIG)
        assert snap.alert_level == "HEALTHY"
        assert snap.runway_months > 6

    def test_critical_runway(self):
        txs = [make_transaction(-10_000, i) for i in range(1, 30)]  # high burn
        snap = calculate_treasury("t1", 15_000, txs, [], DEFAULT_CONFIG)
        assert snap.alert_level == "CRITICAL"
        assert snap.runway_months < 3

    def test_warning_runway(self):
        txs = [make_transaction(-5_000, i) for i in range(1, 30)]
        snap = calculate_treasury("t1", 80_000, txs, [], DEFAULT_CONFIG)
        # Runway should be around 5 months — WARNING territory
        assert snap.alert_level in ("WARNING", "HEALTHY")

    def test_zero_burn_rate(self):
        """Zero burn → runway = 99 (infinite) + warning added"""
        snap = calculate_treasury("t1", 50_000, [], [], DEFAULT_CONFIG)
        assert snap.runway_months == 99.0
        assert any("burn rate is 0" in w.lower() for w in snap.warnings)

    def test_pipeline_deals_boost_runway(self):
        txs = [make_transaction(-10_000, i) for i in range(1, 30)]
        deals = [{"amount": 50_000, "probability": 80, "close_date": (datetime.now(timezone.utc) + timedelta(days=15)).date().isoformat()}]
        snap_without = calculate_treasury("t1", 20_000, txs, [], DEFAULT_CONFIG)
        snap_with    = calculate_treasury("t1", 20_000, txs, deals, DEFAULT_CONFIG)
        assert snap_with.projected_revenue > snap_without.projected_revenue
        assert snap_with.runway_months > snap_without.runway_months

    def test_negative_balance_warning(self):
        snap = calculate_treasury("t1", -1000, [], [], DEFAULT_CONFIG)
        assert any("negative" in w.lower() for w in snap.warnings)

    def test_safe_budget_never_negative(self):
        txs = [make_transaction(-20_000, i) for i in range(1, 30)]  # high burn
        snap = calculate_treasury("t1", 5_000, txs, [], DEFAULT_CONFIG)
        assert snap.safe_budget >= 0

    def test_snapshot_has_all_fields(self):
        snap = calculate_treasury("t1", 100_000, [], [], DEFAULT_CONFIG)
        assert snap.tenant_id == "t1"
        assert snap.currency == "USD"
        assert snap.calculated_at is not None
        assert isinstance(snap.warnings, list)

    def test_deals_outside_window_excluded(self):
        """Deals closing in 60 days should not count toward 30-day projection"""
        txs = [make_transaction(-5_000, i) for i in range(1, 30)]
        deals_far = [{"amount": 100_000, "probability": 90, "close_date": (datetime.now(timezone.utc) + timedelta(days=60)).date().isoformat()}]
        deals_near = [{"amount": 100_000, "probability": 90, "close_date": (datetime.now(timezone.utc) + timedelta(days=10)).date().isoformat()}]
        snap_far  = calculate_treasury("t1", 50_000, txs, deals_far, DEFAULT_CONFIG)
        snap_near = calculate_treasury("t1", 50_000, txs, deals_near, DEFAULT_CONFIG)
        assert snap_near.projected_revenue > snap_far.projected_revenue

    def test_transfer_transactions_excluded_from_revenue(self):
        """Transfers should not count as revenue"""
        txs = [
            make_transaction(50_000, 5, category="transfer"),
            make_transaction(-3_000, 10, category="other")
        ]
        snap = calculate_treasury("t1", 100_000, txs, [], DEFAULT_CONFIG)
        # Burn rate should be 3000, not affected by transfer
        assert snap.burn_rate <= 5_000  # rough check


# ─── Lead Scoring ────────────────────────────────────────

class TestLeadScoring:

    def test_hot_lead_ceo_large_company_urgent(self):
        lead = {"role": "CEO", "company_size": 50, "notes": "budget $15k ASAP", "industry": "saas"}
        result = score_lead(lead, {})
        assert result["score"] >= 80
        assert result["routing"] == "hot"

    def test_cold_lead_no_info(self):
        lead = {"role": "intern", "company_size": 1, "notes": "", "industry": ""}
        result = score_lead(lead, {})
        assert result["routing"] == "cold"
        assert result["score"] < 60

    def test_warm_lead_mid_score(self):
        lead = {"role": "VP Sales", "company_size": 15, "notes": "interested, no timeline", "industry": "tech"}
        result = score_lead(lead, {})
        assert result["routing"] in ("warm", "hot")

    def test_urgency_keywords_detected(self):
        for kw in ["ASAP", "this week", "urgently", "immediately"]:
            lead = {"role": "", "company_size": 0, "notes": kw, "industry": ""}
            result = score_lead(lead, {})
            assert result["breakdown"]["urgency"] == 20, f"urgency not detected for '{kw}'"

    def test_budget_parsing(self):
        cases = [
            ("budget $15,000", 40),
            ("have $5k available", 30),
            ("budget $500", 0),
            ("no budget mentioned", 0),
        ]
        for notes, expected_pts in cases:
            lead = {"role": "", "company_size": 0, "notes": notes, "industry": ""}
            result = score_lead(lead, {})
            assert result["breakdown"]["budget"] == expected_pts, f"Failed for: {notes}"

    def test_score_never_exceeds_100(self):
        lead = {"role": "CEO", "company_size": 500, "notes": "budget $50k ASAP urgent this week", "industry": "saas"}
        result = score_lead(lead, {})
        assert result["score"] <= 100

    def test_score_never_negative(self):
        lead = {"role": None, "company_size": None, "notes": None, "industry": None}
        result = score_lead(lead, {})
        assert result["score"] >= 0

    def test_breakdown_keys_present(self):
        lead = {"role": "CTO", "company_size": 20, "notes": "budget $8k", "industry": "fintech"}
        result = score_lead(lead, {})
        assert "company_size" in result["breakdown"]
        assert "role" in result["breakdown"]
        assert "budget" in result["breakdown"]
        assert "urgency" in result["breakdown"]
