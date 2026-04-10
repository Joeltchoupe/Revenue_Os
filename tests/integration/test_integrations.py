"""
tests/integration/test_integrations.py
Integration tests — require real credentials via .env.test
Run: pytest tests/integration/ -v --env=test
DO NOT run these in CI without secrets. They hit live APIs.
"""
import os
import pytest
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'python-services'))

from integrations.crm import HubSpotCRM, PipedriveCRM, ZohoCRM, SalesforceCRM, get_crm
from integrations.bank import PlaidBank, StripeBank, ManualBank, get_bank
from integrations.messaging import SlackMessaging, ResendMessaging
from integrations.base import IntegrationError, IntegrationStatus


# ─── CRM Factory ─────────────────────────────────────────

class TestCRMFactory:

    def test_get_crm_hubspot(self):
        crm = get_crm("t1", {"crm_provider": "hubspot", "hubspot_api_key": "test"})
        assert isinstance(crm, HubSpotCRM)

    def test_get_crm_pipedrive(self):
        crm = get_crm("t1", {"crm_provider": "pipedrive", "pipedrive_api_key": "test"})
        assert isinstance(crm, PipedriveCRM)

    def test_get_crm_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown CRM provider"):
            get_crm("t1", {"crm_provider": "unknown_crm"})

    def test_get_crm_empty_raises(self):
        with pytest.raises(ValueError):
            get_crm("t1", {})


# ─── Bank Factory ─────────────────────────────────────────

class TestBankFactory:

    def test_get_bank_plaid(self):
        bank = get_bank("t1", {"bank_provider": "plaid"})
        assert isinstance(bank, PlaidBank)

    def test_get_bank_stripe(self):
        bank = get_bank("t1", {"bank_provider": "stripe"})
        assert isinstance(bank, StripeBank)

    def test_get_bank_manual_fallback(self):
        """Unknown provider falls back to ManualBank"""
        bank = get_bank("t1", {"bank_provider": "unknown"})
        assert isinstance(bank, ManualBank)

    def test_manual_bank_always_configured(self):
        bank = ManualBank("t1", {})
        assert bank.is_configured() is True

    def test_manual_bank_returns_zero_balance(self):
        bank = ManualBank("t1", {})
        result = bank.get_balance()
        assert result["total_cash"] == 0.0
        assert result.get("manual_mode") is True


# ─── HubSpot with mocked HTTP ────────────────────────────

class TestHubSpotMocked:

    @patch("requests.get")
    def test_get_contacts_success(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "results": [{
                    "id": "123",
                    "properties": {
                        "firstname": "Jean",
                        "lastname": "Dupont",
                        "email": "jean@acme.com",
                        "company": "Acme",
                        "jobtitle": "CEO",
                        "phone": "+33123456789",
                        "industry": "SaaS",
                        "num_employees": "25",
                        "notes_last_updated": "2024-01-15"
                    }
                }]
            },
            elapsed=MagicMock(total_seconds=lambda: 0.1)
        )
        crm = HubSpotCRM("t1", {"hubspot_api_key": "test_key"})
        leads = crm.get_contacts(limit=10)
        assert len(leads) == 1
        assert leads[0].name == "Jean Dupont"
        assert leads[0].email == "jean@acme.com"
        assert leads[0].company == "Acme"
        assert leads[0].company_size == 25
        assert leads[0].crm_id == "123"

    @patch("requests.get")
    def test_get_contacts_401_raises_non_retryable(self, mock_get):
        mock_get.return_value = MagicMock(status_code=401, json=lambda: {})
        crm = HubSpotCRM("t1", {"hubspot_api_key": "bad_key"})
        with pytest.raises(IntegrationError) as exc_info:
            crm.get_contacts()
        assert not exc_info.value.retryable

    @patch("requests.get")
    def test_get_contacts_429_retries(self, mock_get):
        """Rate limit errors should retry (and eventually raise after max retries)"""
        mock_get.return_value = MagicMock(status_code=429, json=lambda: {})
        crm = HubSpotCRM("t1", {"hubspot_api_key": "test_key"})
        with pytest.raises(IntegrationError) as exc_info:
            crm.get_contacts()
        assert exc_info.value.retryable
        assert mock_get.call_count == crm.MAX_RETRIES

    @patch("requests.get")
    def test_get_contacts_timeout_retries(self, mock_get):
        import requests
        mock_get.side_effect = requests.exceptions.Timeout()
        crm = HubSpotCRM("t1", {"hubspot_api_key": "test_key"})
        with pytest.raises(IntegrationError):
            crm.get_contacts()
        assert mock_get.call_count == crm.MAX_RETRIES


