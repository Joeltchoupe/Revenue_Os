"""
integrations/messaging.py
Messaging layer — Slack, Gmail, Resend.
Unified send interface regardless of channel.
"""

import smtplib
import requests
import logging
import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from .base import BaseIntegration, IntegrationError, IntegrationHealth, IntegrationStatus

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# SLACK
# ─────────────────────────────────────────────

class SlackMessaging(BaseIntegration):
    def __init__(self, tenant_id: str, config: dict):
        super().__init__(tenant_id, config)
        self.webhook_url = config.get("slack_webhook_url", "")
        self.bot_token   = config.get("slack_bot_token", "")  # optional, for interactive
        self.channel     = config.get("slack_channel", "#general")

    def is_configured(self) -> bool:
        return bool(self.webhook_url)

    def health_check(self) -> IntegrationHealth:
        # Can't truly ping webhook without sending — just validate URL format
        if not self.webhook_url.startswith("https://hooks.slack.com/"):
            return IntegrationHealth("slack", IntegrationStatus.UNCONFIGURED,
                                     error="Invalid webhook URL format")
        return IntegrationHealth("slack", IntegrationStatus.HEALTHY)

    def send(self, text: str, blocks: list = None, channel: str = None) -> bool:
        """Send a message. Returns True on success, False on failure (never raises)."""
        if not self.is_configured():
            logger.warning(f"[{self.tenant_id}] Slack not configured — message dropped: {text[:80]}")
            return False

        payload = {"text": text}
        if blocks:
            payload["blocks"] = blocks
        if channel:
            payload["channel"] = channel

        def _send():
            r = requests.post(self.webhook_url, json=payload, timeout=10)
            if r.status_code != 200:
                raise IntegrationError("slack", "send", f"HTTP {r.status_code}: {r.text}", retryable=r.status_code >= 500)
            if r.text != "ok":
                raise IntegrationError("slack", "send", f"Unexpected response: {r.text}", retryable=False)
            return True

        try:
            return self.call_with_retry(_send)
        except IntegrationError as e:
            logger.error(f"[{self.tenant_id}] Slack send failed after retries: {e}")
            return False

    def send_approval_request(self, rec_id: str, title: str, body: str,
                               approve_url: str, reject_url: str) -> bool:
        """Sends a structured approval message with Approve/Reject buttons."""
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{title}*\n{body}"}
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ Approve"},
                        "style": "primary",
                        "url": approve_url
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "❌ Reject"},
                        "style": "danger",
                        "url": reject_url
                    }
                ]
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Recommendation ID: `{rec_id}`"}]
            }
        ]
        return self.send(title, blocks=blocks)

    def send_alert(self, level: str, title: str, body: str) -> bool:
        """Send a formatted alert. level: CRITICAL | WARNING | INFO"""
        emoji = {"CRITICAL": "🚨", "WARNING": "⚠️", "INFO": "ℹ️"}.get(level, "📌")
        text = f"{emoji} *{level} — {title}*\n{body}"
        return self.send(text)


# ─────────────────────────────────────────────
# GMAIL (via OAuth2)
# ─────────────────────────────────────────────

