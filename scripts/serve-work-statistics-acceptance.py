"""Opt-in browser acceptance: Mock planning, real DPABI, isolated synthetic inputs.

Seeds only a project, an empty conversation and a labelled Mock model. Data
inspection, inclusion, statistical design, approval and run must come from UI.
"""

from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

import nibabel as nib
import numpy as np
import uvicorn

from neuroagent.agent.providers import MockProvider, OpenAICompatibleProvider
from neuroagent.api import create_app
from neuroagent.application.contracts import (
    ConversationCreate,
    ModelProfileCreate,
    ModelProfileInput,
    ProjectCreate,
)
from neuroagent.application.settings import Settings
from neuroagent.bootstrap import build_service, build_worker
from neuroagent.infrastructure.environment import SettingsEnvironmentLockProvider


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument("--real-matlab", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if not args.real_matlab:
        parser.error("--real-matlab is required for this authorized acceptance")
    root = args.output_root.resolve()
    if args.resume:
        if not (root / "browser-setup.json").is_file():
            parser.error("--resume requires this script's existing acceptance directory")
    else:
        root.mkdir(parents=True, exist_ok=False)
    source, work = root / "source", root / "work"
    source.mkdir(exist_ok=args.resume)
    work.mkdir(exist_ok=args.resume)
    rng = np.random.default_rng(20261009)
    shape = (6, 7, 8)
    affine = np.diag([3.0, 3.0, 3.0, 1.0])
    affine[:3, 3] = (-48, -24, 30)
    base = rng.uniform(10, 20, shape)
    variation = rng.uniform(0.5, 2, shape)
    mask = np.zeros(shape, dtype=np.uint8)
    mask[1:-1, 1:-1, 1:-1] = 1
    for index, offset in enumerate((-1, 0, 1, -2, 0, 2), start=1):
        folder = source / ("ASD" if index <= 3 else "HC")
        folder.mkdir(exist_ok=True)
        values = ((base + offset * variation) * mask).astype(np.float32)
        if index <= 3:
            values[2:4, 2:4, 2:4] += 30
        if not args.resume:
            nib.save(nib.Nifti1Image(values, affine), folder / f"zALFFMap_sub-{index:02d}.nii")
    if not args.resume:
        nib.save(nib.Nifti1Image(mask, affine), source / "Mask.nii")
    configuration = SettingsEnvironmentLockProvider(Settings.from_env()).configuration()
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{(root / 'acceptance.sqlite').as_posix()}",
        allowed_source_roots=[source],
        allowed_work_root=work,
        matlab_executable=configuration.matlab_executable,
        spm_dir=configuration.spm_dir,
        dpabi_dir=configuration.dpabi_dir,
        secrets_file=root / "mock-secrets.env",
        redaction_salt="synthetic-browser-acceptance-no-private-data",
        serve_frontend=True,
        enable_real_execution=True,
        rag_db_dir=None,
    )
    answer = json.dumps(
        {
            "answer": "Mock 模型提出固定验收方案；执行仍需独立审批和真实运行确认。",  # noqa: RUF001
            "action": "prepare_statistical_analysis",
            "analysis_patch": {
                "test": "independent_two_sample_t",
                "grouping_column": "group",
                "group_order": ["ASD", "HC"],
                "metric_artifact_type": "external.alff.z_score",
                "tail": "two_sided",
                "missing_value_policy": "error",
                "correction_method": "fdr",
                "correction_parameters": {"q_threshold": 0.05},
            },
        },
        ensure_ascii=False,
    )
    mock = MockProvider([answer] * 20)
    service = build_service(
        settings, providers={"mock": mock, "openai-compatible": OpenAICompatibleProvider()}
    )
    stop = threading.Event()
    thread: threading.Thread | None = None
    worker = build_worker(service, worker_id="browser-real-acceptance")

    def work_loop() -> None:
        while not stop.is_set():
            if not worker.run_once():
                stop.wait(0.2)

    try:
        project = service.create_project(
            ProjectCreate(
                name="Browser synthetic statistics acceptance",
                source_roots=[str(source)],
                work_root=str(work / "project"),
            ),
            "browser-project",
        )
        service.create_model_profile(
            ModelProfileCreate(
                profile=ModelProfileInput(
                    id="browser-mock",
                    provider="mock",
                    base_url=f"http://127.0.0.1:{args.port}/v1",
                    model="Mock planning (real MATLAB execution)",
                    api_key_env="BROWSER_MOCK_API_KEY",
                    capabilities={"json_object"},
                ),
                api_key="mock-only",
            ),
            "browser-mock-profile",
        )
        conversation = service.create_conversation(
            ConversationCreate(
                mode="work",
                project_id=project.project_id,
                workspace_path=str(source),
                title="Mock 规划与真实 MATLAB 完整网页验收",
                preferred_profile_id="browser-mock",
            ),
            "browser-empty-conversation",
        )
        (root / "browser-setup.json").write_text(
            json.dumps(
                {
                    "project_id": project.project_id,
                    "conversation_id": conversation.conversation_id,
                    "source_path": str(source),
                    "model": "Mock",
                    "execution": "real MATLAB",
                    "fixture": (
                        "six synthetic ALFF maps with a fixed eight-voxel ASD-minus-HC effect"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        app = create_app(settings, service=service)

        @app.get("/v1/models")
        def mock_models() -> dict[str, object]:
            return {"data": [{"id": "Mock planning (real MATLAB execution)"}]}

        # The SPA catch-all precedes routes added after create_app().
        app.router.routes.insert(0, app.router.routes.pop())

        thread = threading.Thread(target=work_loop, daemon=True)
        thread.start()
        print("BROWSER_WORK_ACCEPTANCE_READY", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=args.port)
    finally:
        stop.set()
        if thread is not None:
            thread.join()
        service.close()


if __name__ == "__main__":
    main()
