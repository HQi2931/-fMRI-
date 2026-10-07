"""Transport-neutral runtime event definitions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from neuroagent.observability.tracing import current_trace_id


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    event_type: str
    severity: str = "info"
    project_id: str | None = None
    run_id: str | None = None
    trace_id: str = field(default_factory=current_trace_id)
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


SENSITIVE_EVENT_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "password",
        "patient_name",
        "patient_id",
        "raw_demographics",
    }
)
SENSITIVE_EVENT_KEY_FRAGMENTS = (
    "api_key",
    "authorization",
    "password",
    "secret",
    "token",
    "patient",
    "subject_id",
    "participant_id",
    "demographic",
    "source_path",
    "absolute_path",
)
_ABSOLUTE_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/][^\r\n\"'<>]+|\\\\[^\\/\s]+[\\/][^\r\n\"'<>]+|"
    r"(?<!\w)/(?:home|Users|mnt|data)/[^\r\n\"'<>]+)"
)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d -]{8,}\d)(?!\d)")
_SUBJECT_TOKEN = re.compile(
    r"\b(?:sub(?:ject)?|participant|patient)[-_]?[A-Za-z0-9][A-Za-z0-9_.-]*\b",
    re.IGNORECASE,
)
_CLINICAL_TEXT = re.compile(
    r"(?:患者|病人|受试者|姓名|住院号|病历号|门诊号|身份证|手机号|家庭住址|"
    r"(?<!\d)(?:[1-9]?\d|1[01]\d|120)\s*岁)",
    re.IGNORECASE,
)
_CREDENTIAL = re.compile(
    r"(?:\bBearer\s+[A-Za-z0-9._~-]{16,}|\bsk-[A-Za-z0-9_-]{16,}|"
    r"\bgh(?:p|o|u|s|r)_[A-Za-z0-9]{20,}|"
    r"\b(?:api[_ -]?key|secret|token|password)\s*[:=]\s*\S{12,})",
    re.IGNORECASE,
)
_SYSTEM_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)
_SYSTEM_HASH = re.compile(r"[0-9a-f]{64}", re.IGNORECASE)
_SYSTEM_REFERENCE_KEYS = frozenset(
    {
        "project_id",
        "run_id",
        "job_id",
        "artifact_id",
        "plan_revision_id",
        "review_revision_id",
        "approval_record_id",
        "conversation_id",
        "message_id",
        "dataset_id",
        "manifest_id",
        "confirmation_id",
        "design_id",
        "profile_id",
    }
)
_SYSTEM_HASH_KEYS = frozenset(
    {
        "plan_hash",
        "review_hash",
        "manifest_hash",
        "input_manifest_hash",
        "job_hash",
        "environment_hash",
        "design_hash",
        "content_hash",
        "parameters_hash",
        "proposal_hash",
    }
)


def redact_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove known secret/identity fields before persistence."""

    def sensitive_key(key: str) -> bool:
        normalized = key.lower()
        return normalized in SENSITIVE_EVENT_KEYS or any(
            fragment in normalized for fragment in SENSITIVE_EVENT_KEY_FRAGMENTS
        )

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: redact_field(key, nested) for key, nested in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, tuple):
            return [redact(item) for item in value]
        if isinstance(value, str):
            if _CLINICAL_TEXT.search(value) or _CREDENTIAL.search(value):
                return "[REDACTED-SENSITIVE-TEXT]"
            cleaned = _ABSOLUTE_PATH.sub("[REDACTED-PATH]", value)
            cleaned = _EMAIL.sub("[REDACTED-EMAIL]", cleaned)
            cleaned = _PHONE.sub("[REDACTED-PHONE]", cleaned)
            return _SUBJECT_TOKEN.sub("[REDACTED-SUBJECT]", cleaned)
        return value

    def redact_field(key: str, value: Any) -> Any:
        if sensitive_key(key):
            return "[REDACTED]"
        normalized = key.lower()
        # Only complete, typed system references bypass free-text phone matching.
        # Identity/credential keys above always take precedence.
        if isinstance(value, str) and (
            (normalized in _SYSTEM_REFERENCE_KEYS and _SYSTEM_UUID.fullmatch(value))
            or (normalized in _SYSTEM_HASH_KEYS and _SYSTEM_HASH.fullmatch(value))
        ):
            return value
        return redact(value)

    return {key: redact_field(key, value) for key, value in payload.items()}
