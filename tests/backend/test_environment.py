from pathlib import Path

import pytest

from neuroagent.application.contracts import EnvironmentConfigUpdate
from neuroagent.application.settings import Settings
from neuroagent.infrastructure.environment import SettingsEnvironmentLockProvider

DPABI_ENTRY_FILES = (
    "dpabi.m",
    "DPARSF/DPARSFA_run.m",
    "DPARSF/Subfunctions/y_alff_falff.m",
    "DPARSF/Subfunctions/y_reho.m",
    "StatisticalAnalysis/y_TTest1_Image.m",
    "StatisticalAnalysis/y_TTest2_Image.m",
    "StatisticalAnalysis/y_TTestPaired_Image.m",
    "StatisticalAnalysis/y_GroupAnalysis_Image.m",
    "StatisticalAnalysis/y_FDR_Image.m",
    "StatisticalAnalysis/y_GRF_Threshold.m",
    "Subfunctions/y_ReadAll.m",
    "Subfunctions/y_ReadRPI.m",
    "Subfunctions/y_Write.m",
)


def _fake_stack(root: Path, marker: str) -> Settings:
    matlab = root / "MATLAB" / "bin" / "matlab.exe"
    spm = root / "spm12"
    dpabi = root / "DPABI"
    matlab.parent.mkdir(parents=True)
    spm.mkdir(parents=True)
    matlab.write_bytes(f"matlab-{marker}".encode())
    (spm / "spm.m").write_text(f"spm-{marker}", encoding="utf-8")
    for relative_path in DPABI_ENTRY_FILES:
        entry = dpabi.joinpath(*relative_path.split("/"))
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(f"{relative_path}-{marker}", encoding="utf-8")
    work_root = root / "work"
    work_root.mkdir(parents=True, exist_ok=True)
    return Settings(
        matlab_executable=matlab,
        spm_dir=spm,
        dpabi_dir=dpabi,
        allowed_work_root=work_root,
    )


def test_environment_hash_binds_entry_point_contents_without_exposing_paths(
    tmp_path: Path,
) -> None:
    first = SettingsEnvironmentLockProvider(_fake_stack(tmp_path / "first", "same")).current()
    same_content = SettingsEnvironmentLockProvider(
        _fake_stack(tmp_path / "second", "same")
    ).current()
    changed = SettingsEnvironmentLockProvider(
        _fake_stack(tmp_path / "changed", "different")
    ).current()

    assert first.snapshot.environment_hash == same_content.snapshot.environment_hash
    assert first.snapshot.environment_hash != changed.snapshot.environment_hash
    assert first.probe.ready is True
    evidence = " ".join(component.evidence or "" for component in first.probe.components)
    assert "sha256:" in evidence
    assert str(tmp_path) not in evidence


def test_environment_hash_changes_when_installed_entry_point_changes(tmp_path: Path) -> None:
    settings = _fake_stack(tmp_path / "mutable", "before")
    provider = SettingsEnvironmentLockProvider(settings)
    before = provider.current().snapshot.environment_hash

    assert settings.dpabi_dir is not None
    (settings.dpabi_dir / "dpabi.m").write_text("dpabi-after", encoding="utf-8")

    assert provider.current().snapshot.environment_hash != before


@pytest.mark.parametrize("relative_path", DPABI_ENTRY_FILES[2:])
def test_environment_hash_binds_every_metric_statistics_and_correction_entry_point(
    tmp_path: Path,
    relative_path: str,
) -> None:
    settings = _fake_stack(tmp_path / "mutable", "before")
    provider = SettingsEnvironmentLockProvider(settings)
    before = provider.current().snapshot.environment_hash

    assert settings.dpabi_dir is not None
    entry = settings.dpabi_dir.joinpath(*relative_path.split("/"))
    entry.write_text(f"{relative_path}-after", encoding="utf-8")

    assert provider.current().snapshot.environment_hash != before


def test_missing_required_dpabi_entry_point_fails_closed_without_exposing_paths(
    tmp_path: Path,
) -> None:
    settings = _fake_stack(tmp_path / "missing-entry", "same")
    assert settings.dpabi_dir is not None
    (settings.dpabi_dir / "StatisticalAnalysis" / "y_FDR_Image.m").unlink()

    lock = SettingsEnvironmentLockProvider(settings).current()
    correction_component = next(
        component
        for component in lock.probe.components
        if component.name == "DPABI FDR/GRF entry points"
    )

    assert lock.probe.ready is False
    assert correction_component.available is False
    assert "y_FDR_Image.m" in (correction_component.evidence or "")
    assert str(tmp_path) not in (correction_component.evidence or "")


def test_unconfigured_scientific_stack_fails_closed() -> None:
    settings = Settings(
        matlab_executable=None,
        spm_dir=None,
        dpabi_dir=None,
        allowed_work_root=Path("work") / "test-unconfigured-environment",
    )

    lock = SettingsEnvironmentLockProvider(settings).current()
    unavailable = {component.name for component in lock.probe.components if not component.available}

    assert lock.probe.ready is False
    assert {
        f"MATLAB {settings.matlab_version}",
        f"SPM {settings.spm_version}",
        f"DPABI {settings.dpabi_version}",
        "DPARSFA_run",
        "DPABI ALFF/ReHo entry points",
        "DPABI statistical test entry points",
        "DPABI FDR/GRF entry points",
        "DPABI statistical image I/O entry points",
    }.issubset(unavailable)


def test_user_selected_environment_is_persisted_and_versions_are_advisory(
    tmp_path: Path,
) -> None:
    settings = _fake_stack(tmp_path / "selected", "configured")
    provider = SettingsEnvironmentLockProvider(settings)

    selected = provider.update_configuration(
        EnvironmentConfigUpdate(
            matlab_executable=str(settings.matlab_executable),
            spm_dir=str(settings.spm_dir),
            dpabi_dir=str(settings.dpabi_dir),
            matlab_version="R2025a-local",
            spm_version="SPM-custom",
            dpabi_version="DPABI-local-build",
        )
    )

    assert selected.configured is True
    assert selected.matlab_version == "R2025a-local"
    assert provider.current().probe.ready is True
    component_names = {component.name for component in provider.current().probe.components}
    assert "MATLAB R2025a-local" in component_names
    assert "SPM SPM-custom" in component_names
    assert "DPABI DPABI-local-build" in component_names

    reloaded = SettingsEnvironmentLockProvider(settings)
    assert reloaded.configuration_view() == selected
    assert (
        reloaded.current().snapshot.environment_hash == provider.current().snapshot.environment_hash
    )
