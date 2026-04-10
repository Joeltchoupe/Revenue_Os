"""
integrations/crm.py
Unified CRM layer — HubSpot, Pipedrive, Zoho, Salesforce.
All return NormalizedLead / NormalizedDeal regardless of source.
"""

import os
import requests
import logging
from typing import Optional
from datetime import datetime, timezone

from .base import (
    BaseIntegration, IntegrationError, IntegrationHealth,
    IntegrationStatus, NormalizedLead, NormalizedDeal
)

logger = logging.getLogger(__name__)


def get_crm(tenant_id: str, config: dict):
    """Factory — returns the right CRM connector based on config."""
    provider = config.get("crm_provider", "").lower()
    connectors = {
        "hubspot":    HubSpotCRM,
        "pipedrive":  PipedriveCRM,
        "zoho":       ZohoCRM,
        "salesforce": SalesforceCRM,
    }
    cls = connectors.get(provider)
    if cls is None:
        raise ValueError(f"Unknown CRM provider: '{provider}'. Supported: {list(connectors.keys())}")
    return cls(tenant_id, config)


# ─────────────────────────────────────────────
# HUBSPOT
# ─────────────────────────────────────────────

class HubSpotCRM(BaseIntegration):
    BASE = "https://api.hubapi.com"

    def __init__(self, tenant_id: str, config: dict):
        super().__init__(tenant_id, config)
        self.token = config.get("hubspot_api_key", "")
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

    def is_configured(self) -> bool:
        return bool(self.token)

    def health_check(self) -> IntegrationHealth:
        try:
            r = requests.get(f"{self.BASE}/crm/v3/objects/contacts?limit=1",
                             headers=self.headers, timeout=5)
            r.raise_for_status()
            return IntegrationHealth("hubspot", IntegrationStatus.HEALTHY, latency_ms=int(r.elapsed.total_seconds()*1000))
        except Exception as e:
            return IntegrationHealth("hubspot", IntegrationStatus.DOWN, error=str(e))

    def _get(self, path: str, params: dict = None) -> dict:
        url = f"{self.BASE}{path}"
        try:
            r = requests.get(url, headers=self.headers, params=params or {}, timeout=15)
            if r.status_code == 401:
                raise IntegrationError("hubspot", path, "Invalid API key (401)", retryable=False)
            if r.status_code == 429:
                raise IntegrationError("hubspot", path, "Rate limit hit (429)", retryable=True)
            r.raise_for_status()
            return r.json()
        except IntegrationError:
            raise
        except requests.exceptions.Timeout:
            raise IntegrationError("hubspot", path, "Request timed out", retryable=True)
        except Exception as e:
            raise IntegrationError("hubspot", path, str(e), retryable=True)

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self.BASE}{path}"
        try:
            r = requests.post(url, headers=self.headers, json=body, timeout=15)
            if r.status_code == 401:
                raise IntegrationError("hubspot", path, "Invalid API key (401)", retryable=False)
            r.raise_for_status()
            return r.json()
        except IntegrationError:
            raise
        except Exception as e:
            raise IntegrationError("hubspot", path, str(e), retryable=True)

    def get_contacts(self, limit: int = 100, after: str = None) -> list[NormalizedLead]:
        params = {
            "limit": limit,
            "properties": "firstname,lastname,email,company,jobtitle,phone,industry,hs_lead_status,notes_last_updated,num_employees"
        }
        if after:
            params["after"] = after

        def _fetch():
            return self._get("/crm/v3/objects/contacts", params)

        data = self.call_with_retry(_fetch)
        leads = []
        for item in data.get("results", []):
            p = item.get("properties", {})
            leads.append(NormalizedLead(
                id=f"hs_{item['id']}",
                tenant_id=self.tenant_id,
                name=f"{p.get('firstname','') or ''} {p.get('lastname','') or ''}".strip() or "Unknown",
                email=p.get("email", ""),
                company=p.get("company", ""),
                role=p.get("jobtitle", ""),
                phone=p.get("phone", ""),
                industry=p.get("industry", ""),
                company_size=self._safe_int(p.get("num_employees")),
                crm_id=item["id"],
                last_activity_at=p.get("notes_last_updated"),
                raw=item
            ))
        return leads

    def get_deals(self, limit: int = 100) -> list[NormalizedDeal]:
        params = {
            "limit": limit,
            "properties": "dealname,dealstage,amount,closedate,hs_deal_stage_probability,hs_date_entered_dealstage,notes_last_updated"
        }

        def _fetch():
            return self._get("/crm/v3/objects/deals", params)

        data = self.call_with_retry(_fetch)
        deals = []
        for item in data.get("results", []):
            p = item.get("properties", {})
            deals.append(NormalizedDeal(
                id=f"hs_{item['id']}",
                tenant_id=self.tenant_id,
                crm_id=item["id"],
                name=p.get("dealname", "Unnamed"),
                stage=p.get("dealstage", ""),
                amount=float(p.get("amount") or 0),
                probability=float(p.get("hs_deal_stage_probability") or 0),
                close_date=p.get("closedate"),
                last_activity_at=p.get("notes_last_updated"),
                stage_entered_at=p.get("hs_date_entered_dealstage"),
                raw=item
            ))
        return deals

    def create_note(self, contact_id: str, body: str) -> dict:
        payload = {
            "properties": {"hs_note_body": body, "hs_timestamp": int(datetime.now(timezone.utc).timestamp() * 1000)},
            "associations": [{"to": {"id": contact_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]}]
        }
        return self.call_with_retry(lambda: self._post("/crm/v3/objects/notes", payload))

    @staticmethod
    def _safe_int(val) -> Optional[int]:
        try:
            return int(val) if val else None
        except (ValueError, TypeError):
            return None


