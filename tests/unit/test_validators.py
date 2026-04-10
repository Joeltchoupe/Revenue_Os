"""
tests/test_validators.py
Unit tests for LLM output validators.
Run: pytest tests/test_validators.py -v
"""
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'python-services'))

from validators.llm_output import (
    validate_email_output,
    validate_deal_analysis,
    validate_treasury_explanation,
    validate_brief_output,
    validate_next_best_action
)


# ─── Email Validation ────────────────────────────────────

class TestEmailValidation:

    def test_valid_email(self):
        raw = "Subject: Quick question about your pipeline\n\nHey Sarah, most logistics companies at your stage struggle with deal velocity after demos. We helped a 20-person logistics firm cut that cycle by 40%. Worth a 15-min chat this week?"
        result = validate_email_output(raw)
        assert result.valid is True
        assert "Subject:" in result.output

    def test_empty_output_fails(self):
        assert validate_email_output("").valid is False
        assert validate_email_output(None).valid is False

    def test_missing_subject_fails(self):
        raw = "Hey there, just following up..."
        result = validate_email_output(raw)
        assert result.valid is False
        assert any("subject" in e.lower() for e in result.errors)

    def test_body_too_short_fails(self):
        raw = "Subject: Hi\n\nHello."
        result = validate_email_output(raw)
        assert result.valid is False

    def test_unfilled_placeholder_fails(self):
        raw = "Subject: For [Company Name]\n\nHey [LEAD_NAME], I wanted to reach out about [PAIN_POINT]."
        result = validate_email_output(raw)
        assert result.valid is False
        assert any("placeholder" in e.lower() for e in result.errors)

    def test_spam_word_generates_warning(self):
        raw = "Subject: Growth opportunity\n\nHey there, we have a synergistic solution that leverages cutting-edge technology to revolutionize your pipeline. Want to chat?"
        result = validate_email_output(raw)
        # May still be valid but should have warnings
        assert len(result.warnings) > 0

    def test_lead_name_mismatch_warning(self):
        raw = "Subject: Quick question\n\nHey John, I noticed you work in fintech and wanted to connect about scaling your sales ops. Would Tuesday work for a quick call?"
        result = validate_email_output(raw, lead_context={"name": "Marie Dupont"})
        # Name not in email — should warn
        assert any("name" in w.lower() for w in result.warnings)


# ─── Deal Analysis Validation ────────────────────────────

class TestDealAnalysisValidation:

    def test_valid_analysis(self):
        raw = """DIAGNOSIS:
The deal has been stuck at the proposal stage for 3 weeks, suggesting budget approval is pending on their end.

BLOCKING_FACTOR: BUDGET

ACTION:
Send an email to Ahmed on Tuesday offering a quarterly payment option to reduce the upfront commitment.

RISK_IF_NO_ACTION:
Without contact this week, this deal likely dies by end of quarter as the contact moves on."""
        result = validate_deal_analysis(raw)
        assert result.valid is True

    def test_missing_sections_fails(self):
        raw = "This deal needs a follow-up. Contact them soon."
        result = validate_deal_analysis(raw)
        assert result.valid is False
        assert any("DIAGNOSIS" in e for e in result.errors)

    def test_generic_action_generates_warning(self):
        raw = """DIAGNOSIS: Deal has been quiet.
ACTION: Follow up with the contact and check in.
BLOCKING_FACTOR: UNKNOWN"""
        result = validate_deal_analysis(raw)
        # Valid structure but generic action should warn
        if result.valid:
            assert any("generic" in w.lower() for w in result.warnings)

    def test_empty_fails(self):
        assert validate_deal_analysis("").valid is False


# ─── Treasury Explanation Validation ─────────────────────

class TestTreasuryValidation:

    GOOD_SNAPSHOT = {"runway_months": 4.5, "burn_rate": 15000}

    def test_valid_explanation(self):
        raw = """SITUATION:
Your runway is 4.5 months, down from 6.2 last month, primarily because Q2 hiring added $8,000 to monthly burn.

RISK:
At this burn rate of $15,000/month, you exhaust reserves by September unless revenue accelerates.

ACTION:
1. Pause the junior hire planned for July → +1.2 months runway
2. Close the 3 deals in final stage (combined $42k) → +2.8 months if all close
3. Reduce ad spend by 25% temporarily → +0.4 months"""
        result = validate_treasury_explanation(raw, self.GOOD_SNAPSHOT)
        assert result.valid is True

    def test_missing_sections_fails(self):
        raw = "Things look concerning. You should spend less."
        result = validate_treasury_explanation(raw, self.GOOD_SNAPSHOT)
        assert result.valid is False

    def test_hallucinated_runway_fails(self):
        """LLM says 25 months when actual is 4.5 — should fail"""
        raw = """SITUATION:
Your runway is excellent at 25 months, showing strong financial health.

RISK:
No immediate risk with this trajectory.

ACTION:
1. Invest in growth → +5 months"""
        result = validate_treasury_explanation(raw, self.GOOD_SNAPSHOT)
        assert result.valid is False
        assert any("hallucination" in e.lower() or "differs" in e.lower() for e in result.errors)


# ─── Brief Validation ────────────────────────────────────

class TestBriefValidation:

    def test_valid_brief(self):
        raw = """[TREASURY] Runway dropped to 4.5 months → burn increased 18% from new hires → pause non-critical recruiting immediately
[PIPELINE] 3 deals stuck >14 days, combined $47k → slow close cycle hurting Q3 → call Ahmed at TechCorp today
[LEADS] 8 hot leads this week, 0 contacted → missed revenue window → activate email sequence for top 3 now"""
        result = validate_brief_output(raw)
        assert result.valid is True

    def test_empty_fails(self):
        assert validate_brief_output("").valid is False

    def test_template_variables_fail(self):
        raw = "[TREASURY] Runway is {{runway_months}} months → {{alert_level}} → {{action}}"
        result = validate_brief_output(raw)
        assert result.valid is False


# ─── Next Best Action Validation ─────────────────────────

class TestNBAValidation:

    def test_valid_nba(self):
        raw = """ACTION_TYPE: email
CHANNEL: email
MESSAGE_ANGLE: Share the Acme case study showing 35% pipeline velocity improvement — directly relevant to their logistics ops
URGENCY: HIGH
RATIONALE: Deal has been silent 18 days and contact expressed interest in efficiency gains during last call"""
        result = validate_next_best_action(raw)
        assert result.valid is True

    def test_missing_fields_fails(self):
        raw = "Send them an email about our product features."
        result = validate_next_best_action(raw)
        assert result.valid is False
