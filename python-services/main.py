"""
main.py
FastAPI service — exposes HTTP endpoints called by n8n workflows.
All business logic lives in domain modules; this is just routing.
"""

import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

from treasury import calculate_treasury, score_lead
from validators.llm_output import (
    validate_email_output, validate_deal_analysis,
    validate_treasury_explanation, validate_brief_output,
    validate_next_best_action
)
from integrations import get_crm, get_bank, IntegrationError
from utils.supabase_client import get_supabase
from utils.secrets import load_tenant_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

SERVICE_KEY = os.environ.get("SERVICE_SECRET_KEY", "")


# ── Auth ──────────────────────────────────────────────────────────

def verify_service_key(x_service_key: str = Header(None)):
    if not SERVICE_KEY:
        logger.warning("SERVICE_SECRET_KEY not set — running unprotected")
        return
    if x_service_key != SERVICE_KEY:
        raise HTTPException(status_code=401, detail="Invalid service key")


# ── App ───────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Revenue OS Python Service starting")
    yield
    logger.info("Revenue OS Python Service stopping")


app = FastAPI(title="Revenue OS — Python Services", version="1.0.0", lifespan=lifespan)


# ── Health ────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "revenue-os-python"}


# ── Treasury ──────────────────────────────────────────────────────

class TreasuryRequest(BaseModel):
    tenant_id: str
    bank_balance: Optional[float] = None  # if None, fetched live
    use_cached_transactions: bool = False


@app.post("/treasury/snapshot", dependencies=[Depends(verify_service_key)])
async def treasury_snapshot(req: TreasuryRequest):
    try:
        config = load_tenant_config(req.tenant_id)
        supabase = get_supabase()

        # Get bank balance
        if req.bank_balance is not None:
            cash = req.bank_balance
            freshness = "manual"
        else:
            bank = get_bank(req.tenant_id, config)
            if not bank.is_configured():
                cash = 0.0
                freshness = "manual"
                logger.warning(f"[{req.tenant_id}] Bank not configured — using 0 balance")
            else:
                try:
                    balance_data = bank.get_balance()
                    cash = balance_data["total_cash"]
                    freshness = "live"
                except IntegrationError as e:
                    # Fallback: use last known balance from DB
                    last = supabase.table("treasury_snapshots")\
                        .select("cash")\
                        .eq("tenant_id", req.tenant_id)\
                        .order("calculated_at", desc=True)\
                        .limit(1).execute()
                    cash = last.data[0]["cash"] if last.data else 0.0
                    freshness = "stale"
                    logger.warning(f"[{req.tenant_id}] Bank API failed ({e}), using last known balance: {cash}")

        # Get transactions
        txs_res = supabase.table("transactions")\
            .select("*")\
            .eq("tenant_id", req.tenant_id)\
            .gte("date", (
                __import__("datetime").datetime.utcnow() -
                __import__("datetime").timedelta(days=90)
            ).date().isoformat())\
            .execute()
        transactions = txs_res.data or []

        # Get pipeline deals
        deals_res = supabase.table("deals")\
            .select("amount,probability,close_date")\
            .eq("tenant_id", req.tenant_id)\
            .eq("status", "open")\
            .execute()
        deals = deals_res.data or []

        snapshot = calculate_treasury(
            tenant_id=req.tenant_id,
            bank_balance=cash,
            transactions=transactions,
            pipeline_deals=deals,
            tenant_config=config,
            data_freshness=freshness
        )

        # Persist snapshot
        supabase.table("treasury_snapshots").insert({
            "tenant_id":         snapshot.tenant_id,
            "cash":              snapshot.cash,
            "burn_rate":         snapshot.burn_rate,
            "projected_revenue": snapshot.projected_revenue,
            "runway_months":     snapshot.runway_months,
            "alert_level":       snapshot.alert_level,
            "safe_budget":       snapshot.safe_budget,
            "currency":          snapshot.currency,
            "data_freshness":    snapshot.data_freshness,
            "warnings":          snapshot.warnings,
            "calculated_at":     snapshot.calculated_at
        }).execute()

        return snapshot.__dict__

    except Exception as e:
        logger.error(f"[{req.tenant_id}] Treasury snapshot failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Treasury calculation failed: {str(e)}")


# ── Lead Scoring ──────────────────────────────────────────────────

class LeadScoreRequest(BaseModel):
    tenant_id: str
    lead: dict