# ─────────────────────────────────────────────
# PIPEDRIVE
# ─────────────────────────────────────────────

class PipedriveCRM(BaseIntegration):
    def __init__(self, tenant_id: str, config: dict):
        super().__init__(tenant_id, config)
        self.api_token = config.get("pipedrive_api_key", "")
        self.base = f"https://api.pipedrive.com/v1"

    def is_configured(self) -> bool:
        return bool(self.api_token)

    def health_check(self) -> IntegrationHealth:
        try:
            r = requests.get(f"{self.base}/persons?limit=1&api_token={self.api_token}", timeout=5)
            r.raise_for_status()
            return IntegrationHealth("pipedrive", IntegrationStatus.HEALTHY)
        except Exception as e:
            return IntegrationHealth("pipedrive", IntegrationStatus.DOWN, error=str(e))

    def _get(self, path: str, params: dict = None) -> dict:
        p = {"api_token": self.api_token, **(params or {})}
        try:
            r = requests.get(f"{self.base}{path}", params=p, timeout=15)
            if r.status_code == 401:
                raise IntegrationError("pipedrive", path, "Unauthorized (401)", retryable=False)
            if r.status_code == 429:
                raise IntegrationError("pipedrive", path, "Rate limit (429)", retryable=True)
            r.raise_for_status()
            return r.json()
        except IntegrationError:
            raise
        except Exception as e:
            raise IntegrationError("pipedrive", path, str(e))

    def get_contacts(self, limit: int = 100) -> list[NormalizedLead]:
        def _fetch():
            return self._get("/persons", {"limit": limit, "status": "open"})

        data = self.call_with_retry(_fetch)
        leads = []
        for item in (data.get("data") or []):
            email = (item.get("email") or [{}])[0].get("value", "")
            phone = (item.get("phone") or [{}])[0].get("value", "")
            leads.append(NormalizedLead(
                id=f"pd_{item['id']}",
                tenant_id=self.tenant_id,
                name=item.get("name", ""),
                email=email,
                company=item.get("org_name", ""),
                role=item.get("job_title", ""),
                phone=phone,
                crm_id=str(item["id"]),
                last_activity_at=item.get("last_activity_date"),
                raw=item
            ))
        return leads

    def get_deals(self, limit: int = 100) -> list[NormalizedDeal]:
        def _fetch():
            return self._get("/deals", {"limit": limit, "status": "open"})

        data = self.call_with_retry(_fetch)
        deals = []
        for item in (data.get("data") or []):
            deals.append(NormalizedDeal(
                id=f"pd_{item['id']}",
                tenant_id=self.tenant_id,
                crm_id=str(item["id"]),
                name=item.get("title", "Unnamed"),
                stage=item.get("stage_name", ""),
                amount=float(item.get("value") or 0),
                probability=float(item.get("probability") or 0),
                close_date=item.get("expected_close_date"),
                last_activity_at=item.get("last_activity_date"),
                raw=item
            ))
        return deals


