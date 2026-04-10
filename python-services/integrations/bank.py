"""
integrations/bank.py
Bank / financial data layer — Plaid, Stripe.
Returns NormalizedTransaction objects regardless of source.
"""

import requests
import logging
from typing import Optional
from datetime import datetime, timedelta, timezone

from .base import (
    BaseIntegration, IntegrationError, IntegrationHealth,
    IntegrationStatus, NormalizedTransaction
)

logger = logging.getLogger(__name__)


def get_bank(tenant_id: str, config: dict):
    """Factory — returns the right bank connector."""
    provider = config.get("bank_provider", "").lower()
    connectors = {
        "plaid":  PlaidBank,
        "stripe": StripeBank,
        "manual": ManualBank,
    }
    cls = connectors.get(provider)
    if cls is None:
        logger.warning(f"Unknown bank provider '{provider}', falling back to ManualBank")
        return ManualBank(tenant_id, config)
    return cls(tenant_id, config)


# ─────────────────────────────────────────────
# PLAID
# ─────────────────────────────────────────────

class PlaidBank(BaseIntegration):
    ENV_URLS = {
        "sandbox":     "https://sandbox.plaid.com",
        "development": "https://development.plaid.com",
        "production":  "https://production.plaid.com",
    }

    def __init__(self, tenant_id: str, config: dict):
        super().__init__(tenant_id, config)
        self.client_id    = config.get("plaid_client_id", "")
        self.secret       = config.get("plaid_secret", "")
        self.access_token = config.get("plaid_access_token", "")
        env               = config.get("plaid_environment", "sandbox")
        self.base         = self.ENV_URLS.get(env, self.ENV_URLS["sandbox"])

    def is_configured(self) -> bool:
        return bool(self.client_id and self.secret and self.access_token)

    def health_check(self) -> IntegrationHealth:
        try:
            result = self._post("/accounts/balance/get", {})
            if "accounts" in result:
                return IntegrationHealth("plaid", IntegrationStatus.HEALTHY)
            return IntegrationHealth("plaid", IntegrationStatus.DEGRADED, error="No accounts returned")
        except IntegrationError as e:
            return IntegrationHealth("plaid", IntegrationStatus.DOWN, error=str(e))

    def _post(self, path: str, body: dict) -> dict:
        payload = {
            "client_id": self.client_id,
            "secret": self.secret,
            "access_token": self.access_token,
            **body
        }
        try:
            r = requests.post(f"{self.base}{path}", json=payload, timeout=20)
            data = r.json()

            # Plaid returns errors in the body with 200 status
            if "error_code" in data:
                code = data["error_code"]
                non_retryable = ["INVALID_ACCESS_TOKEN", "ITEM_LOGIN_REQUIRED", "INVALID_API_KEYS"]
                retryable = code not in non_retryable
                raise IntegrationError("plaid", path, f"{code}: {data.get('error_message','')}", retryable=retryable)

            return data
        except IntegrationError:
            raise
        except requests.exceptions.Timeout:
            raise IntegrationError("plaid", path, "Request timed out", retryable=True)
        except Exception as e:
            raise IntegrationError("plaid", path, str(e))

    def get_balance(self) -> dict:
        """Returns {total_cash, accounts: [{name, balance, type}]}"""
        def _fetch():
            return self._post("/accounts/balance/get", {})

        data = self.call_with_retry(_fetch)
        accounts = []
        total = 0.0
        for acc in data.get("accounts", []):
            bal = acc.get("balances", {}).get("current", 0) or 0
            accounts.append({
                "name": acc.get("name", ""),
                "type": acc.get("type", ""),
                "balance": float(bal)
            })
            if acc.get("type") in ("depository", "checking", "savings"):
                total += float(bal)
        return {"total_cash": round(total, 2), "accounts": accounts}

    def get_transactions(self, days: int = 90) -> list[NormalizedTransaction]:
        end_date   = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=days)

        def _fetch():
            return self._post("/transactions/get", {
                "start_date": start_date.isoformat(),
                "end_date":   end_date.isoformat(),
                "count": 500
            })

        data = self.call_with_retry(_fetch)
        txs = []
        for item in data.get("transactions", []):
            # Plaid: positive amount = expense, negative = income/credit
            # We flip: negative = expense, positive = income
            amount = -float(item.get("amount", 0))
            category_list = item.get("category") or ["uncategorized"]
            category = "_".join(category_list).lower()[:50]

            txs.append(NormalizedTransaction(
                id=f"plaid_{item['transaction_id']}",
                tenant_id=self.tenant_id,
                date=item.get("date", ""),
                amount=round(amount, 2),
                description=item.get("name", ""),
                category=self._simplify_category(category_list),
                source="plaid",
                raw=item
            ))
        return txs

    @staticmethod
    def _simplify_category(cats: list) -> str:
        """Map Plaid category hierarchy to our simple categories."""
        if not cats:
            return "other"
        top = (cats[0] or "").lower()
        mapping = {
            "transfer": "transfer",
            "payment":  "payment",
            "payroll":  "payroll",
            "service":  "subscription",
            "shops":    "purchase",
            "food":     "food",
            "travel":   "travel",
            "utilities": "subscription",
        }
        for key, val in mapping.items():
            if key in top:
                return val
        return "other"


