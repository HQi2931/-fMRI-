"""Verify Work interpretation against existing synthetic real-DPABI results.

Clones an owned acceptance database into a new output directory. Existing
images, reports, runs and approvals are read only. A labelled Mock Provider
verifies the public Work path, tool context, permissions and persisted audit;
this does not claim to validate a real model's scientific reasoning.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from neuroagent.agent.models import ModelCapability, ModelProfile, ProviderResponse, ToolCall
from neuroagent.agent.providers import MockProvider
from neuroagent.api.app import create_app
from neuroagent.application.contracts.conversations import (
    ConversationCreate,
)
from neuroagent.application.settings import Settings
from neuroagent.application.statistical_result_facts import aggregate_roi, render_result_readout
from neuroagent.bootstrap import build_service
from neuroagent.infrastructure.environment import SettingsEnvironmentLockProvider


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acceptance-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--without-tools", action="store_true")
    args = parser.parse_args()
    source_root = args.acceptance_root.resolve(strict=True)
    if not (
        (source_root / "summary.json").is_file() or (source_root / "verification.json").is_file()
    ):
        parser.error("an existing synthetic acceptance marker is required")
    database_path = source_root / "acceptance.sqlite"
    root = args.output_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True) as original:
        states = original.execute(
            "SELECT run_id,state FROM workflow_runs ORDER BY run_id"
        ).fetchall()
        if any(
            state not in {"succeeded", "failed_terminal", "cancelled", "timed_out"}
            for _, state in states
        ):
            parser.error("the acceptance database still contains unfinished work")
        with sqlite3.connect(root / "interpretation.sqlite") as cloned:
            original.backup(cloned)
    source_hashes = {
        path: _hash(path) for path in (source_root / "source").rglob("*") if path.is_file()
    }
    secret_file = root / "mock-secrets.env"
    secret_file.write_text("INTERPRETATION_DEMO_API_KEY=mock-local-test-key\n", encoding="utf-8")
    configuration = SettingsEnvironmentLockProvider(Settings.from_env()).configuration()
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{(root / 'interpretation.sqlite').as_posix()}",
        allowed_source_roots=[source_root / "source"],
        allowed_work_root=source_root / "work",
        matlab_executable=configuration.matlab_executable,
        spm_dir=configuration.spm_dir,
        dpabi_dir=configuration.dpabi_dir,
        enable_real_execution=False,
        serve_frontend=False,
        rag_db_dir=None,
        secrets_file=secret_file,
        redaction_salt="synthetic-statistical-interpretation-test-salt",
        agent_max_tool_steps=6,
        agent_max_tool_tokens=50_000,
    )
    provider = MockProvider([])
    service = build_service(settings, providers={"openai-compatible": provider})
    try:
        run = service.runs.get_run(args.run_id)
        assert run.state.value == "succeeded"
        results = service.runs.list_statistical_results(
            project_id=run.project_id, run_id=run.run_id
        )
        assert len(results) == 1 and results[0].mode == "real" and not results[0].non_scientific
        result_id = results[0].result_id
        detail = service.runs.get_statistical_result(result_id)
        assert detail.method_summary is not None
        assert detail.method_summary.correction_evidence_status == "verified"
        facts = service._statistical_result_facts(result_id)
        assert facts["status"] == "succeeded"
        cluster_id = facts["clusters"][0]["cluster_id"] if facts["clusters"] else None
        final_answer = render_result_readout(facts)
        if facts["localization_status"] == "pending_atlas_verification":
            localization = service.runs.localize_statistical_result(
                result_id, facts["frozen_atlas"]["atlas_id"]
            )
            peak = localization.clusters[0]
            coverage = "；".join(
                f"{region.label} {region.proportion:.1%}" for region in peak.coverage
            )
            final_answer += f"\n峰值标签：{peak.label}；整簇覆盖：{coverage}。"
        roi_facts = aggregate_roi(service.runs.statistical_cluster_roi(result_id, cluster_id))
        if roi_facts["groups"]:
            final_answer += (
                "\nROI 描述："
                + "；".join(
                    f"{group['group'] or group['condition'] or '全体'} n={group['count']}，"
                    f"均值={group['mean']:.5g}"
                    for group in roi_facts["groups"]
                )
                + "。仅用于同一显著簇的描述，不是独立验证。"
            )
        responses = [
            ProviderResponse(
                content="",
                model="mock-statistical-interpretation",
                finish_reason="tool_calls",
                tool_calls=(ToolCall(call_id=f"call-{index}", name=name, arguments=arguments),),
            )
            for index, (name, arguments) in enumerate(
                (
                    ("get_statistical_result_detail", {"result_id": result_id}),
                    ("get_statistical_result_localization", {"result_id": result_id}),
                    (
                        "get_statistical_cluster_summary",
                        {"result_id": result_id, "cluster_id": cluster_id},
                    ),
                ),
                1,
            )
        ]
        if args.without_tools:
            responses = []
        provider._responses.extend(
            [
                *responses,
                ProviderResponse(
                    content=json.dumps(
                        {"answer": final_answer, "action": "conversation", "analysis_patch": {}},
                        ensure_ascii=False,
                    ),
                    model="mock-statistical-interpretation",
                ),
            ]
        )
        profile = ModelProfile(
            id="mock-statistical-interpretation",
            provider="openai-compatible",
            base_url="https://example.invalid/v1",
            model="mock-statistical-interpretation",
            api_key_env="INTERPRETATION_DEMO_API_KEY",
            capabilities=frozenset(
                {ModelCapability.JSON_OBJECT}
                if args.without_tools
                else {ModelCapability.JSON_OBJECT, ModelCapability.TOOL_USE}
            ),
        )
        service.repository.create_model_profile(profile)
        conversation = service.conversations.create_conversation(
            ConversationCreate(
                mode="work", project_id=run.project_id, preferred_profile_id=profile.id
            ),
            "interpretation-conversation",
        )
        with TestClient(create_app(service=service)) as client:
            response = client.post(
                f"/api/v1/conversations/{conversation.conversation_id}/turns",
                headers={"Idempotency-Key": "interpretation-turn"},
                json={
                    "content": "解释当前统计结果、脑区定位及 ROI 描述。",
                    "agent_role": "analysis",
                    "target_run_id": run.run_id,
                    "preferred_profile_id": profile.id,
                },
            )
            assert response.status_code == 201, response.status_code
            body = response.json()
        updated = body["conversation"]
        assert updated["messages"][-1]["content"] == final_answer
        assert updated["messages"][-1]["payload"]["agent_role"] == "analysis"
        request_text = json.dumps(provider.requests, default=str, ensure_ascii=False)
        assert "analysis_result_facts" in request_text
        contexts = [
            json.loads(message["content"])
            for _, messages in provider.requests
            for message in messages
            if message.get("role") == "user"
        ]
        model_facts = contexts[0]["work_context"]["analysis_result_facts"]["results"][0]
        assert model_facts["roi_summary"]["status"] == roi_facts["status"]
        assert model_facts["roi_summary"].get("cluster_id") == cluster_id
        assert model_facts["localization"]["status"] == (
            "localized"
            if facts["localization_status"] == "pending_atlas_verification"
            else facts["localization_status"]
        )
        assert "report_json" not in request_text and '"mean_value"' not in request_text
        assert "sub-01" not in request_text and str(source_root) not in request_text
        tool_messages = [
            message
            for _, messages in provider.requests
            for message in messages
            if message.get("role") == "tool"
        ]
        assert bool(tool_messages) is (not args.without_tools)
        tool_text = json.dumps(tool_messages, ensure_ascii=False)
        if not args.without_tools:
            assert "untrusted_content" in tool_text
        assert (
            "report_json" not in tool_text
            and '"mean_value"' not in tool_text
            and "sub-01" not in tool_text
        )
        assert (
            len(service.runs.list_statistical_results(project_id=run.project_id, run_id=run.run_id))
            == 1
        )
        assert service.runs.get_run(run.run_id).state.value == "succeeded"
        assert all(_hash(path) == digest for path, digest in source_hashes.items())
        with sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True) as original:
            assert (
                original.execute(
                    "SELECT run_id,state FROM workflow_runs ORDER BY run_id"
                ).fetchall()
                == states
            )
        evidence = {
            "mode": "Mock interpretation of existing real DPABI evidence",
            "model_tool_use": not args.without_tools,
            "source_run_id": run.run_id,
            "conversation_id": conversation.conversation_id,
            "result_bundle_hash": detail.bundle_hash,
            "facts": facts,
            "model_facts": model_facts,
            "answer": final_answer,
            "tool_messages": tool_messages,
            "tool_audit": updated["tool_calls"],
            "source_files_unchanged": True,
            "existing_run_states_unchanged": True,
        }
        (root / "verification.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )
        print("STATISTICAL_INTERPRETATION_ACCEPTANCE_PASS", flush=True)
    finally:
        service.close()


if __name__ == "__main__":
    main()