# ─────────────────────────────────────────────
# ZOHO CRM
# ─────────────────────────────────────────────

class ZohoCRM(BaseIntegration):
    def __init__(self, tenant_id: str, config: dict):
        super().__init__(tenant_id, config)
        self.access_token = config.get("zoho_access_token", "")
        self.region = config.get("zoho_region", "com")  # com, eu, in, au
        self.base = f"https://www.zohoapis.{self.region}/crm/v3"

    def is_configured(self) -> bool:
        return bool(self.access_token)

    def health_check(self) -> IntegrationHealth:
        try:
            r = requests.get(f"{self.base}/Leads?per_page=1",
                             headers={"Authorization": f"Zoho-oauthtoken {self.access_token}"}, timeout=5)
            if r.status_code == 401:
                return IntegrationHealth("zoho", IntegrationStatus.DOWN, error="Token expired — needs refresh")
            r.raise_for_status()
            return IntegrationHealth("zoho", IntegrationStatus.HEALTHY)
        except Exception as e:
            return IntegrationHealth("zoho", IntegrationStatus.DOWN, error=str(e))

    def _get(self, path: str, params: dict = None) -> dict:
        headers = {"Authorization": f"Zoho-oauthtoken {self.access_token}"}
        try:
            r = requests.get(f"{self.base}{path}", headers=headers, params=params or {}, timeout=15)
            if r.status_code == 401:
                raise IntegrationError("zoho", path, "Token expired (401) — refresh required", retryable=False)
            if r.status_code == 429:
                raise IntegrationError("zoho", path, "Rate limit (429)", retryable=True)
            r.raise_for_status()
            return r.json()
        except IntegrationError:
            raise
        except Exception as e:
            raise IntegrationError("zoho", path, str(e))

    def get_contacts(self, limit: int = 100) -> list[NormalizedLead]:
        def _fetch():
            return self._get("/Leads", {"per_page": min(limit, 200)})

        data = self.call_with_retry(_fetch)
        leads = []
        for item in (data.get("data") or []):
            leads.append(NormalizedLead(
                id=f"zoho_{item['id']}",
                tenant_id=self.tenant_id,
                name=f"{item.get('First_Name','')} {item.get('Last_Name','')}".strip(),
                email=item.get("Email", ""),
                company=item.get("Company", ""),
                role=item.get("Title", ""),
                phone=item.get("Phone", ""),
                industry=item.get("Industry", ""),
                crm_id=item["id"],
                last_activity_at=item.get("Last_Activity_Time"),
                raw=item
            ))
        return leads

    def get_deals(self, limit: int = 100) -> list[NormalizedDeal]:
        def _fetch():
            return self._get("/Deals", {"per_page": min(limit, 200)})

        data = self.call_with_retry(_fetch)
        deals = []
        for item in (data.get("data") or []):
            deals.append(NormalizedDeal(
                id=f"zoho_{item['id']}",
                tenant_id=self.tenant_id,
                crm_id=item["id"],
                name=item.get("Deal_Name", "Unnamed"),
                stage=item.get("Stage", ""),
                amount=float(item.get("Amount") or 0),
                probability=float(item.get("Probability") or 0),
                close_date=item.get("Closing_Date"),
                last_activity_at=item.get("Last_Activity_Time"),
                raw=item
            ))
        return deals


