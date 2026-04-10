"""
validators/llm_output.py
Validates ALL LLM outputs before they reach the DB or any action.
If validation fails, the output is quarantined and flagged for review.
"""

import re
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

SPAM_WORDS = [
    "synergy", "leverage", "game-changer", "revolutionary", "paradigm",
    "disruptive", "cutting-edge", "best-in-class", "world-class", "robust solution"
]


@dataclass
class ValidationResult:
    valid: bool
    output: str          # cleaned output if valid, empty string if invalid
    warnings: list
    errors: list
    fallback_used: bool = False


def validate_email_output(raw: str, lead_context: dict = None) -> ValidationResult:
    """
    Validate a generated email (subject + body).
    Returns ValidationResult with cleaned output or errors.
    """
    warnings = []
    errors = []

    if not raw or len(raw.strip()) < 10:
        return ValidationResult(False, "", [], ["Empty or too short output from LLM"], False)

    # Extract subject
    subject_match = re.search(r"Subject:\s*(.+)", raw, re.IGNORECASE)
    if not subject_match:
        errors.append("No 'Subject:' line found in LLM output")
        return ValidationResult(False, "", warnings, errors)

    subject = subject_match.group(1).strip()
    body    = re.sub(r"Subject:\s*.+\n?", "", raw, flags=re.IGNORECASE).strip()

    # Subject checks
    if len(subject) < 3:
        errors.append(f"Subject too short: '{subject}'")
    if len(subject) > 100:
        warnings.append(f"Subject longer than 100 chars ({len(subject)})")
    if "!!!" in subject or subject.isupper():
        warnings.append("Subject may look spammy (all caps or triple exclamation)")

    # Body checks
    word_count = len(body.split())
    if word_count < 10:
        errors.append(f"Body too short: {word_count} words")
    if word_count > 200:
        warnings.append(f"Body longer than 200 words ({word_count}) — may reduce reply rate")

    # Spam signal checks
    body_lower = body.lower()
    for spam in SPAM_WORDS:
        if spam in body_lower:
            warnings.append(f"Spam word detected: '{spam}'")

    # Hallucination checks — look for placeholder brackets
    if re.search(r"\[.*?\]", body):
        errors.append("Body contains unfilled template placeholders: " +
                      str(re.findall(r"\[.*?\]", body)))

    # Lead name check — if lead context provided, verify name is correct
    if lead_context:
        lead_name = lead_context.get("name", "")
        if lead_name and lead_name.lower() not in body.lower() and lead_name.lower() not in subject.lower():
            warnings.append(f"Lead name '{lead_name}' not found in email — check personalization")

    if errors:
        logger.warning(f"Email validation failed: {errors}")
        return ValidationResult(False, "", warnings, errors)

    cleaned = f"Subject: {subject}\n\n{body}"
    return ValidationResult(True, cleaned, warnings, errors)


def validate_deal_analysis(raw: str) -> ValidationResult:
    """Validate deal stagnation analysis output."""
    warnings = []
    errors = []

    if not raw or len(raw.strip()) < 20:
        return ValidationResult(False, "", [], ["Empty output from LLM"])

    # Check expected sections
    required = ["DIAGNOSIS:", "ACTION:"]
    for section in required:
        if section not in raw.upper():
            errors.append(f"Missing required section: {section}")

    if errors:
        return ValidationResult(False, "", warnings, errors)

    # Check action is specific (not generic)
    action_match = re.search(r"ACTION:\s*(.+?)(?:RISK|$)", raw, re.IGNORECASE | re.DOTALL)
    if action_match:
        action = action_match.group(1).strip()
        generic_phrases = ["follow up", "check in", "reach out", "touch base", "ping them"]
        for phrase in generic_phrases:
            if phrase in action.lower():
                warnings.append(f"Action may be too generic: '{phrase}' detected — consider more specific guidance")

    return ValidationResult(True, raw.strip(), warnings, errors)


def validate_treasury_explanation(raw: str, snapshot: dict) -> ValidationResult:
    """
    Validate treasury explanation from LLM.
    Critical: verify numerical claims match the actual calculated snapshot.
    """
    warnings = []
    errors = []

    if not raw or len(raw.strip()) < 20:
        return ValidationResult(False, "", [], ["Empty output from LLM"])

    # Check required sections
    for section in ["SITUATION:", "ACTION:"]:
        if section not in raw.upper():
            errors.append(f"Missing required section: {section}")

    if errors:
        return ValidationResult(False, "", warnings, errors)

    # Numerical plausibility check
    # Extract any numbers mentioned in the explanation
    numbers_in_text = [float(n.replace(",", "")) for n in re.findall(r"[\d,]+\.?\d*", raw)]

    actual_runway = float(snapshot.get("runway_months", 0))
    if actual_runway > 0 and numbers_in_text:
        # Check if the explanation references a runway wildly different from reality
        runway_candidates = [n for n in numbers_in_text if 0 < n < 120]
        if runway_candidates:
            closest = min(runway_candidates, key=lambda x: abs(x - actual_runway))
            if abs(closest - actual_runway) > actual_runway * 0.5:
                errors.append(
                    f"LLM runway claim ({closest}) differs >50% from calculated ({actual_runway}) — "
                    "possible hallucination. Do not send."
                )

    if errors:
        return ValidationResult(False, "", warnings, errors)

    return ValidationResult(True, raw.strip(), warnings, errors)


def validate_brief_output(raw: str) -> ValidationResult:
    """Validate weekly executive brief."""
    warnings = []
    errors = []

    if not raw or len(raw.strip()) < 30:
        return ValidationResult(False, "", [], ["Empty brief output"])

    # Must have at least 2 bullet points or structured sections
    bullet_count = len(re.findall(r"^[\-\*•]|\n[\-\*•]", raw))
    bracket_count = len(re.findall(r"\[.+?\]", raw))
    if bullet_count < 2 and bracket_count < 2:
        warnings.append("Brief has fewer than 2 bullet points — may be too sparse")

    # Check for placeholder brackets (unfilled template)
    if re.search(r"\{\{.*?\}\}", raw):
        errors.append("Brief contains unfilled template variables — LLM failed to substitute")

    if errors:
        return ValidationResult(False, "", warnings, errors)

    return ValidationResult(True, raw.strip(), warnings, errors)


def validate_next_best_action(raw: str) -> ValidationResult:
    """Validate next-best-action recommendation."""
    warnings = []
    errors = []

    if not raw or len(raw.strip()) < 10:
        return ValidationResult(False, "", [], ["Empty output"])

    # Should contain: action type + channel + rationale
    expected_fields = ["ACTION_TYPE", "CHANNEL", "RATIONALE"]
    missing = [f for f in expected_fields if f not in raw.upper()]
    if missing:
        errors.append(f"Missing required fields: {missing}")

    if errors:
        return ValidationResult(False, "", warnings, errors)

    return ValidationResult(True, raw.strip(), warnings, errors)
