"""Serve an isolated browser acceptance using a copy of approved synthetic preprocessing.

The source acceptance database and files remain read-only. Browser actions create
a fresh Work proposal, statistical confirmation, and real Worker run in the copy.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import threading
from pathlib import Path

import uvicorn

from neuroagent.agent.providers import MockProvider
from neuroagent.api import create_app
from neuroagent.application.contracts import (
    ConversationCreate,
    ModelProfileCreate,
    ModelProfileInput,
)
from neuroagent.application.settings import Settings
from neuroagent.bootstrap import build_service, build_worker
from neuroagent.infrastructure.environment import SettingsEnvironmentLockProvider


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=9091)
    parser.add_argument("--real-matlab", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if not args.real_matlab:
        parser.error("--real-matlab is required for this synthetic browser acceptance")
    source = args.source_root.resolve()
    proof = json.loads((source / "verification.json").read_text(encoding="utf-8"))
    assert (
        proof["status"] == "pass"
        and proof["mode"] == "real_matlab_synthetic_data_service_integration"
    )
    root = args.output_root.resolve()
    database = root / "acceptance.sqlite"
    if args.resume:
        if not (root / "browser-setup.json").is_file() or not database.is_file():
            parser.error("--resume requires this script's own prior browser acceptance")
    else:
        root.mkdir(parents=True, exist_ok=False)
        shutil.copytree(source / "work", root / "work")
        with (
            sqlite3.connect(
                f"{(source / 'acceptance.sqlite').as_uri()}?mode=ro", uri=True
            ) as original,
            sqlite3.connect(database) as copy,
        ):
            original.backup(copy)
            copy.execute(
                "UPDATE projects SET work_root=? WHERE project_id=?",
                (str(root / "work" / "project"), proof["project_id"]),
            )
    installed = Settings.from_env()
    configuration = SettingsEnvironmentLockProvider(installed).configuration()
    settings = installed.model_copy(
        update={
            "database_url": f"sqlite:///{database.as_posix()}",
            "allowed_source_roots": [source / "source"],
            "allowed_work_root": root / "work",
            "matlab_executable": configuration.matlab_executable,
            "spm_dir": configuration.spm_dir,
            "dpabi_dir": configuration.dpabi_dir,
            "secrets_file": root / "mock-secrets.env",
            "enable_real_execution": True,
            "serve_frontend": True,
            "rag_db_dir": None,
        }
    )
    prepare = json.dumps(
        {
            "answer": "Mock 模型准备两样本方案；统计执行需单独确认。",  # noqa: RUF001
            "action": "prepare_statistical_analysis",
            "analysis_patch": {
                "test": "independent_two_sample_t",
                "grouping_column": "group",
                "group_order": ["ASD", "HC"],
                "metric_artifact_type": "metric.alff.z_score",
                "tail": "two_sided",
                "missing_value_policy": "error",
                "correction_method": "fdr",
                "correction_parameters": {"q_threshold": 0.05},
            },
        },
        ensure_ascii=False,
    )
    read = json.dumps({"answer": "读取确定性结果摘要。", "action": "get_statistical_results"})
    service = build_service(
        settings,
        providers={"mock": MockProvider(([prepare] if not args.resume else []) + [read] * 20)},
    )
    stop = threading.Event()
    thread = None
    try:
        profile_id = "internal-browser-mock"
        service.create_model_profile(
            ModelProfileCreate(
                profile=ModelProfileInput(
                    id=profile_id,
                    provider="mock",
                    base_url=f"http://127.0.0.1:{args.port}/v1",
                    model="Mock planning / real MATLAB",
                    api_key_env="INTERNAL_BROWSER_MOCK_API_KEY",
                    capabilities={"json_object"},
                ),
                api_key="mock-only",
            ),
            "internal-browser-profile",
        )
        pre = service.get_run(proof["preprocessing_run_id"])
        assert pre.state.value == "succeeded"
        conversation = service.create_conversation(
            ConversationCreate(
                mode="work",
                project_id=proof["project_id"],
                workspace_path=str(source / "source"),
                title="内部指标交接与统计网页验收",
                preferred_profile_id=profile_id,
            ),
            "internal-browser-conversation",
        )
        if not args.resume:
            service.repository.append_conversation_exchange(
                conversation.conversation_id,
                user_content="载入已完成 QC 的合成预处理来源",
                assistant_content="合成预处理与 QC 已完成；可准备独立统计方案。",  # noqa: RUF001
                assistant_payload={
                    "agent_role": "preprocessing",
                    "run": pre.model_dump(mode="json"),
                },
                tool=None,
                workspace_path=str(source / "source"),
                preferred_profile_id=profile_id,
                project_id=proof["project_id"],
                active_run_id=pre.run_id,
            )
        (root / "browser-setup.json").write_text(
            json.dumps(
                {
                    "project_id": proof["project_id"],
                    "conversation_id": conversation.conversation_id,
                    "preprocessing_run_id": pre.run_id,
                    "source_root": str(source),
                    "model": "Mock",
                    "execution": "real MATLAB in isolated copy",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        app = create_app(settings, service=service)

        @app.get("/v1/models")
        def models() -> dict[str, object]:
            return {"data": [{"id": "Mock planning / real MATLAB"}]}

        app.router.routes.insert(0, app.router.routes.pop())
        worker = build_worker(service, worker_id="internal-browser-acceptance")

        def work_loop() -> None:
            while not stop.is_set():
                if not worker.run_once():
                    stop.wait(0.2)

        thread = threading.Thread(target=work_loop, daemon=True)
        thread.start()
        print("INTERNAL_BROWSER_ACCEPTANCE_READY", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=args.port)
    finally:
        stop.set()
        if thread is not None:
            thread.join()
        service.close()


if __name__ == "__main__":
    main()
