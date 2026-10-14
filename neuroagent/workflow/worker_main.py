"""Separate local worker process entry point."""

from __future__ import annotations

import argparse
import logging
import time

from neuroagent.bootstrap import build_service, build_worker

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local NeuroAgent worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="claim at most one Workflow job and reconcile at most one WorkTask",
    )
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    args = parser.parse_args()
    service = build_service()
    worker = build_worker(service)
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
                return
            if not handled:
                time.sleep(max(args.poll_seconds, 0.05))
    except KeyboardInterrupt:
        return
    finally:
        service.close()


if __name__ == "__main__":
    main()