# ─────────────────────────────────────────────
# SALESFORCE
# ─────────────────────────────────────────────

class SalesforceCRM(BaseIntegration):
    def __init__(self, tenant_id: str, config: dict):
        super().__init__(tenant_id, config)
        self.instance_url = config.get("salesforce_instance_url", "")
        self.access_token = config.get("salesforce_access_token", "")
        self.api_version = "v58.0"

    def is_configured(self) -> bool:
        return bool(self.instance_url and self.access_token)

    def health_check(self) -> IntegrationHealth:
        try:
            r = requests.get(
                f"{self.instance_url}/services/data/{self.api_version}/sobjects/",
                headers={"Authorization": f"Bearer {self.access_token}"}, timeout=5
            )
            if r.status_code == 401:
                return IntegrationHealth("salesforce", IntegrationStatus.DOWN, error="Token expired")
            r.raise_for_status()
            return IntegrationHealth("salesforce", IntegrationStatus.HEALTHY)
        except Exception as e:
            return IntegrationHealth("salesforce", IntegrationStatus.DOWN, error=str(e))

    def _query(self, soql: str) -> dict:
        headers = {"Authorization": f"Bearer {self.access_token}"}
        url = f"{self.instance_url}/services/data/{self.api_version}/query"
        try:
            r = requests.get(url, headers=headers, params={"q": soql}, timeout=20)
            if r.status_code == 401:
                raise IntegrationError("salesforce", "query", "Token expired (401)", retryable=False)
            r.raise_for_status()
            return r.json()
        except IntegrationError:
            raise
        except Exception as e:
            raise IntegrationError("salesforce", "query", str(e))

    def get_contacts(self, limit: int = 100) -> list[NormalizedLead]:
        soql = f"SELECT Id, FirstName, LastName, Email, Company, Title, Phone, Industry, LastActivityDate, NumberOfEmployees FROM Contact LIMIT {limit}"

        def _fetch():
            return self._query(soql)

        data = self.call_with_retry(_fetch)
        leads = []
        for item in data.get("records", []):
            leads.append(NormalizedLead(
                id=f"sf_{item['Id']}",
                tenant_id=self.tenant_id,
                name=f"{item.get('FirstName','')} {item.get('LastName','')}".strip(),
                email=item.get("Email", ""),
                company=item.get("Company", ""),
                role=item.get("Title", ""),
                phone=item.get("Phone", ""),
                industry=item.get("Industry", ""),
                company_size=item.get("NumberOfEmployees"),
                crm_id=item["Id"],
                last_activity_at=item.get("LastActivityDate"),
                raw=item
            ))
        return leads

    def get_deals(self, limit: int = 100) -> list[NormalizedDeal]:
        soql = f"SELECT Id, Name, StageName, Amount, Probability, CloseDate, LastActivityDate FROM Opportunity WHERE IsClosed = false LIMIT {limit}"

        def _fetch():
            return self._query(soql)

        data = self.call_with_retry(_fetch)
        deals = []
        for item in data.get("records", []):
            deals.append(NormalizedDeal(
                id=f"sf_{item['Id']}",
                tenant_id=self.tenant_id,
                crm_id=item["Id"],
                name=item.get("Name", "Unnamed"),
                stage=item.get("StageName", ""),
                amount=float(item.get("Amount") or 0),
                probability=float(item.get("Probability") or 0),
                close_date=item.get("CloseDate"),
                last_activity_at=item.get("LastActivityDate"),
                raw=item
            ))
        return deals