# ─────────────────────────────────────────────
# STRIPE
# ─────────────────────────────────────────────

class StripeBank(BaseIntegration):
    BASE = "https://api.stripe.com/v1"

    def __init__(self, tenant_id: str, config: dict):
        super().__init__(tenant_id, config)
        self.secret_key = config.get("stripe_secret_key", "")
        self.headers = {"Authorization": f"Bearer {self.secret_key}"}

    def is_configured(self) -> bool:
        return bool(self.secret_key)

    def health_check(self) -> IntegrationHealth:
        try:
            r = requests.get(f"{self.BASE}/balance", headers=self.headers, timeout=5)
            if r.status_code == 401:
                return IntegrationHealth("stripe", IntegrationStatus.DOWN, error="Invalid API key (401)")
            r.raise_for_status()
            return IntegrationHealth("stripe", IntegrationStatus.HEALTHY)
        except Exception as e:
            return IntegrationHealth("stripe", IntegrationStatus.DOWN, error=str(e))

    def _get(self, path: str, params: dict = None) -> dict:
        try:
            r = requests.get(f"{self.BASE}{path}", headers=self.headers, params=params or {}, timeout=15)
            if r.status_code == 401:
                raise IntegrationError("stripe", path, "Invalid API key", retryable=False)
            if r.status_code == 429:
                raise IntegrationError("stripe", path, "Rate limit", retryable=True)
            r.raise_for_status()
            return r.json()
        except IntegrationError:
            raise
        except Exception as e:
            raise IntegrationError("stripe", path, str(e))

    def get_balance(self) -> dict:
        def _fetch():
            return self._get("/balance")

        data = self.call_with_retry(_fetch)
        available = sum(
            b["amount"] / 100
            for b in data.get("available", [])
            if b.get("currency") == "usd"
        )
        return {"total_cash": round(available, 2), "accounts": [{"name": "Stripe Available", "balance": available}]}

    def get_transactions(self, days: int = 90) -> list[NormalizedTransaction]:
        since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())

        def _fetch():
            return self._get("/balance/history", {"limit": 100, "created[gte]": since})

        data = self.call_with_retry(_fetch)
        txs = []
        for item in data.get("data", []):
            # Stripe amount in cents
            amount = item.get("amount", 0) / 100
            if item.get("type") in ("payout", "transfer"):
                amount = -abs(amount)  # outflow

            txs.append(NormalizedTransaction(
                id=f"stripe_{item['id']}",
                tenant_id=self.tenant_id,
                date=datetime.fromtimestamp(item["created"], tz=timezone.utc).date().isoformat(),
                amount=round(amount, 2),
                description=item.get("description") or item.get("type", ""),
                category=item.get("type", "other"),
                source="stripe",
                raw=item
            ))
        return txs


# ─────────────────────────────────────────────
# MANUAL (fallback — client enters data manually)
# ─────────────────────────────────────────────

class ManualBank(BaseIntegration):
    """Used when no bank API is configured. Data comes from manual Supabase inserts."""

    def is_configured(self) -> bool:
        return True  # Always "configured" — just uses existing DB data

    def health_check(self) -> IntegrationHealth:
        return IntegrationHealth("manual", IntegrationStatus.HEALTHY,
                                 error="Manual mode: no live bank connection")

    def get_balance(self) -> dict:
        logger.warning(f"[{self.tenant_id}] Manual bank mode — returning 0 balance. Client must input manually.")
        return {"total_cash": 0.0, "accounts": [], "manual_mode": True}

    def get_transactions(self, days: int = 90) -> list[NormalizedTransaction]:
        logger.warning(f"[{self.tenant_id}] Manual bank mode — no transactions fetched from API")
        return []
