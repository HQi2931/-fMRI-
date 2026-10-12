"""Guards that the test suite never inherits the developer's machine state.

The repository ``.env`` is untracked and carries real DPABI installation paths,
real provider API keys and ``RSFMRI_ENABLE_REAL_EXECUTION``.  Before this guard
existed the ``service`` fixture loaded that file, so the same tree produced a
different result on a developer machine than in CI: the real-execution gate was
satisfied locally and the environment-readiness guard fired instead, failing
``test_real_run_is_blocked_by_default_even_after_confirmation``.

These assertions pin the mechanism rather than the symptom, so a future field
read through ``.env`` cannot silently reintroduce the divergence.
"""

from __future__ import annotations

from pathlib import Path

from neuroagent.application.services import ApplicationServices
from neuroagent.application.settings import Settings


def test_dotenv_is_ignored_when_env_file_is_disabled(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "RSFMRI_ENABLE_REAL_EXECUTION=true\n"
        "RSFMRI_DPABI_VERSION=V8.2_240510\n"
        "RSFMRI_SERVE_FRONTEND=true\n",
        encoding="utf-8",
    )

    inherited = Settings(_env_file=dotenv)
    isolated = Settings(_env_file=None, enable_real_execution=False)

    # The leak this guard exists for: a dotenv file could enable real execution.
    assert inherited.enable_real_execution is True
    assert inherited.dpabi_version == "V8.2_240510"

    # An explicitly pinned value wins, and nothing is read from disk.
    assert isolated.enable_real_execution is False
    assert isolated.dpabi_version == "unspecified"
    assert isolated.serve_frontend is False


def test_service_fixture_settings_are_hermetic(
    service: ApplicationServices, tmp_path: Path
) -> None:
    settings = service.settings

    # Real MATLAB execution stays off no matter what the developer exported.
    assert settings.enable_real_execution is False
    assert settings.serve_frontend is False

    # A test must not be able to read the developer's real provider keys.
    project_root = Path(__file__).resolve().parents[2]
    assert settings.secrets_file != project_root / ".env"
    assert tmp_path in settings.secrets_file.parents

    # Filesystem access stays inside the per-test temporary root.
    assert settings.allowed_work_root == tmp_path / "work"
    assert all(tmp_path in root.parents for root in settings.allowed_source_roots)