@app.post("/leads/score", dependencies=[Depends(verify_service_key)])
async def score_lead_endpoint(req: LeadScoreRequest):
    try:
        config = load_tenant_config(req.tenant_id)
        result = score_lead(req.lead, config)
        return result
    except Exception as e:
        logger.error(f"Lead scoring failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Validate LLM Outputs ──────────────────────────────────────────

class ValidateRequest(BaseModel):
    tenant_id: str
    output_type: str    # email | deal_analysis | treasury | brief | next_best_action
    raw_output: str
    context: Optional[dict] = None


@app.post("/validate/llm-output", dependencies=[Depends(verify_service_key)])
async def validate_output(req: ValidateRequest):
    validators = {
        "email":            lambda: validate_email_output(req.raw_output, req.context),
        "deal_analysis":    lambda: validate_deal_analysis(req.raw_output),
        "treasury":         lambda: validate_treasury_explanation(req.raw_output, req.context or {}),
        "brief":            lambda: validate_brief_output(req.raw_output),
        "next_best_action": lambda: validate_next_best_action(req.raw_output),
    }

    fn = validators.get(req.output_type)
    if not fn:
        raise HTTPException(status_code=400, detail=f"Unknown output_type: {req.output_type}")

    result = fn()
    return {
        "valid":          result.valid,
        "output":         result.output,
        "warnings":       result.warnings,
        "errors":         result.errors,
        "fallback_used":  result.fallback_used
    }


# ── Integration Health Check ──────────────────────────────────────

@app.get("/health/integrations/{tenant_id}", dependencies=[Depends(verify_service_key)])
async def check_integrations(tenant_id: str):
    config = load_tenant_config(tenant_id)
    results = {}

    try:
        crm = get_crm(tenant_id, config)
        results["crm"] = crm.health_check().__dict__
    except Exception as e:
        results["crm"] = {"status": "error", "error": str(e)}

    try:
        bank = get_bank(tenant_id, config)
        results["bank"] = bank.health_check().__dict__
    except Exception as e:
        results["bank"] = {"status": "error", "error": str(e)}

    return {"tenant_id": tenant_id, "integrations": results}


# ── CRM Sync ──────────────────────────────────────────────────────

class SyncRequest(BaseModel):
    tenant_id: str
    resource: str   # "contacts" | "deals"
    limit: int = 100


@app.post("/crm/sync", dependencies=[Depends(verify_service_key)])
async def sync_crm(req: SyncRequest):
    try:
        config  = load_tenant_config(req.tenant_id)
        crm     = get_crm(req.tenant_id, config)
        supabase = get_supabase()

        if not crm.is_configured():
            raise HTTPException(status_code=400, detail="CRM not configured for this tenant")

        if req.resource == "contacts":
            items = crm.get_contacts(limit=req.limit)
            rows  = [
                {
                    "tenant_id":     req.tenant_id,
                    "crm_id":        item.crm_id,
                    "name":          item.name,
                    "email":         item.email,
                    "company":       item.company,
                    "role":          item.role,
                    "phone":         item.phone,
                    "industry":      item.industry,
                    "company_size":  item.company_size,
                    "last_activity_at": item.last_activity_at,
                    "source":        config.get("crm_provider", "crm"),
                    "raw":           item.raw
                }
                for item in items
            ]
            # Upsert by (tenant_id, crm_id)
            supabase.table("leads").upsert(rows, on_conflict="tenant_id,crm_id").execute()
            return {"synced": len(rows), "resource": "contacts"}

        elif req.resource == "deals":
            items = crm.get_deals(limit=req.limit)
            rows  = [
                {
                    "tenant_id":        req.tenant_id,
                    "crm_id":           item.crm_id,
                    "name":             item.name,
                    "stage":            item.stage,
                    "amount":           item.amount,
                    "probability":      item.probability,
                    "close_date":       item.close_date,
                    "last_activity_at": item.last_activity_at,
                    "stage_entered_at": item.stage_entered_at,
                    "status":           "open",
                    "raw":              item.raw
                }
                for item in items
            ]
            supabase.table("deals").upsert(rows, on_conflict="tenant_id,crm_id").execute()
            return {"synced": len(rows), "resource": "deals"}

        else:
            raise HTTPException(status_code=400, detail=f"Unknown resource: {req.resource}")

    except IntegrationError as e:
        logger.error(f"CRM sync failed [{req.tenant_id}]: {e}")
        raise HTTPException(status_code=502, detail=f"CRM integration error: {str(e)}")
    except Exception as e:
        logger.error(f"CRM sync unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
