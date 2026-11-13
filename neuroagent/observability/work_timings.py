"""Per-request measurements kept outside frozen scientific configuration."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter


@dataclass
class WorkTimings:
    clock: Callable[[], float] = perf_counter
    stages: dict[str, tuple[float, int]] = field(default_factory=dict)

    def add(self, stage: str, seconds: float) -> None:
        duration, calls = self.stages.get(stage, (0.0, 0))
        self.stages[stage] = (duration + max(0.0, seconds), calls + 1)

    def snapshot(self) -> list[dict[str, str | float | int]]:
        return [
            {"stage": stage, "duration_ms": round(duration * 1000, 3), "calls": calls}
            for stage, (duration, calls) in sorted(self.stages.items())
        ]


_CURRENT: ContextVar[WorkTimings | None] = ContextVar("work_timings", default=None)


@contextmanager
def capture_work_timings() -> Iterator[WorkTimings]:
    timings = WorkTimings()
    token = _CURRENT.set(timings)
    try:
        yield timings
    finally:
        _CURRENT.reset(token)


@contextmanager
def measure_work_stage(stage: str) -> Iterator[None]:
    timings = _CURRENT.get()
    if timings is None:
        yield
        return
    started = timings.clock()
    try:
        yield
    finally:
        timings.add(stage, timings.clock() - started)
