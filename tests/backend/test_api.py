from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from neuroagent.api import create_app
from neuroagent.application.contracts import RunCreate
from neuroagent.application.services import NeuroAgentService
from neuroagent.bootstrap import build_worker

from .conftest import make_approved_plan, make_project


def test_health_openapi_and_error_envelope(service: NeuroAgentService) -> None:
    with TestClient(create_app(service=service)) as client:
        assert client.get("/api/v1/health").json() == {"status": "ok", "database": "ok"}
        schema = client.get("/api/v1/openapi.json").json()
        assert "/api/v1/runs/{run_id}/events" in schema["paths"]
        contracts = schema["components"]["schemas"]
        assert "environment" not in contracts["SkillPlanResolveRequest"]["properties"]
        assert "input_artifact" not in contracts["SkillPlanIntent"]["properties"]
        assert "base_cfg_artifact_id" not in contracts["SkillPlanIntent"]["properties"]
        assert "environment_hash" not in contracts["StatisticalDesignCreate"]["properties"]
        response = client.get("/api/v1/projects/not-found")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
        assert response.json()["error"]["trace_id"]


def test_write_endpoint_requires_idempotency_header(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    with TestClient(create_app(service=service)) as client:
        response = client.post(
            "/api/v1/projects",
            json={
                "name": "missing header",
                "source_roots": [str(source_root)],
                "work_root": str(work_root / "project"),
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "request_validation_failed"


def test_environment_probe_never_starts_matlab(service: NeuroAgentService) -> None:
    with TestClient(create_app(service=service)) as client:
        response = client.get("/api/v1/environment/probe")
        assert response.status_code == 200
        payload = response.json()
        assert payload["ready"] is False
        assert all(
            "no MATLAB process started" in item["evidence"] for item in payload["components"]
        )
        assert all("configured_path" not in item for item in payload["components"])
        assert str(service.settings.matlab_executable) not in response.text


def test_unexpected_error_uses_safe_envelope(
    service: NeuroAgentService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode() -> None:
        raise RuntimeError("private implementation detail")

    monkeypatch.setattr(service, "health", explode)
    with TestClient(create_app(service=service), raise_server_exceptions=False) as client:
        response = client.get("/api/v1/health", headers={"X-Trace-ID": "test-trace-id"})

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_server_error",
            "message": "服务发生未预期错误, 请使用 trace ID 查看本地日志。",
            "details": {},
            "trace_id": "test-trace-id",
        }
    }
    assert "private implementation detail" not in response.text


def test_sse_returns_persisted_run_events_and_supports_resume_cursor(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
        ),
        "sse-run",
    )
    build_worker(service, worker_id="sse-worker").run_once()
    with TestClient(create_app(service=service)) as client:
        response = client.get(f"/api/v1/runs/{run.run_id}/events?once=true")
        assert response.status_code == 200
        assert "event: RunQueued" in response.text
        assert "event: WorkflowTransitioned" in response.text
        last_id = service.list_run_events(run.run_id)[-1].event_id
        resumed = client.get(
            f"/api/v1/runs/{run.run_id}/events?once=true",
            headers={"Last-Event-ID": str(last_id)},
        )
        assert resumed.status_code == 200
        assert resumed.text == ""
