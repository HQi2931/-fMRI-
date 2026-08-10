"""Separate local worker process entry point."""

from __future__ import annotations

import argparse
import time

from neuroagent.bootstrap import build_service, build_worker


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local NeuroAgent worker")
    parser.add_argument("--once", action="store_true", help="claim at most one job")
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    args = parser.parse_args()
    service = build_service()
    worker = build_worker(service)
    try:
        while True:
            handled = worker.run_once()
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
