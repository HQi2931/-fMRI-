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


def test_system_references_survive_phone_shaped_digits_but_identities_do_not() -> None:
    identifier = "823ac579-a123-4567-890c-5791bfa49be4"
    checksum = "a" * 20 + "12345678901" + "b" * 33
    payload = redact_event_payload(
        {
            "review_revision_id": identifier,
            "nested": {"review_hash": checksum},
            "patient_id": identifier,
            "subject_id": identifier,
            "phone_id": "13812345678",
            "review_revision_id_invalid": "13812345678",
            "arbitrary_id": identifier,
        }
    )
    assert payload["review_revision_id"] == identifier
    assert payload["nested"]["review_hash"] == checksum
    assert payload["patient_id"] == payload["subject_id"] == "[REDACTED]"
    assert payload["phone_id"] == payload["review_revision_id_invalid"] == "[REDACTED-PHONE]"
    assert payload["arbitrary_id"] != identifier
