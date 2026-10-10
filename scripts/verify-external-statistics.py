"""Opt-in real MATLAB acceptance of reviewed external metric inputs.

Creates a new isolated synthetic source, then uses the application service,
approval, public Worker, DPABI statistics, completion registration, and result
query paths. No existing dataset or scientific installation is modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import nibabel as nib
import numpy as np

from neuroagent.analysis.dpabi_atlas import DPABI_ATLASES
from neuroagent.application.contracts import (
    ApprovalCreate,
    ApprovalDecision,
    ExecutionBackend,
    ExternalAnalysisImportIncludeRequest,
    ExternalAnalysisImportInspectRequest,
    ExternalDemographicsImportRequest,
    ProjectCreate,
    StatisticalDesignCreate,
    StatisticalDesignValidationRequest,
    StatisticsRunCreate,
)
from neuroagent.application.settings import Settings
from neuroagent.application.statistics_draft import StatisticsDraft
from neuroagent.bootstrap import build_service, build_worker
from neuroagent.domain.fmri.statistics import (
    AnalysisImage,
    FdrCorrection,
    StatisticalDesignRevision,
    design_matrix,
)
from neuroagent.infrastructure.environment import SettingsEnvironmentLockProvider


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--real-matlab", action="store_true")
    parser.add_argument("--atlas", choices=("aal", "aal3", "brodmann"))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--with-covariate", action="store_true")
    modes.add_argument("--paired", action="store_true")
    modes.add_argument("--with-signal", action="store_true")
    args = parser.parse_args()
    if not args.real_matlab:
        parser.error("--real-matlab is required to confirm the real acceptance run")
    root = args.output_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    source = root / "source"
    work = root / "work"
    source.mkdir()
    work.mkdir()
    installed = Settings.from_env()
    configuration = SettingsEnvironmentLockProvider(installed).configuration()
    settings = installed.model_copy(
        update={
            "database_url": f"sqlite:///{(root / 'acceptance.sqlite').as_posix()}",
            "allowed_source_roots": [source],
            "allowed_work_root": work,
            "matlab_executable": configuration.matlab_executable,
            "spm_dir": configuration.spm_dir,
            "dpabi_dir": configuration.dpabi_dir,
            "enable_real_execution": True,
            "serve_frontend": False,
            "rag_db_dir": None,
        }
    )
    rng = np.random.default_rng(20261009)
    shape = (6, 7, 8)
    affine = np.diag([3.0, 3.0, 3.0, 1.0])
    if args.with_signal:
        # Explicit synthetic MNI geometry; no claim of measured/normalized data.
        affine[:3, 3] = (-48, -24, 30)
    base = rng.uniform(10, 20, size=shape)
    variation = rng.uniform(0.5, 2, size=shape)
    mask = np.zeros(shape, dtype=np.uint8)
    mask[1:-1, 1:-1, 1:-1] = 1
    signal = np.zeros(shape, dtype=bool)
    signal[2:4, 2:4, 2:4] = True
    source_values: list[np.ndarray] = []
    for index, offset in enumerate((-1, 0, 1, -2, 0, 2), start=1):
        group = "first" if index <= 3 else "second"
        folder = source / group
        folder.mkdir(exist_ok=True)
        values = ((base + offset * variation) * mask).astype(np.float32)
        if args.with_signal and index <= 3:
            values[signal] += 30
        source_values.append(values)
        subject_index = (index - 1) % 3 + 1 if args.paired else index
        nib.save(nib.Nifti1Image(values, affine), folder / f"zALFFMap_sub-{subject_index:02d}.nii")
    nib.save(nib.Nifti1Image(mask, affine), source / "Mask.nii")
    source_hashes = {
        path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source.rglob("*.nii")
    }

    service = build_service(settings)
    try:
        project = service.create_project(
            ProjectCreate(
                name="Synthetic external statistics acceptance",
                source_roots=[str(source)],
                work_root=str(work / "project"),
            ),
            "acceptance-project",
        )
        inspected = service.inspect_external_analysis_import(
            ExternalAnalysisImportInspectRequest(
                project_id=project.project_id,
                source_path=str(source),
                layout="paired_conditions" if args.paired else "grouped",
            ),
            "acceptance-inspect",
        )
        assert not inspected.inspection.blocking_issues
        included = service.include_external_analysis_import(
            inspected.import_id,
            ExternalAnalysisImportIncludeRequest(
                expected_version=inspected.version,
                included_subject_ids=tuple(
                    dict.fromkeys(
                        item.subject_id
                        for item in (
                            reversed(inspected.inspection.images)
                            if args.paired
                            else inspected.inspection.images
                        )
                    )
                ),
                group_order=("first", "second"),
                coordinate_space="MNI" if args.with_signal else "unknown",
                acknowledge_unknown_processing=True,
                actor="authorized_acceptance",
                reason="User-authorized real MATLAB acceptance on six synthetic metric images.",
                mask_relative_path="Mask.nii",
            ),
            "acceptance-include",
        )
        assert included.selection is not None
        selection = included.selection
        # The design order is deliberately shuffled; all joins use subject IDs.
        rows = tuple(reversed(selection.images))
        design = StatisticalDesignRevision(
            revision_id="external-two-sample-fdr",
            test="paired_t" if args.paired else "independent_two_sample_t",
            subject_order=tuple(dict.fromkeys(item.subject_id for item in rows)),
            images=tuple(
                AnalysisImage(
                    subject_id=item.subject_id,
                    artifact_id=item.artifact_id,
                    group=None if args.paired else item.group,
                    condition=item.condition,
                )
                for item in rows
            ),
            group_order=() if args.paired else ("first", "second"),
            condition_order=("second", "first") if args.paired else (),
            covariates=(),
            contrast=(1.0, 0.0, 0.0, 0.0) if args.paired else (1.0, 0.0),
            one_sample_baseline=None,
            mask_artifact_id=selection.mask_artifact_id,
            tail="two_sided",
            missing_value_policy="error",
            qc_source="external_import",
            external_import_id=included.import_id,
            external_import_hash=included.inclusion_hash,
        )
        correction = FdrCorrection(
            method="fdr",
            q_threshold=0.05,
            mask_artifact_id=selection.mask_artifact_id,
            statistic_type="T",
            df1=2 if args.paired else 4,
            df2=None,
        )
        if args.paired:
            proposal, issues = service.prepare_work_statistical_design_proposal(
                project_id=project.project_id,
                external_import_id=included.import_id,
                draft=StatisticsDraft(
                    test="paired_t",
                    condition_column="condition",
                    condition_order=["first", "second"],
                    metric_artifact_type="external.alff.z_score",
                    tail="two_sided",
                    missing_value_policy="error",
                    correction_method="fdr",
                    correction_parameters={"q_threshold": 0.05},
                ),
            )
            assert proposal is not None and not issues, issues
            design = proposal.design
            assert isinstance(proposal.correction, FdrCorrection)
            correction = proposal.correction
        if args.with_covariate:
            table = source / "demographics.csv"
            table.write_text(
                "ID,Age\nsub-06,21\nsub-02,23\nsub-05,23\nsub-01,20\nsub-04,20\nsub-03,21\n",
                encoding="utf-8",
            )
            demographics = service.import_external_demographics(
                included.import_id,
                ExternalDemographicsImportRequest(
                    source_path=str(table),
                    subject_id_column="ID",
                    column_mapping={"age": "Age"},
                    expected_import_version=included.version,
                ),
                "acceptance-demographics",
            )
            proposal, issues = service.prepare_work_statistical_design_proposal(
                project_id=project.project_id,
                external_import_id=included.import_id,
                draft=StatisticsDraft(
                    test="independent_two_sample_t",
                    grouping_column="group",
                    group_order=["first", "second"],
                    metric_artifact_type="external.alff.z_score",
                    tail="two_sided",
                    missing_value_policy="error",
                    covariates=[{"name": "age", "centering": "grand_mean"}],
                    correction_method="fdr",
                    correction_parameters={"q_threshold": 0.05},
                ),
            )
            assert proposal is not None and not issues, issues
            assert proposal.design.external_demographics_hash == demographics.content_hash
            design = proposal.design
            assert isinstance(proposal.correction, FdrCorrection)
            correction = proposal.correction
        proposed = service.create_statistical_design(
            StatisticalDesignCreate(
                project_id=project.project_id,
                expected_project_version=service.get_project(project.project_id).version,
                input_manifest_hash=selection.input_manifest_hash,
                design=design,
                correction=correction,
                atlas_id=args.atlas,
            ),
            "acceptance-design",
        )
        validated = service.validate_statistical_design(
            proposed.plan_revision.plan_revision_id,
            StatisticalDesignValidationRequest(expected_version=proposed.plan_revision.version),
            "acceptance-validate",
        )
        plan = validated.plan_revision
        service.approve_plan(
            plan.plan_revision_id,
            ApprovalCreate(
                expected_version=plan.version,
                plan_hash=plan.plan_hash,
                actor="authorized_acceptance",
                decision=ApprovalDecision.APPROVED,
                reason="User-authorized synthetic two-sample t plus two-sided FDR acceptance.",
            ),
            "acceptance-approve",
        )
        run = service.create_statistics_run(
            StatisticsRunCreate(
                project_id=project.project_id,
                statistical_design_revision_id=plan.plan_revision_id,
                expected_plan_hash=plan.plan_hash,
                execution_backend=ExecutionBackend.MATLAB,
                real_execution_confirmed=True,
            ),
            "acceptance-run",
        )
        print(f"QUEUED {run.run_id}", flush=True)
        assert build_worker(service, worker_id="external-acceptance").run_once()
        completed = service.get_run(run.run_id)
        print(f"COMPLETED {completed.state.value}", flush=True)
        if completed.state.value != "succeeded":
            raise RuntimeError(completed.error or "external acceptance failed")
        artifacts = service.repository.list_artifacts(run.run_id)
        statistic = next(
            item
            for item in artifacts
            if item.artifact_type == "statistics.uncorrected_statistical_map"
        )
        actual_image = nib.as_closest_canonical(
            nib.load(str(Path(project.work_root) / run.run_id / statistic.relative_path))
        )
        samples = np.asarray(source_values, dtype=np.float64)
        first, second = samples[:3], samples[3:]
        pooled = (first.var(axis=0, ddof=1) + second.var(axis=0, ddof=1)) / 2
        expected = np.zeros(shape)
        inside = mask.astype(bool)
        expected[inside] = (first.mean(axis=0) - second.mean(axis=0))[inside] / np.sqrt(
            pooled[inside] * (2 / 3)
        )
        if args.with_covariate:
            matrix = np.asarray(design_matrix(design))
            data_by_subject = {
                f"sub-{index:02d}": value for index, value in enumerate(source_values, 1)
            }
            response = np.asarray([data_by_subject[subject] for subject in design.subject_order])[
                :, inside
            ]
            beta = np.linalg.lstsq(matrix, response, rcond=None)[0]
            residual = response - matrix @ beta
            variance = np.sum(residual**2, axis=0) / (len(matrix) - matrix.shape[1])
            contrast = np.asarray(design.contrast)
            expected[inside] = (
                contrast
                @ beta
                / np.sqrt(variance * (contrast @ np.linalg.inv(matrix.T @ matrix) @ contrast))
            )
        if args.paired:
            differences = first - second
            expected[inside] = differences.mean(axis=0)[inside] / np.sqrt(
                differences.var(axis=0, ddof=1)[inside] / len(differences)
            )
        expected_image = nib.as_closest_canonical(nib.Nifti1Image(expected, affine))
        error = float(np.max(np.abs(actual_image.get_fdata() - expected_image.get_fdata())))
        assert error < 1e-5, error
        result = service.repository.list_statistical_results(
            project_id=project.project_id, run_id=run.run_id
        )[0]
        detail = service.repository.get_statistical_result(result.result_id)
        report = json.loads(detail.report_json)
        atlas_id = args.atlas or "aal"
        localization = service.localize_statistical_result(result.result_id, atlas_id)
        if args.atlas:
            assert proposed.localization_plan is not None
            assert report["localization_plan"] == proposed.localization_plan.model_dump(mode="json")
            if args.with_signal:
                assert (
                    localization.atlas_content_hash == proposed.localization_plan.atlas_content_hash
                )
        roi_summary = None
        atlas_summary = None
        if args.with_signal:
            assert report["inference_summary"]["significant_voxel_count"] == int(signal.sum())
            assert report["inference_summary"]["localization_applicable"] is True
            assert len(localization.clusters) == 1
            if atlas_id == "aal":
                assert localization.clusters[0].label == "Postcentral_L"
            assert sum(region.voxel_count for region in localization.clusters[0].coverage) == 8
            assert any(region.label_value > 0 for region in localization.clusters[0].coverage)
            # Independent direct sampling of the known eight-voxel synthetic cube.
            # This does not call the application localization implementation.
            descriptor = next(item for item in DPABI_ATLASES if item.atlas_id == atlas_id)
            assert configuration.dpabi_dir is not None
            atlas_image = nib.load(
                str(configuration.dpabi_dir / "Templates" / descriptor.nifti_filename)
            )
            atlas_values = np.asarray(atlas_image.dataobj)
            voxels = np.argwhere(signal)
            world = affine @ np.column_stack((voxels, np.ones(len(voxels)))).T
            sampled_voxels = np.rint((np.linalg.inv(atlas_image.affine) @ world)[:3].T).astype(int)
            inside_atlas = np.all(
                (sampled_voxels >= 0) & (sampled_voxels < np.asarray(atlas_image.shape)), axis=1
            )
            sampled_labels = np.zeros(len(voxels), dtype=int)
            sample = sampled_voxels[inside_atlas]
            sampled_labels[inside_atlas] = atlas_values[sample[:, 0], sample[:, 1], sample[:, 2]]
            label_values, counts = np.unique(sampled_labels, return_counts=True)
            expected_coverage = dict(zip(label_values.tolist(), counts.tolist(), strict=True))
            actual_coverage = {
                region.label_value: region.voxel_count
                for region in localization.clusters[0].coverage
            }
            assert actual_coverage == expected_coverage
            clusters = detail.manifest["clusters"]
            peak_world = np.asarray((*clusters[0]["peak_coordinate_mm"], 1))
            peak_voxel = np.rint((np.linalg.inv(atlas_image.affine) @ peak_world)[:3]).astype(int)
            expected_peak = int(atlas_values[tuple(peak_voxel)])
            assert localization.clusters[0].label_value == expected_peak
            for region in localization.clusters[0].coverage:
                assert region.proportion == expected_coverage[region.label_value] / len(voxels)
            atlas_summary = {
                "atlas_id": atlas_id,
                "atlas_content_hash": localization.atlas_content_hash,
                "expected_peak_label_value": expected_peak,
                "coverage_counts": expected_coverage,
                "independent_affine_sampling_verified": True,
            }
            assert len(clusters) == 1 and clusters[0]["direction"] == 1
            roi = service.statistical_cluster_roi(result.result_id, clusters[0]["cluster_id"])
            assert roi.status == "descriptive" and roi.voxel_count == int(signal.sum())
            assert len(roi.rows) == 6
            expected_means = {
                f"sub-{index:02d}": float(values[signal].astype(np.float64).mean())
                for index, values in enumerate(source_values, 1)
            }
            roi_error = max(
                abs(row.mean_value - expected_means[row.subject_id]) for row in roi.rows
            )
            assert roi_error < 1e-6, roi_error
            (root / "roi.json").write_text(roi.model_dump_json(indent=2), encoding="utf-8")
            (root / "localization.json").write_text(
                localization.model_dump_json(indent=2), encoding="utf-8"
            )
            roi_summary = {
                "voxel_count": roi.voxel_count,
                "rows": len(roi.rows),
                "max_absolute_mean_error": roi_error,
            }
        else:
            assert report["inference_summary"]["status"] == "no_significant_voxels"
            assert report["inference_summary"]["significant_voxel_count"] == 0
            assert report["inference_summary"]["localization_applicable"] is False
            assert localization.status == "no_significant_voxels"
            roi = service.statistical_cluster_roi(result.result_id, None)
            assert roi.status == "no_significant_voxels" and not roi.rows
        (root / "report.md").write_text(detail.report_markdown, encoding="utf-8")
        (root / "report.json").write_text(detail.report_json, encoding="utf-8")
        assert all(
            hashlib.sha256((source / relative).read_bytes()).hexdigest() == digest
            for relative, digest in source_hashes.items()
        )
        summary = {
            "input": "six synthetic ALFF maps; "
            + (
                "fixed eight-voxel group effect; "
                if args.with_signal
                else "identical group means; "
            )
            + ("shuffled demographics rows" if args.with_covariate else "shuffled design order"),
            "project_id": project.project_id,
            "import_id": included.import_id,
            "inclusion_hash": included.inclusion_hash,
            "input_manifest_hash": selection.input_manifest_hash,
            "plan_hash": plan.plan_hash,
            "run_id": run.run_id,
            "state": completed.state.value,
            "with_covariate": args.with_covariate,
            "paired": args.paired,
            "with_signal": args.with_signal,
            "roi": roi_summary,
            "atlas": atlas_summary,
            "localization_plan": report.get("localization_plan"),
            "source_files_unchanged": True,
            "subject_order": list(design.subject_order),
            "condition_order": list(design.condition_order),
            "external_demographics_revision_id": design.external_demographics_revision_id,
            "external_demographics_hash": design.external_demographics_hash,
            "max_absolute_t_error": error,
            "inference_summary": report["inference_summary"],
            "localization_status": localization.status,
            "result_bundle_hash": detail.bundle_hash,
            "artifacts": [
                {"type": item.artifact_type, "sha256": item.checksum} for item in artifacts
            ],
        }
        (root / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print("EXTERNAL_STATISTICS_ACCEPTANCE_PASS", flush=True)
    finally:
        service.close()


if __name__ == "__main__":
    main()
