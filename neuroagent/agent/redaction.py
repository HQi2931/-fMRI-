"""Fail-closed policy for information leaving the local machine."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from neuroagent.agent.models import RedactedContext


class OutboundPolicyError(ValueError):
    """Raised when outbound content cannot be proven safe."""


class OutboundContextPolicy:
    _drop_key_fragments = (
        "api_key",
        "secret",
        "token",
        "password",
        "patient_name",
        "full_name",
        "email",
        "phone",
        "address",
        "identity",
        "dicom",
        "nifti",
        "image_bytes",
        "absolute_path",
        "source_path",
        "output_path",
        "姓名",
        "患者",
        "病人",
        "住院号",
        "病历号",
        "门诊号",
        "身份证",
        "手机号",
        "电话",
        "地址",
        "出生",
    )
    _drop_keys = frozenset(
        {
            "demographic",
            "demographics",
            "date_of_birth",
            "birth_date",
            "birth_year",
            "age",
            "ages",
            "sex",
            "sexes",
            "gender",
            "genders",
        }
    )
    _subject_keys = frozenset({"subject_id", "participant_id", "patient_id"})
    _nested_subject_identity_keys = frozenset({"id", "ids", "identifier", "identifiers"})
    _absolute_path = re.compile(
        r"(?:[A-Za-z]:[\\/][^\r\n\"'<>]+|\\\\[^\\/\s]+[\\/][^\r\n\"'<>]+|"
        r"(?<!\w)/(?:home|Users|mnt|data)/[^\r\n\"'<>]+)"
    )
    _email = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
    _phone = re.compile(r"(?<!\d)(?:\+?\d[\d -]{8,}\d)(?!\d)")
    _subject_token = re.compile(
        r"\b(?:sub(?:ject)?|participant|patient)[-_]?[A-Za-z0-9][A-Za-z0-9_.-]*\b",
        re.IGNORECASE,
    )
    _opaque_subject_token = re.compile(r"\b(?:S|P)\d{3,}\b")
    # Free-form text is permitted only when it does not look like a clinical
    # narrative.  These patterns intentionally reject instead of attempting a
    # lossy substitution: a partial redaction is not proof that the remaining
    # sentence is de-identified.
    _clinical_identifier = re.compile(
        r"(?:患者|病人|受试者|姓名|住院号|病历号|门诊号|就诊号|身份证(?:号)?|"
        r"医保号|手机号|联系电话|家庭住址|出生日期|"
        r"\b(?:patient|name|mrn|medical\s*record|admission)\b)",
        re.IGNORECASE,
    )
    _age_demographic = re.compile(
        r"(?<!\d)(?:[1-9]?\d|1[01]\d|120)\s*岁(?:\s*(?:男|女|男性|女性))?"
    )
    _chinese_person_reference = re.compile(
        r"(?:患者|病人|受试者|姓名(?:为|是)?)[\uff1a:\s]*[\u3400-\u9fff·]{2,8}"
    )
    _credential = re.compile(
        r"(?:\bBearer\s+[A-Za-z0-9._~-]{16,}|\bsk-[A-Za-z0-9_-]{16,}|"
        r"\bgh(?:p|o|u|s|r)_[A-Za-z0-9]{20,}|"
        r"\b(?:api[_ -]?key|secret|token|password)\s*[:=]\s*\S{12,})",
        re.IGNORECASE,
    )

    def __init__(self, salt: str) -> None:
        if len(salt) < 16:
            raise OutboundPolicyError("redaction salt must contain at least 16 characters")
        self._salt = salt.encode("utf-8")

    def redact(self, payload: Mapping[str, Any]) -> RedactedContext:
        sanitized, count = self._sanitize_mapping(payload, depth=0, subject_context=False)
        encoded = json.dumps(
            sanitized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if self._absolute_path.search(encoded) or self._email.search(encoded):
            raise OutboundPolicyError("outbound context still contains a prohibited identifier")
        context_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return RedactedContext(
            payload=sanitized,
            redaction_count=count,
            context_hash=context_hash,
        )

    def _sanitize_mapping(
        self, payload: Mapping[str, Any], *, depth: int, subject_context: bool
    ) -> tuple[dict[str, Any], int]:
        if depth > 12:
            raise OutboundPolicyError("outbound context is nested too deeply")
        if len(payload) > 100:
            raise OutboundPolicyError("outbound object contains too many fields")
        result: dict[str, Any] = {}
        redactions = 0
        for raw_key, value in payload.items():
            key = str(raw_key)
            normalized = key.lower()
            if normalized in self._drop_keys or any(
                fragment in normalized for fragment in self._drop_key_fragments
            ):
                redactions += 1
                continue
            is_subject_container = any(
                fragment in normalized for fragment in ("subject", "participant", "patient")
            ) and normalized not in {"n_subjects", "subject_count", "participant_count"}
            is_subject_field = (
                normalized in self._subject_keys
                or (
                    normalized.endswith("_ids")
                    and any(
                        prefix in normalized for prefix in ("subject", "participant", "patient")
                    )
                )
                or (subject_context and normalized in self._nested_subject_identity_keys)
            )
            if (
                is_subject_container
                and isinstance(value, Sequence)
                and not isinstance(value, (str, bytes))
                and all(isinstance(item, str) for item in value)
            ):
                is_subject_field = True
            if is_subject_field:
                result[key] = self._pseudonymize_field(value)
                redactions += 1
                continue
            cleaned, child_count = self._sanitize_value(
                value,
                depth=depth + 1,
                subject_context=subject_context or is_subject_container,
            )
            result[key] = cleaned
            redactions += child_count
        return result, redactions

    def _sanitize_value(self, value: Any, *, depth: int, subject_context: bool) -> tuple[Any, int]:
        if value is None or isinstance(value, (bool, int, float)):
            return value, 0
        if isinstance(value, bytes):
            raise OutboundPolicyError("binary content cannot leave the local machine")
        if isinstance(value, str):
            return self._sanitize_string(value)
        if isinstance(value, Mapping):
            return self._sanitize_mapping(
                value,
                depth=depth,
                subject_context=subject_context,
            )
        if isinstance(value, Sequence):
            if len(value) > 100:
                raise OutboundPolicyError("outbound list contains too many entries")
            items: list[Any] = []
            redactions = 0
            for item in value:
                cleaned, child_count = self._sanitize_value(
                    item,
                    depth=depth + 1,
                    subject_context=subject_context,
                )
                items.append(cleaned)
                redactions += child_count
            return items, redactions
        raise OutboundPolicyError(f"unsupported outbound value type: {type(value).__name__}")

    def _sanitize_string(self, value: str) -> tuple[str, int]:
        if len(value) > 20_000:
            raise OutboundPolicyError("outbound string is too long")
        if (
            self._clinical_identifier.search(value)
            or self._age_demographic.search(value)
            or self._chinese_person_reference.search(value)
            or self._credential.search(value)
        ):
            raise OutboundPolicyError("free-form sensitive text cannot leave the local machine")
        redactions = 0
        cleaned = value
        if self._absolute_path.search(cleaned):
            cleaned = self._absolute_path.sub("[redacted-path]", cleaned)
            redactions += 1
        if self._email.search(cleaned):
            cleaned = self._email.sub("[redacted-email]", cleaned)
            redactions += 1
        if self._phone.search(cleaned):
            cleaned = self._phone.sub("[redacted-phone]", cleaned)
            redactions += 1
        subject_matches = tuple(self._subject_token.finditer(cleaned))
        if subject_matches:
            cleaned = self._subject_token.sub(
                lambda match: self._pseudonym(match.group(0)), cleaned
            )
            redactions += len(subject_matches)
        opaque_matches = tuple(self._opaque_subject_token.finditer(cleaned))
        if opaque_matches:
            cleaned = self._opaque_subject_token.sub(
                lambda match: self._pseudonym(match.group(0)), cleaned
            )
            redactions += len(opaque_matches)
        return cleaned, redactions

    def _pseudonymize_field(self, value: Any) -> str | list[str]:
        if isinstance(value, str):
            return self._pseudonym(value)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if len(value) > 100 or any(not isinstance(item, str) for item in value):
                raise OutboundPolicyError("subject ID fields must contain at most 100 strings")
            return [self._pseudonym(item) for item in value]
        raise OutboundPolicyError("subject ID fields must be a string or list of strings")

    def _pseudonym(self, source_id: str) -> str:
        digest = hmac.new(self._salt, source_id.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"subject-{digest[:12]}"
