"""Independently verify an existing synthetic run created through the Work UI."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import httpx
import nibabel as nib
import numpy as np


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:9090")
    parser.add_argument(
        "--verify-duplicate-request",
        action="store_true",
        help="Verify a repeated Work start is rejected; the test requests Mock execution only.",
    )
    args = parser.parse_args()
    root = args.output_root.resolve()
    setup = json.loads((root / "browser-setup.json").read_text(encoding="utf-8"))
    with httpx.Client(base_url=args.base_url, timeout=30) as client:

        def query(path: str):
            response = client.get("/api/v1" + path)
            response.raise_for_status()
            return response.json()

        run = query(f"/runs/{args.run_id}")
        assert run["state"] == "succeeded"
        conversation = query(f"/conversations/{setup['conversation_id']}")
        task = next(item for item in conversation["work_tasks"] if item["agent_role"] == "analysis")
        assert conversation["foreground_agent_role"] == "analysis"
        assert conversation["active_run_id"] == task["run_id"] == args.run_id
        assert task["plan_revision_id"] == run["plan_revision_id"]
        frozen_plan = query(f"/statistical-designs/{run['plan_revision_id']}")["plan_revision"]
        context = frozen_plan["plan"]["work_context"]
        assert context["conversation_id"] == setup["conversation_id"]
        assert context["proposal_hash"] == task["proposal_hash"]
        assert context["proposal_message_id"] == task["proposal_message_id"]
        assert context["draft_hash"] == task["draft_hash"]
        duplicate_blocked = None
        if args.verify_duplicate_request:
            before = query(f"/runs?project_id={setup['project_id']}")
            duplicate = client.post(
                "/api/v1/statistics/runs",
                json={
                    "project_id": setup["project_id"],
                    "statistical_design_revision_id": run["plan_revision_id"],
                    "expected_plan_hash": frozen_plan["plan_hash"],
                    "execution_backend": "mock",
                    "max_attempts": 1,
                },
                headers={"Idempotency-Key": "verify-duplicate-work-statistics-task"},
            )
            assert duplicate.status_code == 409, duplicate.text
            assert duplicate.json()["error"]["code"] == "statistics_work_run_already_created"
            after = query(f"/runs?project_id={setup['project_id']}")
            assert {item["run_id"] for item in before} == {item["run_id"] for item in after}
            assert duplicate.json()["error"]["details"]["run_id"] == args.run_id
            duplicate_blocked = True
        results = query(
            f"/statistics/results?project_id={setup['project_id']}&run_id={args.run_id}"
        )
        assert len(results) == 1 and results[0]["mode"] == "real"
        result = query(f"/statistics/results/{results[0]['result_id']}")
        report = json.loads(result["report_json"])
        design = report["statistical_design"]
        assert design["group_order"] == ["ASD", "HC"]
        assert design["test"] == "independent_two_sample_t"
        assert result["manifest"]["correction"]["q_threshold"] == 0.05
        imported = query(f"/statistics/external-imports/{design['external_import_id']}")
        for image in imported["inspection"]["images"]:
            path = root / "source" / image["relative_path"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == image["sha256"]
        artifacts = query(f"/runs/{args.run_id}/artifacts")
        downloaded = {}
        for artifact in artifacts:
            response = client.get(f"/api/v1/artifacts/{artifact['artifact_id']}/content")
            response.raise_for_status()
            assert len(response.content) == artifact["size_bytes"]
            assert hashlib.sha256(response.content).hexdigest() == artifact["checksum"]
            downloaded[artifact["artifact_type"]] = response.content
        actual = nib.as_closest_canonical(
            nib.Nifti1Image.from_bytes(downloaded["statistics.uncorrected_statistical_map"])
        ).get_fdata()
        images = [
            nib.load(root / "source" / group / f"zALFFMap_sub-{index:02d}.nii").get_fdata()
            for index, group in enumerate(["ASD"] * 3 + ["HC"] * 3, start=1)
        ]
        samples = np.asarray(images)
        first, second = samples[:3], samples[3:]
        inside = nib.load(root / "source" / "Mask.nii").get_fdata().astype(bool)
        variance = (first.var(axis=0, ddof=1) + second.var(axis=0, ddof=1)) / 2
        expected = np.zeros(inside.shape)
        expected[inside] = (first.mean(axis=0) - second.mean(axis=0))[inside] / np.sqrt(
            variance[inside] * 2 / 3
        )
        error = float(np.max(np.abs(actual - expected)))
        assert error < 1e-5
        assert report["inference_summary"]["significant_voxel_count"] == 8
        assert report["inference_summary"]["cluster_count"] == 1
        cluster = result["manifest"]["clusters"][0]
        assert cluster["direction"] == 1
        roi = query(
            f"/statistics/results/{result['result_id']}/roi?cluster_id={cluster['cluster_id']}"
        )
        signal = np.zeros(inside.shape, dtype=bool)
        signal[2:4, 2:4, 2:4] = True
        expected_means = {f"sub-{i:02d}": float(v[signal].mean()) for i, v in enumerate(images, 1)}
        roi_error = max(
            abs(row["mean_value"] - expected_means[row["subject_id"]]) for row in roi["rows"]
        )
        assert roi_error == 0 and roi["voxel_count"] == 8
        localization = query(f"/statistics/results/{result['result_id']}/localization?atlas_id=aal")
        assert localization["clusters"][0]["label"] == "Postcentral_L"
        summary = {
            "model": "Mock",
            "execution": "real MATLAB through public Worker",
            "run_id": args.run_id,
            "state": run["state"],
            "result_id": result["result_id"],
            "result_bundle_hash": result["bundle_hash"],
            "max_absolute_t_error": error,
            "max_absolute_roi_mean_error": roi_error,
            "artifact_hashes_verified": len(artifacts),
            "inference_summary": report["inference_summary"],
            "localization": localization,
            "conversation_id": conversation["conversation_id"],
            "persisted_analysis_task": task,
            "duplicate_start_blocked": duplicate_blocked,
        }
        (root / "browser-verification.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (root / "report.json").write_text(result["report_json"], encoding="utf-8")
        (root / "report.md").write_text(result["report_markdown"], encoding="utf-8")
        print("WORK_BROWSER_STATISTICS_VERIFIED", flush=True)


if __name__ == "__main__":
    main()
