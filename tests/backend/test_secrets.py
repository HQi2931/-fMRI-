"""Local dotenv secret handling.

Secrets must never reach the database, logs or model context, so the write path
whitelists the name shape and the read path gives the process environment
precedence.  These tests pin both, including the failure modes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from neuroagent.infrastructure.secrets import (
    LocalDotenvSecretResolver,
    LocalDotenvSecretWriter,
    write_env_secret,
)


@pytest.mark.parametrize(
    "name",
    [
        "lowercase_api_key",
        "NO_SUFFIX",
        "MISSINGKEY",
        "1LEADING_DIGIT_API_KEY",
        "WITH-DASH_API_KEY",
        "HAS SPACE_API_KEY",
        "API_KEY",
    ],
)
def test_write_rejects_names_outside_the_api_key_shape(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="_API_KEY pattern"):
        write_env_secret(tmp_path / ".env", name, "value")


@pytest.mark.parametrize("value", ["", "   ", "two\nlines", "two\rlines"])
def test_write_rejects_empty_or_multiline_values(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError, match="single non-empty line"):
        write_env_secret(tmp_path / ".env", "TEST_API_KEY", value)


def test_write_normalises_surrounding_whitespace(tmp_path: Path) -> None:
    # A trailing CR from CRLF input is stripped rather than rejected; only an
    # embedded line break is a hard failure.
    target = tmp_path / ".env"

    write_env_secret(target, "TEST_API_KEY", "  spaced-value\r\n")

    assert target.read_text(encoding="utf-8") == "TEST_API_KEY=spaced-value\n"


def test_write_creates_missing_file_and_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / ".env"

    write_env_secret(target, "TEST_API_KEY", " secret-value ")

    assert target.read_text(encoding="utf-8") == "TEST_API_KEY=secret-value\n"


def test_write_replaces_an_existing_line_in_place(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text(
        "OTHER_SETTING=keep\nTEST_API_KEY=old-value\nTRAILING=keep\n", encoding="utf-8"
    )

    write_env_secret(target, "TEST_API_KEY", "new-value")

    assert target.read_text(encoding="utf-8") == (
        "OTHER_SETTING=keep\nTEST_API_KEY=new-value\nTRAILING=keep\n"
    )


def test_write_upgrades_a_commented_placeholder(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("# TEST_API_KEY=\nOTHER=1\n", encoding="utf-8")

    write_env_secret(target, "TEST_API_KEY", "filled")

    assert target.read_text(encoding="utf-8") == "TEST_API_KEY=filled\nOTHER=1\n"


def test_write_appends_when_the_name_is_absent(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("OTHER=1\n", encoding="utf-8")

    write_env_secret(target, "TEST_API_KEY", "appended")

    assert target.read_text(encoding="utf-8") == "OTHER=1\nTEST_API_KEY=appended\n"


def test_writer_adapter_delegates_to_the_same_contract(tmp_path: Path) -> None:
    target = tmp_path / ".env"

    LocalDotenvSecretWriter().write(target, "TEST_API_KEY", "via-adapter")

    assert target.read_text(encoding="utf-8") == "TEST_API_KEY=via-adapter\n"


@pytest.mark.parametrize("name", ["not_a_key", "TEST_API_KEY ", "test_api_key"])
def test_resolve_ignores_names_outside_the_api_key_shape(
    tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name.strip(), "process-value")
    assert LocalDotenvSecretResolver(tmp_path / ".env").resolve(name) is None


def test_resolve_prefers_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / ".env"
    target.write_text("TEST_API_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("TEST_API_KEY", "from-process")

    assert LocalDotenvSecretResolver(target).resolve("TEST_API_KEY") == "from-process"


def test_resolve_falls_back_to_the_dotenv_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_API_KEY", raising=False)
    target = tmp_path / ".env"
    target.write_text("TEST_API_KEY=from-dotenv\n", encoding="utf-8")

    assert LocalDotenvSecretResolver(target).resolve("TEST_API_KEY") == "from-dotenv"


def test_resolve_returns_none_when_the_file_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_API_KEY", raising=False)

    assert LocalDotenvSecretResolver(tmp_path / "missing.env").resolve("TEST_API_KEY") is None


def test_resolve_returns_none_for_a_blank_dotenv_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_API_KEY", raising=False)
    target = tmp_path / ".env"
    target.write_text("TEST_API_KEY=\n", encoding="utf-8")

    assert LocalDotenvSecretResolver(target).resolve("TEST_API_KEY") is None


def test_write_then_resolve_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_API_KEY", raising=False)
    target = tmp_path / ".env"

    write_env_secret(target, "TEST_API_KEY", "round-trip")

    assert LocalDotenvSecretResolver(target).resolve("TEST_API_KEY") == "round-trip"
