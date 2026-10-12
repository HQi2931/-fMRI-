"""Opt-in public Worker acceptance for four statistical capabilities.

Every invocation creates its own small synthetic source and database. Inputs,
demographics, inclusion, design, approval, execution, and registration use the
normal application services. Numerical references use NumPy/SciPy rather than
the application's matrix builder or MATLAB-generated expected values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np
from scipy import ndimage, stats

from neuroagent.application.contracts.external_imports import (
    ExternalAnalysisImportIncludeRequest,
    ExternalAnalysisImportInspectRequest,
)
from neuroagent.application.contracts.plans import (
    ApprovalCreate,
    ApprovalDecision,
)
from neuroagent.application.contracts.projects import (
    ExternalDemographicsImportRequest,
    ProjectCreate,
)
from neuroagent.application.contracts.runs import (
    ExecutionBackend,
)
from neuroagent.application.contracts.statistics import (
    StatisticalDesignCreate,
    StatisticalDesignValidationRequest,
    StatisticsRunCreate,
)
from neuroagent.application.settings import Settings
from neuroagent.application.statistics_draft import StatisticsDraft
from neuroagent.bootstrap import build_service, build_worker
from neuroagent.domain.fmri.results import StatisticalResultManifest
from neuroagent.domain.fmri.statistics import GrfCorrection
from neuroagent.infrastructure.environment import SettingsEnvironmentLockProvider

TESTS = ("one_sample_t", "correlation", "regression", "one_way_anova_ancova")
SUBJECTS = tuple(f"sub-{index:02d}" for index in range(1, 13))
INCLUSION_ORDER = (9, 1, 7, 4, 11, 0, 6, 10, 3, 8, 2, 5)
TABLE_ORDER = (5, 2, 8, 0, 11, 6, 3, 9, 1, 10, 4, 7)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixtures(source: Path, test: str) -> dict[str, Any]:
    shape = (10, 10, 10)
    affine = np.diag([3.0, 3.0, 3.0, 1.0])
    affine[:3, 3] = (-48, -24, 30)
    x, y, z = np.indices(shape, dtype=float)
    mask = np.zeros(shape, dtype=np.uint8)
    mask[1:-1, 1:-1, 1:-1] = 1
    signal = np.where(x < 5, 1.0, -1.0)
    predictor = np.linspace(-2.5, 2.5, len(SUBJECTS))
    nuisance = np.asarray((1, -1, 0, 1, -1, 0, -1, 1, 0, 1, 0, -1), dtype=float)
    groups = ("first", "second", "third")
    images: dict[str, np.ndarray] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for index, subject in enumerate(SUBJECTS):
        group = groups[index // 4]
        noise = 0.45 * np.sin(0.37 * (index + 1) * x + 0.21 * y + 0.13 * z)
        noise += 0.18 * np.cos((index + 1) * 0.31 * z + 0.17 * y)
        if test == "one_sample_t":
            volume = 2.0 + 0.8 * signal + noise
        elif test in {"correlation", "regression"}:
            volume = 4.0 + 0.7 * predictor[index] * signal + 0.4 * nuisance[index] + noise
        else:
            group_effect = (0.9, 0.6, 0.0)[index // 4]
            volume = 4.0 + group_effect * signal + 0.3 * nuisance[index] + noise
        volume = volume.astype(np.float32)
        folder = source / group
        folder.mkdir(exist_ok=True)
        nib.save(nib.Nifti1Image(volume, affine), folder / f"zALFFMap_{subject}.nii")
        images[subject] = volume
        metadata[subject] = {
            "group": group,
            "predictor": float(predictor[index]),
            "nuisance": float(nuisance[index]),
        }
    nib.save(nib.Nifti1Image(mask, affine), source / "Mask.nii")
    table = source / "demographics.csv"
    table.write_text(
        "ID,Predictor,Nuisance\n"
        + "".join(
            f"{SUBJECTS[index]},{predictor[index]:.17g},{nuisance[index]:.17g}\n"
            for index in TABLE_ORDER
        ),
        encoding="utf-8",
    )
    return {"mask": mask, "affine": affine, "images": images, "metadata": metadata}


def _reference(test: str, subjects: tuple[str, ...], fixture: dict[str, Any]) -> dict[str, Any]:
    """Construct a separate model from source identity and independent choices."""
    mask = fixture["mask"].astype(bool)
    response = np.asarray([fixture["images"][subject] for subject in subjects], dtype=float)
    response = response[:, mask]
    rows = [fixture["metadata"][subject] for subject in subjects]
    predictor = np.asarray([row["predictor"] for row in rows])
    nuisance = np.asarray([row["nuisance"] for row in rows])
    predictor -= predictor.mean()
    nuisance -= nuisance.mean()
    intercept = np.ones(len(subjects))
    if test == "one_sample_t":
        matrix = intercept[:, None]
        response -= 2.0
        contrast = np.asarray((1.0,))
    elif test == "correlation":
        matrix = np.column_stack((predictor, intercept, nuisance))
        contrast = np.asarray((1.0, 0.0, 0.0))
    elif test == "regression":
        matrix = np.column_stack((intercept, predictor, nuisance))
        contrast = np.asarray((0.0, 1.0, 0.0))
    else:
        matrix = np.column_stack(
            (
                [row["group"] == "third" for row in rows],
                [row["group"] == "first" for row in rows],
                intercept,
                nuisance,
            )
        ).astype(float)
        contrast = np.asarray((1.0, 1.0, 0.0, 0.0))
    beta = np.linalg.lstsq(matrix, response, rcond=None)[0]
    residual = response - matrix @ beta
    df = len(subjects) - matrix.shape[1]
    sse = np.sum(residual**2, axis=0)
    if test == "one_way_anova_ancova":
        reduced = np.column_stack((intercept, nuisance))
        reduced_residual = response - reduced @ np.linalg.lstsq(reduced, response, rcond=None)[0]
        difference = np.sum(reduced_residual**2, axis=0) - sse
        statistic = (difference / 2.0) / (sse / df)
        effect = difference / sse
    else:
        coefficient = contrast @ beta
        statistic = coefficient / np.sqrt(
            (sse / df) * (contrast @ np.linalg.inv(matrix.T @ matrix) @ contrast)
        )
        if test == "correlation":
            controls = np.column_stack((intercept, nuisance))
            residual_x = predictor - controls @ np.linalg.lstsq(controls, predictor, rcond=None)[0]
            residual_y = response - controls @ np.linalg.lstsq(controls, response, rcond=None)[0]
            statistic = (residual_x @ residual_y) / np.sqrt(
                (residual_x @ residual_x) * np.sum(residual_y**2, axis=0)
            )
            effect = statistic
        elif test == "regression":
            effect = coefficient
        else:
            effect = coefficient / np.sqrt(sse / df)
    return {
        "matrix": matrix,
        "contrast": contrast,
        "df": df,
        "statistic": statistic,
        "effect": effect,
    }


def _compare(actual: np.ndarray, expected: np.ndarray) -> dict[str, float]:
    assert np.all(np.isfinite(actual)) and np.all(np.isfinite(expected))
    error = float(np.max(np.abs(actual - expected)))
    relative = float(np.max(np.abs(actual - expected) / (1.0 + np.abs(expected))))
    assert relative < 5e-6, (error, relative)
    return {"max_absolute_error": error, "max_scaled_error": relative}


def _verify_correction(
    test: str,
    raw: np.ndarray,
    corrected: np.ndarray,
    evidence: dict[str, Any],
    df: int,
    corrected_z: np.ndarray | None,
    raw_description: str,
) -> dict[str, Any]:
    if evidence["method"] == "fdr":
        t_value = raw * np.sqrt(df / (1.0 - raw**2)) if test == "correlation" else raw
        p_values = 2.0 * stats.t.sf(np.abs(t_value), df)
        ordered = np.sort(p_values)
        accepted = ordered <= np.arange(1, len(ordered) + 1) / len(ordered) * 0.05
        expected_cutoff = float(ordered[np.flatnonzero(accepted)[-1]]) if accepted.any() else None
        actual_cutoff = evidence["actual_p_threshold"]
        if actual_cutoff == []:
            actual_cutoff = None
        if expected_cutoff is None:
            assert actual_cutoff is None or float(actual_cutoff) == 0
            keep = np.zeros(raw.shape, dtype=bool)
        else:
            assert actual_cutoff is not None
            assert abs(float(actual_cutoff) - expected_cutoff) < 1e-10
            keep = p_values <= float(actual_cutoff) + 1e-12
        assert np.array_equal(corrected != 0, keep)
        return {"method": "fdr", "independent_bh_p_cutoff": expected_cutoff}
    assert corrected_z is not None
    dlh = float(evidence["effective_smoothness_dlh"])
    assert math.isfinite(dlh) and dlh > 0
    minimum = int(evidence["minimum_cluster_size"])
    assert minimum == evidence["minimum_cluster_size"] and minimum > 0
    voxel_count = int(evidence.get("effective_mask_voxel_count", raw.size))
    two_tailed = test != "one_way_anova_ancova"
    voxel_p, cluster_p = 0.001, 0.05
    z_threshold = float(stats.norm.isf(voxel_p / 2 if two_tailed else voxel_p))
    em = (
        voxel_count
        * (2 * math.pi) ** -2
        * dlh
        * (z_threshold**2 - 1)
        * math.exp(-(z_threshold**2) / 2)
    )
    en = voxel_count * float(stats.norm.sf(z_threshold))
    cluster_beta = (math.gamma(2.5) * em / en) ** (2 / 3)
    target_p = cluster_p / 2 if two_tailed else cluster_p
    expected_minimum = 1
    while -math.expm1(-em * math.exp(-cluster_beta * expected_minimum ** (2 / 3))) >= target_p:
        expected_minimum += 1
        assert expected_minimum < 1_000_000
    assert minimum == expected_minimum
    assert evidence["cluster_connectivity"] == 26
    source = evidence["smoothness_source"]
    header_smoothness = re.search(
        r"\{dLh_([^}]+)\}\{FWHMx_(.*?)FWHMy_(.*?)FWHMz_(.*?)mm\}", raw_description
    )
    header_dlh = float(header_smoothness[1]) if header_smoothness is not None else 0.0
    expected_source = (
        "provided"
        if test == "one_way_anova_ancova"
        else "header"
        if header_dlh != 0
        else "estimated"
    )
    assert source == expected_source
    if source == "provided":
        assert dlh == 0.05
    elif source == "header":
        assert header_smoothness is not None and dlh == header_dlh
        assert np.allclose(
            evidence["effective_fwhm"],
            [float(header_smoothness[index]) for index in (2, 3, 4)],
            atol=1e-9,
            rtol=0,
        )
    # The caller passes complete masked geometry for independent 26-neighbor
    # labelling. Masked-out voxels have zero, and cannot connect separate signs.
    p_one_tail = stats.f.sf(raw, 2, df) if not two_tailed else stats.t.sf(np.abs(raw), df)
    z_values = stats.norm.isf(p_one_tail)
    if two_tailed:
        z_values *= np.sign(raw)
    keep = np.zeros(raw.shape, dtype=bool)
    for sign in (1, -1) if two_tailed else (1,):
        labels, count = ndimage.label(sign * z_values >= z_threshold, structure=np.ones((3, 3, 3)))
        sizes = np.bincount(labels.ravel())
        accepted_ids = np.flatnonzero(sizes >= minimum)
        accepted_ids = accepted_ids[accepted_ids != 0]
        keep |= np.isin(labels, accepted_ids) if count else False
    assert np.array_equal(corrected != 0, keep)
    assert np.array_equal(corrected_z != 0, keep)
    assert np.allclose(corrected, raw * keep, atol=1e-5, rtol=1e-6)
    return {
        "method": "grf",
        "minimum_cluster_size": minimum,
        "independent_minimum_cluster_size": expected_minimum,
        "effective_smoothness_dlh": dlh,
        "smoothness_source": source,
        "effective_fwhm": evidence.get("effective_fwhm"),
        "effective_mask_voxel_count": voxel_count,
        "smoothness_source_independently_verified": True,
        "independent_26_neighbor_support_verified": True,
    }


def _direct_grf(
    root: Path,
    executable: Path,
    raw_path: Path,
    source_mask: Path,
    corrected: np.ndarray,
    corrected_z: np.ndarray,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Call DPABI directly as a reference, without executing the Work template."""
    folder = root / "direct-grf-reference"
    folder.mkdir()
    bootstrap = raw_path.parent.parent / "scripts" / "bootstrap.m"
    assert bootstrap.is_file()

    def quoted(path: Path) -> str:
        return path.as_posix().replace("'", "''")

    dlh = str(evidence["smoothness_dlh"]) if evidence["smoothness_source"] == "provided" else "[]"
    df2 = str(evidence["df2"]) if evidence["statistic_type"] == "F" else "[]"
    script = folder / "direct_grf.m"
    script.write_text(
        f"run('{quoted(bootstrap)}');\n"
        f"[z,minimum,header]=y_GRF_Threshold('{quoted(raw_path)}',"
        f"0.001,{int(evidence['two_tailed'])},0.05,'{quoted(folder / 'reference.nii')}',"
        f"'{quoted(source_mask)}','{evidence['statistic_type']}',{evidence['df1']},{df2},[],[],{dlh});\n"
        f"[raw,~,raw_header]=y_ReadRPI('{quoted(raw_path)}');\n"
        f"y_Write(raw.*(z~=0),raw_header,'{quoted(folder / 'corrected.nii')}');\n"
        f"y_Write(z,header,'{quoted(folder / 'corrected_z.nii')}');\n"
        f"fid=fopen('{quoted(folder / 'summary.json')}','w');\n"
        "fprintf(fid,'%s',jsonencode(struct('minimum_cluster_size',minimum))); fclose(fid);\n",
        encoding="utf-8",
    )
    process = subprocess.run(
        [str(executable), "-batch", f"run('{quoted(script)}');"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    (folder / "stdout.log").write_text(process.stdout, encoding="utf-8")
    (folder / "stderr.log").write_text(process.stderr, encoding="utf-8")
    assert process.returncode == 0, (
        "Direct DPABI GRF reference failed; inspect retained local logs."
    )
    direct_statistic = nib.as_closest_canonical(nib.load(str(folder / "corrected.nii"))).get_fdata()
    direct_z = nib.as_closest_canonical(nib.load(str(folder / "corrected_z.nii"))).get_fdata()
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    assert summary["minimum_cluster_size"] == evidence["minimum_cluster_size"]
    assert np.array_equal(direct_statistic != 0, corrected != 0)
    assert np.array_equal(direct_z != 0, corrected_z != 0)
    return {
        "same_parameter_direct_dpabi_verified": True,
        "statistic_check": _compare(corrected, direct_statistic),
        "z_check": _compare(corrected_z, direct_z),
        "minimum_cluster_size": summary["minimum_cluster_size"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", choices=TESTS, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--real-matlab", action="store_true")
    args = parser.parse_args()
    if not args.real_matlab:
        parser.error("--real-matlab is required to confirm this synthetic acceptance")
    root = args.output_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    source, work = root / "source", root / "work"
    source.mkdir()
    work.mkdir()
    fixture = _fixtures(source, args.test)
    source_hashes = {
        path.relative_to(source).as_posix(): _sha256(path)
        for path in source.rglob("*")
        if path.is_file()
    }
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
    service = build_service(settings)
    try:
        project = service.projects.create_project(
            ProjectCreate(
                name="Synthetic statistical capability acceptance",
                source_roots=[str(source)],
                work_root=str(work / "project"),
            ),
            "capability-project",
        )
        imported = service.external_imports.inspect_external_analysis_import(
            ExternalAnalysisImportInspectRequest(
                project_id=project.project_id,
                source_path=str(source),
                layout="grouped",
            ),
            "capability-inspect",
        )
        assert not imported.inspection.blocking_issues
        included = service.external_imports.include_external_analysis_import(
            imported.import_id,
            ExternalAnalysisImportIncludeRequest(
                expected_version=imported.version,
                included_subject_ids=tuple(SUBJECTS[index] for index in INCLUSION_ORDER),
                group_order=("third", "first", "second"),
                coordinate_space="MNI",
                acknowledge_unknown_processing=True,
                actor="authorized_acceptance",
                reason="User-authorized synthetic public Worker numerical acceptance.",
                mask_relative_path="Mask.nii",
            ),
            "capability-include",
        )
        assert included.selection is not None
        demographics = service.external_imports.import_external_demographics(
            included.import_id,
            ExternalDemographicsImportRequest(
                source_path=str(source / "demographics.csv"),
                subject_id_column="ID",
                column_mapping={"predictor": "Predictor", "nuisance": "Nuisance"},
                expected_import_version=included.version,
            ),
            "capability-demographics",
        )
        covariates = (
            []
            if args.test == "one_sample_t"
            else [
                {"name": name, "centering": "grand_mean"}
                for name in (
                    ("nuisance",)
                    if args.test == "one_way_anova_ancova"
                    else ("nuisance", "predictor")
                )
            ]
        )
        use_grf = args.test in {"one_sample_t", "one_way_anova_ancova"}
        draft = StatisticsDraft.model_validate(
            {
                "test": args.test,
                "grouping_column": "group" if args.test == "one_way_anova_ancova" else None,
                "group_order": ["third", "first", "second"]
                if args.test == "one_way_anova_ancova"
                else [],
                "target_predictor": "predictor"
                if args.test in {"correlation", "regression"}
                else None,
                "one_sample_baseline": 2.0 if args.test == "one_sample_t" else None,
                "metric_artifact_type": "external.alff.z_score",
                "tail": "two_sided",
                "missing_value_policy": "error",
                "covariates": covariates,
                "correction_method": "grf" if use_grf else "fdr",
                "correction_parameters": {"voxel_p_threshold": 0.001, "cluster_p_threshold": 0.05}
                if use_grf
                else {"q_threshold": 0.05},
            }
        )
        proposal, issues = service.statistics.prepare_work_statistical_design_proposal(
            project_id=project.project_id,
            external_import_id=included.import_id,
            draft=draft,
        )
        assert proposal is not None and not issues, issues
        design, correction = proposal.design, proposal.correction
        if args.test == "one_way_anova_ancova":
            assert isinstance(correction, GrfCorrection)
            correction = GrfCorrection.model_validate(
                {
                    **correction.model_dump(mode="python"),
                    "smoothness_mode": "provided_dlh",
                    "smoothness_dlh": 0.05,
                }
            )
        assert design.subject_order != SUBJECTS
        if covariates:
            assert design.external_demographics_hash == demographics.content_hash
        reference = _reference(args.test, design.subject_order, fixture)
        # Regression deliberately supplies predictors in nuisance/target order.
        # Its matrix and unit contrast follow that order, unlike correlation,
        # where the target predictor is moved to the first model column.
        if args.test == "regression":
            reference["matrix"] = reference["matrix"][:, (0, 2, 1)]
            reference["contrast"] = np.asarray((0.0, 0.0, 1.0))
        created = service.statistics.create_statistical_design(
            StatisticalDesignCreate(
                project_id=project.project_id,
                expected_project_version=service.projects.get_project(project.project_id).version,
                input_manifest_hash=included.selection.input_manifest_hash,
                design=design,
                correction=correction,
            ),
            "capability-design",
        )
        plan = service.statistics.validate_statistical_design(
            created.plan_revision.plan_revision_id,
            StatisticalDesignValidationRequest(expected_version=created.plan_revision.version),
            "capability-validate",
        ).plan_revision
        service.plans.approve_plan(
            plan.plan_revision_id,
            ApprovalCreate(
                expected_version=plan.version,
                plan_hash=plan.plan_hash,
                actor="authorized_acceptance",
                decision=ApprovalDecision.APPROVED,
                reason="User-authorized small synthetic MATLAB capability acceptance.",
            ),
            "capability-approve",
        )
        run = service.runs.create_statistics_run(
            StatisticsRunCreate(
                project_id=project.project_id,
                statistical_design_revision_id=plan.plan_revision_id,
                expected_plan_hash=plan.plan_hash,
                execution_backend=ExecutionBackend.MATLAB,
                real_execution_confirmed=True,
            ),
            "capability-run",
        )
        print(f"QUEUED {args.test} {run.run_id}", flush=True)
        assert build_worker(service, worker_id="statistical-capability-acceptance").run_once()
        completed = service.runs.get_run(run.run_id)
        assert completed.state.value == "succeeded", (
            "Capability Worker did not complete successfully; inspect retained local logs."
        )
        artifacts = service.repository.list_artifacts(run.run_id)
        paths = {
            item.artifact_type: Path(project.work_root) / run.run_id / item.relative_path
            for item in artifacts
        }
        assert all(
            _sha256(Path(project.work_root) / run.run_id / item.relative_path) == item.checksum
            and (Path(project.work_root) / run.run_id / item.relative_path).stat().st_size
            == item.size_bytes
            for item in artifacts
        )
        raw_image = nib.as_closest_canonical(
            nib.load(str(paths["statistics.uncorrected_statistical_map"]))
        )
        raw = raw_image.get_fdata()
        expected_geometry = nib.as_closest_canonical(
            nib.Nifti1Image(fixture["mask"], fixture["affine"])
        )
        assert np.allclose(raw_image.affine, expected_geometry.affine)
        mask = expected_geometry.get_fdata().astype(bool)
        for key in ("statistic", "effect"):
            volume = np.zeros((10, 10, 10))
            volume[fixture["mask"].astype(bool)] = reference[key]
            canonical = nib.as_closest_canonical(nib.Nifti1Image(volume, fixture["affine"]))
            reference[key] = canonical.get_fdata()[mask]
        statistic_check = _compare(raw[mask], reference["statistic"])
        effect = nib.as_closest_canonical(nib.load(str(paths["statistics.effect_map"]))).get_fdata()
        effect_check = _compare(effect[mask], reference["effect"])
        matrix_evidence = json.loads(paths["statistics.design_matrix"].read_text(encoding="utf-8"))
        assert np.allclose(matrix_evidence["matrix"], reference["matrix"])
        assert tuple(matrix_evidence["ordered_subject_ids"]) == design.subject_order
        contrast_evidence = json.loads(paths["statistics.contrast"].read_text(encoding="utf-8"))
        assert np.allclose(contrast_evidence["contrast"], reference["contrast"])
        assert matrix_evidence["residual_df"] == reference["df"]
        correction_evidence = json.loads(
            paths["statistics.correction_evidence"].read_text(encoding="utf-8")
        )
        corrected = nib.as_closest_canonical(
            nib.load(str(paths["statistics.corrected_statistical_map"]))
        ).get_fdata()
        corrected_z = (
            nib.as_closest_canonical(nib.load(str(paths["statistics.corrected_z_map"]))).get_fdata()
            if use_grf
            else None
        )
        correction_check = _verify_correction(
            args.test,
            raw if use_grf else raw[mask],
            corrected if use_grf else corrected[mask],
            correction_evidence,
            reference["df"],
            corrected_z,
            raw_image.header["descrip"].tobytes().decode(errors="replace").rstrip("\x00"),
        )
        if use_grf:
            assert configuration.matlab_executable is not None and corrected_z is not None
            correction_check["direct_dpabi"] = _direct_grf(
                root,
                configuration.matlab_executable,
                paths["statistics.uncorrected_statistical_map"],
                source / "Mask.nii",
                corrected,
                corrected_z,
                correction_evidence,
            )
        result = service.repository.list_statistical_results(
            project_id=project.project_id, run_id=run.run_id
        )[0]
        detail = service.runs.get_statistical_result(result.result_id)
        assert detail.mode == "real" and not detail.non_scientific
        assert detail.method_summary is not None
        assert detail.method_summary.correction_evidence_status == "verified"
        clusters = StatisticalResultManifest.model_validate(detail.manifest).clusters
        assert detail.cluster_count == len(clusters)
        assert sum(cluster.extent_voxels for cluster in clusters) == int(
            np.count_nonzero(corrected)
        )
        if args.test == "one_way_anova_ancova":
            assert all(cluster.direction is None for cluster in clusters)
            assert np.all(raw >= 0) and np.all(corrected >= 0)
        assert all(
            _sha256(source / relative) == checksum for relative, checksum in source_hashes.items()
        )
        (root / "report.json").write_text(detail.report_json, encoding="utf-8")
        (root / "report.md").write_text(detail.report_markdown, encoding="utf-8")
        summary = {
            "status": "pass",
            "mode": "real_matlab_synthetic_public_worker",
            "test": args.test,
            "state": completed.state.value,
            "run_id": run.run_id,
            "result_id": result.result_id,
            "subject_count": len(SUBJECTS),
            "subject_order": design.subject_order,
            "demographics_revision_id": design.external_demographics_revision_id,
            "plan_hash": plan.plan_hash,
            "result_bundle_hash": detail.bundle_hash,
            "statistic_check": statistic_check,
            "effect_check": effect_check,
            "correction_check": correction_check,
            "source_files_unchanged": True,
            "software_versions": detail.method_summary.software_versions,
            "significant_voxel_count": int(np.count_nonzero(corrected)),
            "cluster_count": len(clusters),
            "artifact_count": len(artifacts),
            "artifacts_integrity_verified": True,
            "limitations": [
                "Synthetic service/Worker acceptance; "
                "no browser or real Provider planning is exercised."
            ],
        }
        (root / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"STATISTICAL_CAPABILITY_ACCEPTANCE_PASS {args.test}", flush=True)
    finally:
        service.close()


if __name__ == "__main__":
    main()
