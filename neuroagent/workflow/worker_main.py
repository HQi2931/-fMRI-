"""Separate local worker process entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import threading
import time
from uuid import uuid4

from neuroagent.application.services import ApplicationServices
from neuroagent.bootstrap import build_service, build_worker

logger = logging.getLogger(__name__)


def process_one_analysis_outbox(service: ApplicationServices, worker_id: str) -> str | None:
    """Wait for at most one READY continuation attempt, including its model call."""
    return asyncio.run(
        service.conversations.consume_next_analysis_outbox(
            worker_id,
            lease_seconds=service.settings.automation_claim_lease_seconds,
        )
    )


def process_one_result_interpretation_outbox(
    service: ApplicationServices, worker_id: str
) -> str | None:
    """Attempt at most one independently leased statistical result continuation."""
    return asyncio.run(
        service.conversations.consume_next_result_interpretation_outbox(
            worker_id,
            lease_seconds=service.settings.automation_claim_lease_seconds,
        )
    )


def _analysis_outbox_worker(stop: threading.Event, poll_seconds: float) -> None:
    """Run model-backed continuations in an isolated worker thread/service."""
    service = build_service()
    worker_id = f"analysis-outbox-{uuid4().hex}"

    async def run() -> None:
        while not stop.is_set():
            try:
                outcome = await service.conversations.consume_next_analysis_outbox(
                    worker_id,
                    lease_seconds=service.settings.automation_claim_lease_seconds,
                )
                if outcome is not None:
                    logger.info("Analysis outbox attempt finished: outcome=%s", outcome)
            except Exception:
                logger.exception("Analysis outbox worker pass failed; it will be retried")
                outcome = None
            if outcome is None:
                await asyncio.to_thread(stop.wait, poll_seconds)

    try:
        asyncio.run(run())
    finally:
        service.close()


def _result_interpretation_outbox_worker(stop: threading.Event, poll_seconds: float) -> None:
    """Poll result interpretation using its own service, lease and worker identity."""
    service = build_service()
    worker_id = f"result-interpretation-outbox-{uuid4().hex}"

    async def run() -> None:
        while not stop.is_set():
            try:
                outcome = await service.conversations.consume_next_result_interpretation_outbox(
                    worker_id,
                    lease_seconds=service.settings.automation_claim_lease_seconds,
                )
                if outcome is not None:
                    logger.info(
                        "Result interpretation outbox attempt finished: outcome=%s", outcome
                    )
            except Exception:
                logger.exception("Result interpretation outbox pass failed; it will be retried")
                outcome = None
            if outcome is None:
                await asyncio.to_thread(stop.wait, poll_seconds)

    try:
        asyncio.run(run())
    finally:
        service.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local NeuroAgent worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "claim at most one Workflow job, reconcile at most one WorkTask, and attempt at most "
            "one READY analysis proposal outbox and one READY result interpretation outbox "
            "(awaiting each claimed model call)"
        ),
    )
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    args = parser.parse_args()
    service = build_service()
    worker = build_worker(service)
    stop_outbox = threading.Event()
    outbox_thread: threading.Thread | None = None
    result_interpretation_thread: threading.Thread | None = None
    if not args.once:
        outbox_thread = threading.Thread(
            target=_analysis_outbox_worker,
            args=(stop_outbox, max(args.poll_seconds, 0.05)),
            name="analysis-outbox-worker",
            daemon=True,
        )
        outbox_thread.start()
        result_interpretation_thread = threading.Thread(
            target=_result_interpretation_outbox_worker,
            args=(stop_outbox, max(args.poll_seconds, 0.05)),
            name="result-interpretation-outbox-worker",
            daemon=True,
        )
        result_interpretation_thread.start()
    try:
        while True:
            handled = worker.run_once()
            try:
                reconciliation = service.automation.reconcile_next_due_task(
                    worker.worker_id,
                    lease_seconds=service.settings.automation_claim_lease_seconds,
                    cooldown_seconds=max(args.poll_seconds, 0.05),
                )
                if reconciliation is not None:
                    logger.info(
                        "Reconciled WorkTask %s: readiness=%s, version=%d",
                        reconciliation.task.task_id,
                        reconciliation.readiness.value,
                        reconciliation.task.version,
                    )
            except Exception:
                logger.exception("WorkTask reconciliation pass failed; it will be retried")
            if args.once:
                outcome = process_one_analysis_outbox(
                    service, f"analysis-outbox-once-{uuid4().hex}"
                )
                if outcome is not None:
                    logger.info("Analysis outbox one-shot attempt finished: outcome=%s", outcome)
                result_outcome = process_one_result_interpretation_outbox(
                    service, f"result-interpretation-once-{uuid4().hex}"
                )
                if result_outcome is not None:
                    logger.info(
                        "Result interpretation one-shot attempt finished: outcome=%s",
                        result_outcome,
                    )
                return
            if not handled:
                time.sleep(max(args.poll_seconds, 0.05))
    except KeyboardInterrupt:
        return
    finally:
        stop_outbox.set()
        if outbox_thread is not None:
            outbox_thread.join(timeout=1)
        if result_interpretation_thread is not None:
            result_interpretation_thread.join(timeout=1)
        service.close()


if __name__ == "__main__":
    main()
