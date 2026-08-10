import pytest

from neuroagent.agent.redaction import OutboundContextPolicy, OutboundPolicyError


def test_redacts_paths_identifiers_and_pseudonymizes_subjects() -> None:
    policy = OutboundContextPolicy("a-stable-test-salt-value")
    result = policy.redact(
        {
            "subject_id": "sub-001",
            "source_path": r"D:\private\sub-001.nii.gz",
            "message": "Read D:\\private\\scan.nii.gz and contact person@example.org",
            "summary": {"n_subjects": 12},
        }
    )

    assert result.payload["subject_id"].startswith("subject-")
    assert "source_path" not in result.payload
    assert "private" not in result.payload["message"]
    assert "example.org" not in result.payload["message"]
    assert result.redaction_count >= 3


def test_binary_payload_fails_closed() -> None:
    policy = OutboundContextPolicy("a-stable-test-salt-value")
    with pytest.raises(OutboundPolicyError):
        policy.redact({"payload": b"not allowed"})


def test_subject_lists_and_demographics_are_removed_or_pseudonymized() -> None:
    policy = OutboundContextPolicy("a-stable-test-salt-value")
    result = policy.redact(
        {
            "subject_ids": ["sub-001", "sub-002"],
            "demographics": [{"age": 31, "sex": "F"}],
            "note": "Review sub-003 without exposing the identifier.",
            "aggregate_count": 3,
        }
    )

    assert result.payload["subject_ids"] == [
        policy._pseudonym("sub-001"),
        policy._pseudonym("sub-002"),
    ]
    assert "demographics" not in result.payload
    assert "sub-003" not in result.payload["note"]
    assert result.payload["aggregate_count"] == 3


def test_plural_demographic_fields_are_removed() -> None:
    policy = OutboundContextPolicy("a-stable-test-salt-value")
    result = policy.redact({"ages": [31, 42], "sexes": ["F", "M"], "count": 2})

    assert result.payload == {"count": 2}


def test_subject_order_and_nested_opaque_ids_are_pseudonymized() -> None:
    policy = OutboundContextPolicy("a-stable-test-salt-value")
    result = policy.redact(
        {
            "subject_order": ["S001", "P001"],
            "included_subjects": ["S002"],
            "subjects": [
                {"id": "S003", "qc_passed": True},
                {"identifier": "P004", "qc_passed": False},
            ],
            "n_subjects": 4,
        }
    )

    encoded = str(result.payload)
    for raw_id in ("S001", "P001", "S002", "S003", "P004"):
        assert raw_id not in encoded
    assert result.payload["n_subjects"] == 4
    assert result.redaction_count >= 4


def test_opaque_subject_tokens_in_free_text_are_pseudonymized() -> None:
    policy = OutboundContextPolicy("a-stable-test-salt-value")
    result = policy.redact({"note": "S001 and P002 require manual QC"})

    assert "S001" not in result.payload["note"]
    assert "P002" not in result.payload["note"]


def test_non_string_subject_list_fails_closed() -> None:
    policy = OutboundContextPolicy("a-stable-test-salt-value")
    with pytest.raises(OutboundPolicyError):
        policy.redact({"subject_ids": ["sub-001", 2]})


@pytest.mark.parametrize(
    "unsafe_text",
    (
        "请分析患者张三, 65岁女性, 住院号12345678的结果",
        "受试者: 李四, 本次检查结果需要总结",
        "patient Alice has medical record 12345678",
    ),
)
def test_free_form_clinical_text_fails_closed(unsafe_text: str) -> None:
    policy = OutboundContextPolicy("a-stable-test-salt-value")
    with pytest.raises(OutboundPolicyError, match="sensitive text"):
        policy.redact({"user_question": unsafe_text})


def test_generic_chinese_method_question_remains_allowed() -> None:
    policy = OutboundContextPolicy("a-stable-test-salt-value")
    result = policy.redact({"user_question": "请解释 ReHo 的处理顺序和待确认参数"})
    assert result.payload == {"user_question": "请解释 ReHo 的处理顺序和待确认参数"}


def test_credential_embedded_in_free_text_fails_closed() -> None:
    policy = OutboundContextPolicy("a-stable-test-salt-value")
    with pytest.raises(OutboundPolicyError, match="sensitive text"):
        policy.redact({"note": "api_key=abcdefghijklmnopqrstuv"})
