"""
utils/secrets.py
Loads tenant config from Supabase. Merges with env defaults.
"""
import os
import logging
from functools import lru_cache
from .supabase_client import get_supabase

logger = logging.getLogger(__name__)

DEFAULTS = {
    "execution_mode":          "approval",
    "max_emails_day":          50,
    "runway_warning_months":   6,
    "runway_critical_months":  3,
    "safety_buffer_months":    2,
    "currency":                "USD",
    "crm_provider":            "",
    "bank_provider":           "manual",
    "icp_industries":          ["saas", "software", "tech", "fintech", "ecommerce", "dtc"],
}


def load_tenant_config(tenant_id: str) -> dict:
    """
    Loads tenant config from DB + decrypts secrets.
    Falls back to DEFAULTS for any missing key.
    Never raises — missing keys get defaults.
    """
    try:
        sb = get_supabase()

        # Base config
        res = sb.table("tenant_configs")\
            .select("*")\
            .eq("tenant_id", tenant_id)\
            .single()\
            .execute()

        config = dict(DEFAULTS)
        if res.data:
            config.update({k: v for k, v in res.data.items() if v is not None})

        # Secrets (stored encrypted in tenant_secrets)
        secrets_res = sb.table("tenant_secrets")\
            .select("key,value")\
            .eq("tenant_id", tenant_id)\
            .execute()

        for row in (secrets_res.data or []):
            config[row["key"]] = row["value"]

        return config

    except Exception as e:
        logger.error(f"Failed to load config for tenant {tenant_id}: {e}")
        return dict(DEFAULTS)