class GmailMessaging(BaseIntegration):
    BASE = "https://gmail.googleapis.com/gmail/v1"

    def __init__(self, tenant_id: str, config: dict):
        super().__init__(tenant_id, config)
        self.access_token    = config.get("gmail_access_token", "")
        self.from_email      = config.get("email_from_address", "")
        self.from_name       = config.get("email_from_name", "Revenue OS")

    def is_configured(self) -> bool:
        return bool(self.access_token and self.from_email)

    def health_check(self) -> IntegrationHealth:
        try:
            r = requests.get(f"{self.BASE}/users/me/profile",
                             headers={"Authorization": f"Bearer {self.access_token}"}, timeout=5)
            if r.status_code == 401:
                return IntegrationHealth("gmail", IntegrationStatus.DOWN, error="Token expired (401)")
            r.raise_for_status()
            return IntegrationHealth("gmail", IntegrationStatus.HEALTHY)
        except Exception as e:
            return IntegrationHealth("gmail", IntegrationStatus.DOWN, error=str(e))

    def send(self, to: str, subject: str, body: str, html: str = None) -> dict:
        """Send email. Returns {message_id, status}."""
        if not self.is_configured():
            raise IntegrationError("gmail", "send", "Gmail not configured", retryable=False)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{self.from_name} <{self.from_email}>"
        msg["To"]      = to

        msg.attach(MIMEText(body, "plain"))
        if html:
            msg.attach(MIMEText(html, "html"))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        def _send():
            r = requests.post(
                f"{self.BASE}/users/me/messages/send",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json"
                },
                json={"raw": raw},
                timeout=20
            )
            if r.status_code == 401:
                raise IntegrationError("gmail", "send", "Token expired", retryable=False)
            if r.status_code == 429:
                raise IntegrationError("gmail", "send", "Rate limit", retryable=True)
            r.raise_for_status()
            data = r.json()
            return {"message_id": data.get("id"), "status": "sent"}

        return self.call_with_retry(_send)

    def list_recent_threads(self, query: str = "", max_results: int = 20) -> list[dict]:
        """List recent email threads matching query."""
        params = {"maxResults": max_results, "q": query}

        def _fetch():
            r = requests.get(
                f"{self.BASE}/users/me/threads",
                headers={"Authorization": f"Bearer {self.access_token}"},
                params=params, timeout=15
            )
            if r.status_code == 401:
                raise IntegrationError("gmail", "list_threads", "Token expired", retryable=False)
            r.raise_for_status()
            return r.json().get("threads", [])

        return self.call_with_retry(_fetch)

    def check_reply(self, thread_id: str) -> bool:
        """Returns True if a thread has more than 1 message (i.e., someone replied)."""
        def _fetch():
            r = requests.get(
                f"{self.BASE}/users/me/threads/{thread_id}",
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=15
            )
            if r.status_code == 401:
                raise IntegrationError("gmail", "check_reply", "Token expired", retryable=False)
            r.raise_for_status()
            return r.json()

        try:
            data = self.call_with_retry(_fetch)
            messages = data.get("messages", [])
            return len(messages) > 1
        except IntegrationError as e:
            logger.warning(f"[{self.tenant_id}] Could not check reply for thread {thread_id}: {e}")
            return False


# ─────────────────────────────────────────────
# RESEND (transactional email API)
# ─────────────────────────────────────────────

class ResendMessaging(BaseIntegration):
    BASE = "https://api.resend.com"

    def __init__(self, tenant_id: str, config: dict):
        super().__init__(tenant_id, config)
        self.api_key    = config.get("resend_api_key", "")
        self.from_email = config.get("email_from_address", "noreply@yourdomain.com")
        self.from_name  = config.get("email_from_name", "Revenue OS")

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def health_check(self) -> IntegrationHealth:
        try:
            r = requests.get(f"{self.BASE}/emails",
                             headers={"Authorization": f"Bearer {self.api_key}"}, timeout=5)
            if r.status_code == 401:
                return IntegrationHealth("resend", IntegrationStatus.DOWN, error="Invalid API key")
            return IntegrationHealth("resend", IntegrationStatus.HEALTHY)
        except Exception as e:
            return IntegrationHealth("resend", IntegrationStatus.DOWN, error=str(e))

    def send(self, to: str, subject: str, body: str, html: str = None) -> dict:
        if not self.is_configured():
            raise IntegrationError("resend", "send", "Resend not configured", retryable=False)

        payload = {
            "from": f"{self.from_name} <{self.from_email}>",
            "to": [to],
            "subject": subject,
            "text": body,
        }
        if html:
            payload["html"] = html

        def _send():
            r = requests.post(
                f"{self.BASE}/emails",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload, timeout=15
            )
            if r.status_code == 401:
                raise IntegrationError("resend", "send", "Invalid API key", retryable=False)
            if r.status_code == 429:
                raise IntegrationError("resend", "send", "Rate limit", retryable=True)
            r.raise_for_status()
            data = r.json()
            return {"message_id": data.get("id"), "status": "sent"}

        return self.call_with_retry(_send)
