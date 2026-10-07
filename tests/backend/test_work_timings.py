"""Measurements remain request-scoped and never alter scientific configuration."""

import asyncio

import pytest

from neuroagent.observability.work_timings import capture_work_timings, measure_work_stage


def test_measurement_is_exception_safe_and_does_not_invent_absent_stages():
    with capture_work_timings() as timings:
        moments = iter([1.0, 1.025])
        timings.clock = lambda: next(moments)
        with pytest.raises(ValueError), measure_work_stage("validation"):
            raise ValueError("invalid input")
        assert timings.snapshot() == [{"stage": "validation", "duration_ms": 25.0, "calls": 1}]
    with measure_work_stage("model_response"):
        pass
    assert len(timings.snapshot()) == 1


@pytest.mark.asyncio
async def test_concurrent_work_requests_have_independent_measurements():
    async def request(stage):
        with capture_work_timings() as timings:
            with measure_work_stage(stage):
                await asyncio.sleep(0)
            return timings.snapshot()

    first, second = await asyncio.gather(request("file_tools"), request("model_response"))
    assert [item["stage"] for item in first] == ["file_tools"]
    assert [item["stage"] for item in second] == ["model_response"]
