"""
integrations/base.py
Unified integration facade — all connectors normalize to standard objects.
Every integration MUST inherit from BaseIntegration and implement the abstract methods.
"""

import os
import time
import logging
from abc import ABC, abstractmethod
from typing import Any, Optional
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class IntegrationStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNCONFIGURED = "unconfigured"


@dataclass
class NormalizedLead:
    id: str
    tenant_id: str
    name: str
    email: str
    company: str
    role: str = ""
    phone: str = ""
    industry: str = ""
    notes: str = ""
    source: str = ""
    company_size: Optional[int] = None
    crm_id: Optional[str] = None
    last_activity_at: Optional[str] = None
    raw: dict = field(default_factory=dict)


@dataclass
class NormalizedDeal:
    id: str
    tenant_id: str
    crm_id: str
    name: str
    stage: str
    amount: float
    probability: float
    close_date: Optional[str]
    owner_email: str = ""
    last_activity_at: Optional[str] = None
    stage_entered_at: Optional[str] = None
    notes: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class NormalizedTransaction:
    id: str
    tenant_id: str
    date: str
    amount: float          # negative = expense, positive = income
    description: str
    category: str
    source: str            # 'plaid' | 'stripe' | 'manual'
    raw: dict = field(default_factory=dict)


@dataclass
class IntegrationHealth:
    name: str
    status: IntegrationStatus
    latency_ms: Optional[int] = None
    error: Optional[str] = None
    last_checked: Optional[str] = None


class IntegrationError(Exception):
    """Raised when an integration call fails unrecoverably."""
    def __init__(self, integration: str, operation: str, message: str, retryable: bool = True):
        self.integration = integration
        self.operation = operation
        self.retryable = retryable
        super().__init__(f"[{integration}:{operation}] {message}")


class BaseIntegration(ABC):
    """
    All integrations inherit from this.
    Provides: retry logic, health check, logging, error normalization.
    """

    MAX_RETRIES = 3
    RETRY_DELAYS = [1, 5, 15]  # seconds

    def __init__(self, tenant_id: str, config: dict):
        self.tenant_id = tenant_id
        self.config = config
        self.name = self.__class__.__name__

    def call_with_retry(self, fn, *args, **kwargs) -> Any:
        """Execute fn with exponential backoff. Non-retryable errors bubble immediately."""
        last_error = None
        for attempt in range(self.MAX_RETRIES):
            try:
                start = time.time()
                result = fn(*args, **kwargs)
                elapsed = int((time.time() - start) * 1000)
                logger.debug(f"{self.name}: call succeeded in {elapsed}ms (attempt {attempt+1})")
                return result
            except IntegrationError as e:
                if not e.retryable:
                    logger.error(f"{self.name}: non-retryable error — {e}")
                    raise
                last_error = e
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.RETRY_DELAYS[attempt]
                    logger.warning(f"{self.name}: attempt {attempt+1} failed, retrying in {delay}s — {e}")
                    time.sleep(delay)
            except Exception as e:
                last_error = IntegrationError(self.name, "unknown", str(e), retryable=True)
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.RETRY_DELAYS[attempt]
                    logger.warning(f"{self.name}: unexpected error on attempt {attempt+1}, retrying in {delay}s — {e}")
                    time.sleep(delay)

        logger.error(f"{self.name}: all {self.MAX_RETRIES} attempts failed")
        raise last_error

    @abstractmethod
    def health_check(self) -> IntegrationHealth:
        pass

    @abstractmethod
    def is_configured(self) -> bool:
        pass
