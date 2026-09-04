"""Read-only MATLAB/SPM/DPABI environment probing."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from neuroagent.application.contracts import (
    EnvironmentComponent,
    EnvironmentConfigUpdate,
    EnvironmentConfigView,
    EnvironmentProbeView,
)
from neuroagent.application.environment_lock import (
    EnvironmentConfiguration,
    EnvironmentLock,
)
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


class LocalEnvironmentConfigStore:
    """Persist user-selected software paths outside the repository tree."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._config_path = settings.allowed_work_root / ".rsfmri-environment.json"

    def configuration(self) -> EnvironmentConfiguration:
        values: dict[str, object] = {
            "matlab_executable": self._settings.matlab_executable,
            "spm_dir": self._settings.spm_dir,
            "dpabi_dir": self._settings.dpabi_dir,
            "matlab_version": self._settings.matlab_version,
            "spm_version": self._settings.spm_version,
            "dpabi_version": self._settings.dpabi_version,
        }
        if self._config_path.is_file():
            try:
                stored = json.loads(self._config_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("saved scientific environment configuration is invalid") from exc
            if not isinstance(stored, dict):
                raise ValueError("saved scientific environment configuration is invalid")
            values.update(stored)
        # Startup must remain fail-closed but must not crash merely because a
        # previously selected installation was moved or uninstalled.  The
        # probe will report the missing entry point and the UI can repair the
        # selection.  Strict existence checks are reserved for PUT updates.
        return self._validated_configuration(
            values,
            require_existing=False,
            adapter_version=self._settings.adapter_version,
        )

    def update(self, request: EnvironmentConfigUpdate) -> EnvironmentConfigView:
        configuration = self._validated_configuration(
            request.model_dump(mode="python"),
            require_existing=True,
            adapter_version=self._settings.adapter_version,
        )
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "matlab_executable": str(configuration.matlab_executable)
            if configuration.matlab_executable
            else None,
            "spm_dir": str(configuration.spm_dir) if configuration.spm_dir else None,
            "dpabi_dir": str(configuration.dpabi_dir) if configuration.dpabi_dir else None,
            "matlab_version": configuration.matlab_version,
            "spm_version": configuration.spm_version,
            "dpabi_version": configuration.dpabi_version,
        }
        handle, temporary = tempfile.mkstemp(
            prefix=".rsfmri-environment-",
            suffix=".tmp",
            dir=self._config_path.parent,
            text=True,
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._config_path)
        except BaseException:
            with suppress(OSError):
                Path(temporary).unlink(missing_ok=True)
            raise
        return self.view(configuration)

    def view(self, configuration: EnvironmentConfiguration | None = None) -> EnvironmentConfigView:
        value = configuration or self.configuration()
        return EnvironmentConfigView(
            matlab_executable=str(value.matlab_executable) if value.matlab_executable else None,
            spm_dir=str(value.spm_dir) if value.spm_dir else None,
            dpabi_dir=str(value.dpabi_dir) if value.dpabi_dir else None,
            matlab_version=value.matlab_version,
            spm_version=value.spm_version,
            dpabi_version=value.dpabi_version,
            configured=all(
                path is not None
                for path in (value.matlab_executable, value.spm_dir, value.dpabi_dir)
            ),
        )

    @staticmethod
    def _validated_configuration(
        values: dict[str, object], *, require_existing: bool, adapter_version: str
    ) -> EnvironmentConfiguration:
        return EnvironmentConfiguration(
            matlab_executable=_selected_path(
                values.get("matlab_executable"), expect_file=True, require_existing=require_existing
            ),
            spm_dir=_selected_path(
                values.get("spm_dir"), expect_file=False, require_existing=require_existing
            ),
            dpabi_dir=_selected_path(
                values.get("dpabi_dir"), expect_file=False, require_existing=require_existing
            ),
            matlab_version=_version_label(values.get("matlab_version")),
            spm_version=_version_label(values.get("spm_version")),
            dpabi_version=_version_label(values.get("dpabi_version")),
            adapter_version=adapter_version,
        )


def _selected_path(value: object, *, expect_file: bool, require_existing: bool) -> Path | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, (str, Path)):
        raise ValueError("scientific environment paths must be strings")
    path = Path(value).expanduser().resolve(strict=require_existing)
    if require_existing and expect_file and not path.is_file():
        raise ValueError("MATLAB executable path must point to a file")
    if require_existing and not expect_file and not path.is_dir():
        raise ValueError("SPM and DPABI paths must point to directories")
    return path


def _version_label(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "unspecified"
    return value.strip()


def _configured_file(root: Path | None, relative_path: str) -> Path | None:
    """Resolve an entry point below the user-selected DPABI directory."""

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


def probe_environment(
    settings: Settings,
    configuration: EnvironmentConfiguration | None = None,
) -> EnvironmentProbeView:
    selected = configuration or EnvironmentConfiguration(
        matlab_executable=settings.matlab_executable,
        spm_dir=settings.spm_dir,
        dpabi_dir=settings.dpabi_dir,
        matlab_version=settings.matlab_version,
        spm_version=settings.spm_version,
        dpabi_version=settings.dpabi_version,
        adapter_version=settings.adapter_version,
    )
    matlab_path = selected.matlab_executable
    spm_path = selected.spm_dir
    dpabi_path = selected.dpabi_dir
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
        (f"MATLAB {selected.matlab_version}", (("matlab executable", matlab_path),)),
        (f"SPM {selected.spm_version}", (("spm.m", spm_entry),)),
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
        (f"rs-fMRI adapter {selected.adapter_version}", adapter_files),
    ]
    components = [
        _component(
            f"DPABI {selected.dpabi_version}" if name == "DPABI V8.2_240510" else name,
            files,
        )
        for name, files in checks
    ]
    snapshot = [component.model_dump(mode="json") for component in components]
    return EnvironmentProbeView(
        ready=all(component.available for component in components),
        environment_hash=content_hash(snapshot),
        components=components,
    )


class SettingsEnvironmentLockProvider:
    """Build a path-free lock from configured versions and read-only probes."""

    def __init__(
        self, settings: Settings, store: LocalEnvironmentConfigStore | None = None
    ) -> None:
        self._settings = settings
        self._store = store or LocalEnvironmentConfigStore(settings)

    def configuration(self) -> EnvironmentConfiguration:
        return self._store.configuration()

    def configuration_view(self) -> EnvironmentConfigView:
        return self._store.view()

    def update_configuration(self, request: EnvironmentConfigUpdate) -> EnvironmentConfigView:
        return self._store.update(request)

    def current(self) -> EnvironmentLock:
        configuration = self.configuration()
        probe = probe_environment(self._settings, configuration)
        environment_hash = content_hash(
            {
                "matlab_version": configuration.matlab_version,
                "spm_version": configuration.spm_version,
                "dpabi_version": configuration.dpabi_version,
                "adapter_version": configuration.adapter_version,
                "probe_hash": probe.environment_hash,
            }
        )
        snapshot = EnvironmentSnapshot(
            matlab_version=configuration.matlab_version,
            spm_version=configuration.spm_version,
            dpabi_version=configuration.dpabi_version,
            adapter_version=configuration.adapter_version,
            environment_hash=environment_hash,
        )
        return EnvironmentLock(
            snapshot=snapshot,
            probe=probe.model_copy(update={"environment_hash": environment_hash}),
            configuration=configuration,
        )
