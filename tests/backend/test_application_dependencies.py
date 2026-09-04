from __future__ import annotations

from pathlib import Path

from neuroagent.application.contracts import ModelProfileCreate, ModelProfileInput
from neuroagent.application.services import NeuroAgentService
from neuroagent.bootstrap import build_service


class RecordingSecretWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str, str]] = []

    def write(self, secrets_file: Path, name: str, value: str) -> None:
        self.calls.append((secrets_file, name, value))


def test_model_profile_creation_uses_injected_secret_writer(
    service: NeuroAgentService,
) -> None:
    writer = RecordingSecretWriter()
    isolated_service = build_service(
        service.settings,
        database=service.database,
        repository=service.repository,
        providers={},
        secret_writer=writer,
    )
    try:
        profile = ModelProfileInput(
            id="injected-secret-profile",
            provider="openai-compatible",
            base_url="https://provider.example/v1",
            model="test-model",
            api_key_env="INJECTED_PROVIDER_API_KEY",
            priority=1,
            capabilities=frozenset(),
            timeout_seconds=30,
        )

        isolated_service.create_model_profile(
            ModelProfileCreate(profile=profile, api_key="secret-value"),
            "injected-secret-profile-create",
        )

        assert writer.calls == [
            (service.settings.secrets_file, "INJECTED_PROVIDER_API_KEY", "secret-value")
        ]
    finally:
        isolated_service.close()
