"""Read-only MATLAB/SPM/DPABI environment probing."""

from __future__ import annotations

import hashlib
from pathlib import Path

from neuroagent.application.contracts import EnvironmentComponent, EnvironmentProbeView
from neuroagent.application.environment_lock import EnvironmentLock
from neuroagent.application.hashing import content_hash
from neuroagent.application.settings import Settings
from neuroagent.skills.models import EnvironmentSnapshot

_DPABI_COMPONENTS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("DPABI V8.2_240510", (("dpabi.m", "dpabi.m"),)),
    ("DPARSFA_run", (("DPARSFA_run.m", "DPARSF/DPARSFA_run.m"),)),
    (
        "DPABI ALFF/ReHo entry points",
        (
            ("y_alff_falff.m", "DPARSF/Subfunctions/y_alff_falff.m"),
            ("y_reho.m", "DPARSF/Subfunctions/y_reho.m"),
        ),
    ),
    (
        "DPABI statistical test entry points",
        (
            ("y_TTest1_Image.m", "StatisticalAnalysis/y_TTest1_Image.m"),
            ("y_TTest2_Image.m", "StatisticalAnalysis/y_TTest2_Image.m"),
            ("y_TTestPaired_Image.m", "StatisticalAnalysis/y_TTestPaired_Image.m"),
            ("y_GroupAnalysis_Image.m", "StatisticalAnalysis/y_GroupAnalysis_Image.m"),
        ),
    ),
    (
        "DPABI FDR/GRF entry points",
        (
            ("y_FDR_Image.m", "StatisticalAnalysis/y_FDR_Image.m"),
            ("y_GRF_Threshold.m", "StatisticalAnalysis/y_GRF_Threshold.m"),
        ),
    ),
    (
        "DPABI statistical image I/O entry points",
        (
            ("y_ReadAll.m", "Subfunctions/y_ReadAll.m"),
            ("y_ReadRPI.m", "Subfunctions/y_ReadRPI.m"),
            ("y_Write.m", "Subfunctions/y_Write.m"),
        ),
    ),
)


def _configured_file(root: Path | None, relative_path: str) -> Path | None:
    """Resolve a required V8.2 file without accepting a different tree layout."""

    if root is None or not root.is_dir():
        return None
    candidate = root.joinpath(*relative_path.split("/"))
    return candidate if candidate.is_file() else None


def _fingerprint(files: tuple[tuple[str, Path | None], ...]) -> str | None:
    if any(path is None or not path.is_file() for _, path in files):
        return None
    digest = hashlib.sha256()
    for label, path in sorted(files, key=lambda item: item[0]):
        assert path is not None
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()


def _component(name: str, files: tuple[tuple[str, Path | None], ...]) -> EnvironmentComponent:
    fingerprint = _fingerprint(files)
    if fingerprint is not None:
        evidence = f"entry point fingerprint sha256:{fingerprint}; no MATLAB process started"
    else:
        missing = ", ".join(label for label, path in files if path is None or not path.is_file())
        evidence = f"required entry points unavailable: {missing}; no MATLAB process started"
    return EnvironmentComponent(name=name, available=fingerprint is not None, evidence=evidence)


def probe_environment(settings: Settings) -> EnvironmentProbeView:
    matlab_path = settings.matlab_executable
    spm_path = settings.spm_dir
    dpabi_path = settings.dpabi_dir
    spm_entry = None if spm_path is None else spm_path / "spm.m"
    project_root = Path(__file__).resolve().parents[2]
    adapter_files = (
        ("dpabi_v82.py", project_root / "neuroagent" / "tools" / "dpabi_v82.py"),
        ("matlab.py", project_root / "neuroagent" / "execution" / "matlab.py"),
        *tuple(
            (f"templates/{path.name}", path)
            for path in sorted((project_root / "matlab" / "templates").glob("*.tmpl"))
        ),
    )
    checks: list[tuple[str, tuple[tuple[str, Path | None], ...]]] = [
        ("MATLAB R2023b", (("matlab.exe", matlab_path),)),
        ("SPM12", (("spm.m", spm_entry),)),
        *(
            (
                name,
                tuple(
                    (label, _configured_file(dpabi_path, relative_path))
                    for label, relative_path in entries
                ),
            )
            for name, entries in _DPABI_COMPONENTS
        ),
        ("rs-fMRI adapter", adapter_files),
    ]
    components = [_component(name, files) for name, files in checks]
    snapshot = [component.model_dump(mode="json") for component in components]
    return EnvironmentProbeView(
        ready=all(component.available for component in components),
        environment_hash=content_hash(snapshot),
        components=components,
    )


class SettingsEnvironmentLockProvider:
    """Build a path-free lock from configured versions and read-only probes."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def current(self) -> EnvironmentLock:
        probe = probe_environment(self._settings)
        environment_hash = content_hash(
            {
                "matlab_version": self._settings.matlab_version,
                "spm_version": self._settings.spm_version,
                "dpabi_version": self._settings.dpabi_version,
                "adapter_version": self._settings.adapter_version,
                "probe_hash": probe.environment_hash,
            }
        )
        snapshot = EnvironmentSnapshot(
            matlab_version=self._settings.matlab_version,
            spm_version=self._settings.spm_version,
            dpabi_version=self._settings.dpabi_version,
            adapter_version=self._settings.adapter_version,
            environment_hash=environment_hash,
        )
        return EnvironmentLock(
            snapshot=snapshot,
            probe=probe.model_copy(update={"environment_hash": environment_hash}),
        )
