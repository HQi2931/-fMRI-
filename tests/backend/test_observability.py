from neuroagent.observability.events import redact_event_payload


def test_event_payload_redacts_nested_keys_paths_and_clinical_free_text() -> None:
    payload = redact_event_payload(
        {
            "reason": "请查看患者张三, 65岁, 住院号12345678",
            "nested": {
                "api_key_env": "PRIVATE_PROVIDER_API_KEY",
                "note": r"Read D:\private\sub-raw-01\scan.nii and email a@example.org",
            },
            "safe_count": 3,
        }
    )

    assert payload["reason"] == "[REDACTED-SENSITIVE-TEXT]"
    assert payload["nested"]["api_key_env"] == "[REDACTED]"
    assert "private" not in payload["nested"]["note"]
    assert "sub-raw-01" not in payload["nested"]["note"]
    assert "example.org" not in payload["nested"]["note"]
    assert payload["safe_count"] == 3


def test_event_payload_rejects_credential_shaped_free_text_by_redaction() -> None:
    payload = redact_event_payload({"message": "token=abcdefghijklmnopqrstuv"})
    assert payload["message"] == "[REDACTED-SENSITIVE-TEXT]"
