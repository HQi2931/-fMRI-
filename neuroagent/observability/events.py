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


def redact_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove known secret/identity fields before persistence."""

    def sensitive_key(key: str) -> bool:
        normalized = key.lower()
        return normalized in SENSITIVE_EVENT_KEYS or any(
            fragment in normalized for fragment in SENSITIVE_EVENT_KEY_FRAGMENTS
        )

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "[REDACTED]" if sensitive_key(key) else redact(nested)
                for key, nested in value.items()
            }
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

    return {
        key: "[REDACTED]" if sensitive_key(key) else redact(value) for key, value in payload.items()
    }
