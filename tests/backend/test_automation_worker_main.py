from __future__ import annotations

import sys
from types import SimpleNamespace

from neuroagent.workflow import worker_main


def test_worker_main_runs_one_deterministic_task_reconciliation_on_startup(monkeypatch) -> None:
    calls: list[tuple[object, ...]] = []

    class FakeAutomation:
        def reconcile_next_due_task(self, worker_id: str, **kwargs):
            calls.append(("reconcile", worker_id, kwargs))
            return None

    class FakeWorker:
        worker_id = "worker-for-automation"

        def run_once(self) -> bool:
            calls.append(("workflow-job",))
            return False

    class FakeService:
        settings = SimpleNamespace(automation_claim_lease_seconds=17)
        automation = FakeAutomation()

        def close(self) -> None:
            calls.append(("close",))

    monkeypatch.setattr(worker_main, "build_service", lambda: FakeService())
    monkeypatch.setattr(worker_main, "build_worker", lambda _service: FakeWorker())
    monkeypatch.setattr(
        worker_main,
        "process_one_analysis_outbox",
        lambda _service, _worker_id: calls.append(("analysis-outbox",)) or "deferred",
    )
    monkeypatch.setattr(sys, "argv", ["rsfmri-worker", "--once"])

    worker_main.main()

    assert calls == [
        ("workflow-job",),
        (
            "reconcile",
            "worker-for-automation",
            {"lease_seconds": 17, "cooldown_seconds": 0.5},
        ),
        ("analysis-outbox",),
        ("close",),
    ]
