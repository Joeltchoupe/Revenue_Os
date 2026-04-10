"""
integrations/__init__.py
"""
from .base import (
    BaseIntegration, IntegrationError, IntegrationHealth,
    IntegrationStatus, NormalizedLead, NormalizedDeal, NormalizedTransaction
)
from .crm import get_crm, HubSpotCRM, PipedriveCRM, ZohoCRM, SalesforceCRM
from .bank import get_bank, PlaidBank, StripeBank, ManualBank
from .messaging import SlackMessaging, GmailMessaging, ResendMessaging

__all__ = [
    "get_crm", "get_bank",
    "HubSpotCRM", "PipedriveCRM", "ZohoCRM", "SalesforceCRM",
    "PlaidBank", "StripeBank", "ManualBank",
    "SlackMessaging", "GmailMessaging", "ResendMessaging",
    "IntegrationError", "IntegrationHealth", "IntegrationStatus",
    "NormalizedLead", "NormalizedDeal", "NormalizedTransaction"
]