# ─── Plaid with mocked HTTP ──────────────────────────────

class TestPlaidMocked:

    @patch("requests.post")
    def test_get_balance_success(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "accounts": [
                    {"name": "Checking", "type": "depository", "balances": {"current": 45000.00}},
                    {"name": "Savings",  "type": "depository", "balances": {"current": 25000.00}}
                ]
            }
        )
        bank = PlaidBank("t1", {
            "plaid_client_id": "test", "plaid_secret": "test",
            "plaid_access_token": "test", "plaid_environment": "sandbox"
        })
        result = bank.get_balance()
        assert result["total_cash"] == 70000.00
        assert len(result["accounts"]) == 2

    @patch("requests.post")
    def test_plaid_error_in_body(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"error_code": "INVALID_ACCESS_TOKEN", "error_message": "Token invalid"}
        )
        bank = PlaidBank("t1", {
            "plaid_client_id": "test", "plaid_secret": "test",
            "plaid_access_token": "bad", "plaid_environment": "sandbox"
        })
        with pytest.raises(IntegrationError) as exc_info:
            bank.get_balance()
        assert not exc_info.value.retryable  # INVALID_ACCESS_TOKEN is non-retryable

    @patch("requests.post")
    def test_plaid_category_simplification(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "transactions": [
                    {"transaction_id": "tx1", "date": "2024-01-15", "amount": 500.0,
                     "name": "Ahrefs subscription", "category": ["Service", "Subscription"]}
                ]
            }
        )
        bank = PlaidBank("t1", {
            "plaid_client_id": "test", "plaid_secret": "test",
            "plaid_access_token": "test", "plaid_environment": "sandbox"
        })
        txs = bank.get_transactions(30)
        assert len(txs) == 1
        # Plaid amount 500 = expense → our format: -500
        assert txs[0].amount == -500.0
        assert txs[0].category == "subscription"


# ─── Slack Messaging ─────────────────────────────────────

class TestSlackMessaging:

    def test_not_configured_returns_false(self):
        slack = SlackMessaging("t1", {})
        result = slack.send("test message")
        assert result is False  # Should not raise, just return False

    def test_invalid_webhook_url_unconfigured(self):
        slack = SlackMessaging("t1", {"slack_webhook_url": "not-a-url"})
        health = slack.health_check()
        assert health.status == IntegrationStatus.UNCONFIGURED

    @patch("requests.post")
    def test_send_success(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, text="ok")
        slack = SlackMessaging("t1", {"slack_webhook_url": "https://hooks.slack.com/services/test"})
        result = slack.send("Test message")
        assert result is True

    @patch("requests.post")
    def test_send_failure_returns_false_not_raises(self, mock_post):
        mock_post.return_value = MagicMock(status_code=500, text="Server Error")
        slack = SlackMessaging("t1", {"slack_webhook_url": "https://hooks.slack.com/services/test"})
        result = slack.send("Test message")
        assert result is False  # Must not raise — messaging failures are non-fatal


# ─── Health Check Tests ───────────────────────────────────

class TestHealthChecks:

    def test_manual_bank_health(self):
        bank = ManualBank("t1", {})
        health = bank.health_check()
        assert health.status == IntegrationStatus.HEALTHY

    def test_unconfigured_hubspot(self):
        crm = HubSpotCRM("t1", {})
        assert crm.is_configured() is False

    def test_unconfigured_plaid(self):
        bank = PlaidBank("t1", {})
        assert bank.is_configured() is False
