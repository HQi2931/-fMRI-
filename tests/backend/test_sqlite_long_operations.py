from __future__ import annotations

import threading
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import text

from neuroagent.application.contracts import DatasetCreate, ManifestScanRequest, RunCreate
from neuroagent.application.services import NeuroAgentService

from .conftest import make_approved_plan, make_bids_dataset, make_project


def _registered_dataset(
    service: NeuroAgentService,
    source_root: Path,
    work_root: Path,
) -> tuple[str, int]:
    project = make_project(service, source_root, work_root)
    dataset_path = make_bids_dataset(source_root)
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Slow scan BIDS",
            source_path=str(dataset_path),
            expected_project_version=project.version,
        ),
        "slow-scan-dataset",
    )
    return dataset.dataset_id, dataset.version


def test_slow_scan_does_not_block_worker_heartbeat_or_event_write(
    service: NeuroAgentService,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id, dataset_version = _registered_dataset(service, source_root, work_root)
    dataset = service.get_dataset(dataset_id)
    project = service.get_project(dataset.project_id)
    plan = make_approved_plan(service, project.project_id)
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
        ),
        "slow-scan-heartbeat-run",
    )
    claimed = service.repository.claim_next_job("heartbeat-owner", lease_seconds=30)
    assert claimed is not None

    scan_started = threading.Event()
    release_scan = threading.Event()
    scan_errors: list[BaseException] = []
    writer_errors: list[BaseException] = []
    writer_done = threading.Event()
    original_inspect = service.dataset_inspector.inspect

    def slow_inspect(source_path: Path) -> dict[str, object]:
        scan_started.set()
        if not release_scan.wait(timeout=10):
            raise RuntimeError("test did not release slow scan")
        return original_inspect(source_path)

    monkeypatch.setattr(service.dataset_inspector, "inspect", slow_inspect)

    def scan() -> None:
        try:
            service.inspect_dataset(
                dataset_id,
                ManifestScanRequest(expected_dataset_version=dataset_version),
                "slow-scan-idempotency-key",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            scan_errors.append(exc)

    def write_while_scanning() -> None:
        try:
            renewed = service.repository.renew_job_lease(
                claimed["job_id"], "heartbeat-owner", lease_seconds=30
            )
            if not renewed:
                raise AssertionError("worker lease was not renewable")
            service.repository.append_event(
                project_id=project.project_id,
                run_id=run.run_id,
                event_type="ConcurrentWriterProbe",
                severity="info",
                payload={"synthetic": True},
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            writer_errors.append(exc)
        finally:
            writer_done.set()

    scan_thread = threading.Thread(target=scan)
    writer_thread = threading.Thread(target=write_while_scanning)
    scan_thread.start()
    assert scan_started.wait(timeout=2)
    writer_thread.start()
    completed_without_waiting_for_scan = writer_done.wait(timeout=1)
    release_scan.set()
    writer_thread.join(timeout=7)
    scan_thread.join(timeout=7)

    assert completed_without_waiting_for_scan
    assert not writer_thread.is_alive()
    assert not scan_thread.is_alive()
    assert writer_errors == []
    assert scan_errors == []
    assert any(
        event.event_type == "ConcurrentWriterProbe" for event in service.list_run_events(run.run_id)
    )


def test_scan_prepare_failure_releases_reservation_for_same_key_retry(
    service: NeuroAgentService,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id, dataset_version = _registered_dataset(service, source_root, work_root)
    original_inspect = service.dataset_inspector.inspect
    calls = 0

    def fail_once(source_path: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated scan failure")
        return original_inspect(source_path)

    monkeypatch.setattr(service.dataset_inspector, "inspect", fail_once)
    request = ManifestScanRequest(expected_dataset_version=dataset_version)
    with pytest.raises(RuntimeError, match="simulated scan failure"):
        service.inspect_dataset(dataset_id, request, "retry-same-scan-key")

    manifest = service.inspect_dataset(dataset_id, request, "retry-same-scan-key")
    assert manifest.dataset_id == dataset_id
    assert calls == 2


def test_prepared_finalize_and_idempotency_response_commit_atomically(
    service: NeuroAgentService,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_id, dataset_version = _registered_dataset(service, source_root, work_root)
    original_complete = service.repository.complete_idempotent_request
    calls = 0

    def crash_once(
        scope: str,
        key: str,
        request_hash: str,
        owner_token: str,
        response: BaseModel,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated response persistence crash")
        original_complete(scope, key, request_hash, owner_token, response)

    monkeypatch.setattr(service.repository, "complete_idempotent_request", crash_once)
    request = ManifestScanRequest(expected_dataset_version=dataset_version)
    with pytest.raises(RuntimeError, match="response persistence crash"):
        service.inspect_dataset(dataset_id, request, "atomic-scan-finalize")

    dataset = service.get_dataset(dataset_id)
    assert dataset.current_manifest_id is None
    assert not any(
        event.event_type == "DatasetInspected"
        for event in service.list_project_events(dataset.project_id)
    )
    with service.database.session_factory() as session:
        assert session.scalar(text("SELECT COUNT(*) FROM manifest_revisions")) == 0

    created = service.inspect_dataset(dataset_id, request, "atomic-scan-finalize")
    repeated = service.inspect_dataset(dataset_id, request, "atomic-scan-finalize")
    assert repeated == created
    assert (
        sum(
            event.event_type == "DatasetInspected"
            for event in service.list_project_events(dataset.project_id)
        )
        == 1
    )
    with service.database.session_factory() as session:
        assert session.scalar(text("SELECT COUNT(*) FROM manifest_revisions")) == 1
