"""Explicitly opted-in, tiny synthetic MATLAB/DPABI execution and report smoke."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from neuroagent.application.reporting import build_statistical_reproducibility_report
from neuroagent.domain.fmri.results import (
    RegisteredArtifactMetadata,
    StatisticalArtifactRole,
    StatisticalResultManifest,
)
from neuroagent.domain.fmri.statistics import (
    AnalysisImage,
    CorrectionSpec,
    CovariateColumn,
    CovariateValue,
    FdrCorrection,
    GrfCorrection,
    StatisticalDesignRevision,
    StatisticalTest,
)
from neuroagent.execution import (
    ArtifactPathBinding,
    ControlledMatlabExecutor,
    ExpectedArtifact,
    MatlabEnvironment,
    MatlabJobSpec,
    MatlabTemplateRenderer,
    StatisticsJobPayload,
)
from neuroagent.tools.dpabi_v82 import DpabiV82Adapter

pytestmark = pytest.mark.skipif(
    os.getenv("RSFMRI_RUN_MATLAB_STATISTICS_TESTS") != "1",
    reason="real MATLAB synthetic smoke requires explicit opt-in",
)


def _design(kind: StatisticalTest) -> StatisticalDesignRevision:
    paired = kind is StatisticalTest.PAIRED_T
    two_sample = kind is StatisticalTest.INDEPENDENT_TWO_SAMPLE_T
    anova = kind is StatisticalTest.ONE_WAY_ANOVA_ANCOVA
    correlation = kind is StatisticalTest.CORRELATION
    regression = kind is StatisticalTest.REGRESSION
    subjects = tuple(f"synthetic-{i:02d}" for i in range(1, 7))
    values = (-2, -1, 0, 0, 1, 2)
    nuisance_values = (1, -1, -1, 1, 1, -1)
    group_order = (
        ("first", "second") if two_sample else ("first", "second", "third") if anova else ()
    )
    images = tuple(
        AnalysisImage(
            subject_id=subject,
            artifact_id=f"image-{condition}-{index}",
            group=(
                ("first" if index < 3 else "second")
                if two_sample
                else group_order[index // 2]
                if anova
                else None
            ),
            condition=condition if paired else None,
        )
        for condition in (("first", "second") if paired else ("only",))
        for index, subject in enumerate(subjects)
    )
    covariate_specs: list[tuple[str, tuple[int, ...]]] = []
    if two_sample:
        covariate_specs = [("synthetic_covariate", values)]
    elif correlation or regression:
        covariate_specs = [("target_predictor", values), ("nuisance", nuisance_values)]
    elif anova:
        covariate_specs = [("synthetic_covariate", (-1, 1, 0, 0, -1, 1))]
    covariates = tuple(
        CovariateColumn(
            name=name,
            centering="grand_mean",
            values=tuple(
                CovariateValue(subject_id=subject, value=value)
                for subject, value in zip(subjects, column_values, strict=True)
            ),
        )
        for name, column_values in covariate_specs
    )
    columns = (
        7
        if paired
        else 2 + len(covariates)
        if two_sample
        else len(group_order) + len(covariates)
        if anova
        else 1 + len(covariates)
    )
    contrast = (
        (1.0, 1.0, *(0.0 for _ in range(columns - 2)))
        if anova
        else (0.0, 1.0, *(0.0 for _ in range(columns - 2)))
        if regression
        else (1.0, *(0.0 for _ in range(columns - 1)))
    )
    return StatisticalDesignRevision(
        revision_id=kind.value,
        test=kind,
        subject_order=subjects,
        images=images,
        group_order=group_order,
        condition_order=("first", "second") if paired else (),
        covariates=covariates,
        contrast=contrast,
        one_sample_baseline=2.0 if kind is StatisticalTest.ONE_SAMPLE_T else None,
        mask_artifact_id="mask",
        tail="one_sided_negative" if paired else "two_sided",
        missing_value_policy="error",
        qc_review_revision_id="synthetic-qc",
        qc_review_hash="a" * 64,
    )


def _matlab(executable: Path, script: Path) -> None:
    quoted = script.as_posix().replace("'", "''")
    result = subprocess.run(
        [str(executable), "-batch", f"run('{quoted}');"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    script.with_suffix(".stdout.log").write_text(result.stdout, encoding="utf-8")
    script.with_suffix(".stderr.log").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stdout[-5000:] + result.stderr[-5000:]


def test_real_matlab_statistics_and_report_smoke() -> None:
    root = Path(tempfile.mkdtemp(prefix="rsfmri-statistics-smoke-"))
    print(f"Retained synthetic smoke evidence: {root}")
    executable = Path(os.environ["RSFMRI_MATLAB_EXECUTABLE"])
    environment = MatlabEnvironment(
        matlab_executable=executable,
        matlab_root=executable.parent.parent,
        spm_path=Path(os.environ["RSFMRI_SPM_DIR"]),
        dpabi_path=Path(os.environ["RSFMRI_DPABI_DIR"]),
        matlab_version="runtime-observed",
        spm_version="runtime-observed",
        dpabi_version="runtime-observed",
    )
    renderer = MatlabTemplateRenderer(Path("matlab/templates"))
    executor = ControlledMatlabExecutor(renderer, environment, root, allow_real_execution=True)
    adapter = DpabiV82Adapter()
    jobs = []
    for kind in (
        StatisticalTest.ONE_SAMPLE_T,
        StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
        StatisticalTest.PAIRED_T,
        StatisticalTest.CORRELATION,
        StatisticalTest.REGRESSION,
        StatisticalTest.ONE_WAY_ANOVA_ANCOVA,
    ):
        design = _design(kind)
        call = adapter.project_statistics(design)
        correction: CorrectionSpec | None = None
        if kind in {
            StatisticalTest.ONE_SAMPLE_T,
            StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
        }:
            correction = FdrCorrection(
                method="fdr",
                q_threshold=0.05,
                mask_artifact_id="mask",
                statistic_type="T",
                df1=call.residual_df,
                df2=None,
            )
        elif kind is StatisticalTest.PAIRED_T:
            correction = GrfCorrection(
                method="grf",
                voxel_p_threshold=0.001,
                cluster_p_threshold=0.05,
                two_tailed=False,
                mask_artifact_id="mask",
                statistic_type="T",
                df1=call.residual_df,
                df2=None,
                smoothness_mode="provided_dlh",
                smoothness_dlh=0.05,
            )
        elif kind is StatisticalTest.CORRELATION:
            correction = FdrCorrection(
                method="fdr",
                q_threshold=0.05,
                mask_artifact_id="mask",
                statistic_type="R",
                df1=call.residual_df,
                df2=None,
            )
        elif kind is StatisticalTest.ONE_WAY_ANOVA_ANCOVA:
            correction = GrfCorrection(
                method="grf",
                voxel_p_threshold=0.001,
                cluster_p_threshold=0.05,
                two_tailed=False,
                mask_artifact_id="mask",
                statistic_type="F",
                df1=2,
                df2=call.residual_df,
                smoothness_mode="provided_dlh",
                smoothness_dlh=0.05,
            )
        roles = {
            "design_matrix": "output/design_matrix.json",
            "contrast": "output/contrast.json",
            "uncorrected_statistical_map": "output/statistic.nii",
            "effect_map": "output/effect.nii",
            "cluster_table": "output/clusters.tsv",
            "execution_log": "logs/matlab.log",
            "software_version_evidence": "output/software-version.json",
            "correction_evidence": "output/correction.json",
        }
        if correction:
            roles["corrected_statistical_map"] = "output/statistic_corrected.nii"
            if isinstance(correction, GrfCorrection):
                roles["corrected_z_map"] = "output/statistic_corrected_z.nii"
        job = MatlabJobSpec(
            job_id=kind.value,
            run_id=kind.value,
            kind="dpabi_statistics",
            plan_hash="b" * 64,
            approval_record_id="explicit-synthetic-smoke-authorization",
            input_manifest_hash="c" * 64,
            timeout_seconds=180,
            artifact_bindings=tuple(
                ArtifactPathBinding(
                    artifact_id=item, relative_path=f"input/{item}.nii", read_only=True
                )
                for item in [*(image.artifact_id for image in design.images), "mask"]
            ),
            expected_artifacts=tuple(
                ExpectedArtifact(
                    artifact_type=f"statistics.{role}", relative_pattern=path, required=True
                )
                for role, path in roles.items()
            ),
            payload=StatisticsJobPayload(
                statistics=call,
                correction=adapter.project_correction(correction) if correction else None,
            ),
        )
        rendered = renderer.render(job, environment, root)
        jobs.append((job, design, correction, rendered))
    bootstrap = jobs[0][3].run_directory / "scripts/bootstrap.m"
    setup = root / "generate_fixtures.m"
    setup.write_text(
        "run('" + bootstrap.as_posix().replace("'", "''") + "');\n" + _FIXTURE_SCRIPT,
        encoding="utf-8",
    )
    _matlab(executable, setup)
    summary = []
    for job, design, correction, rendered in jobs:
        result = executor.execute(job, is_cancelled=lambda: False)
        assert result.status.value == "succeeded", result.stdout[-5000:] + result.stderr[-5000:]
        evidence = json.loads((rendered.run_directory / "output/software-version.json").read_text())
        assert evidence["dpabi"] not in {"unreported", "runtime-observed"}
        assert evidence["matlab"].startswith("R") and evidence["spm"].startswith("SPM")
        with (rendered.run_directory / "output/clusters.tsv").open(newline="") as stream:
            clusters = list(csv.DictReader(stream, delimiter="\t"))
        if design.test is StatisticalTest.PAIRED_T:
            assert clusters
            assert all(float(row["peak_statistic"]) < 0 for row in clusters)
        artifacts = tuple(
            RegisteredArtifactMetadata(
                artifact_id=f"artifact-{i}",
                role=StatisticalArtifactRole(item.artifact_type.removeprefix("statistics.")),
                artifact_type=item.artifact_type,
                relative_path=item.relative_path,
                checksum_sha256=item.sha256,
                size_bytes=item.size_bytes,
                provenance_hash=hashlib.sha256(
                    (rendered.run_directory / "provenance.json").read_bytes()
                ).hexdigest(),
            )
            for i, item in enumerate(result.registered_artifacts)
        )
        manifest = StatisticalResultManifest(
            result_id=job.run_id,
            run_id=job.run_id,
            design_revision_id=design.revision_id,
            mode="real",
            non_scientific=False,
            correction=correction,
            cluster_connectivity_definition=(
                "26-neighbor, positive and negative effects separately; "
                "uncorrected clusters descriptive"
            ),
            artifacts=artifacts,
        )
        report = build_statistical_reproducibility_report(
            manifest=manifest,
            design=design,
            correction=correction,
            qc_review_hash="a" * 64,
            environment_hash=hashlib.sha256(
                json.dumps(evidence, sort_keys=True).encode()
            ).hexdigest(),
            plan_hash=job.plan_hash,
        )
        (rendered.run_directory / "report.md").write_text(report.markdown, encoding="utf-8")
        (rendered.run_directory / "report.json").write_text(report.json_text, encoding="utf-8")
        summary.append(
            {
                "test": design.test.value,
                "software": evidence,
                "artifact_count": len(artifacts),
                "cluster_count": len(clusters),
                "report_hash": report.bundle_hash,
            }
        )
    verify = root / "verify_outputs.m"
    verify.write_text(
        "run('" + bootstrap.as_posix().replace("'", "''") + "');\n" + _VERIFY_SCRIPT,
        encoding="utf-8",
    )
    _matlab(executable, verify)
    (root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


_FIXTURE_SCRIPT = r"""
root = fileparts(mfilename('fullpath'));
    cases = {'one_sample_t', 'independent_two_sample_t', 'paired_t', ...
        'correlation', 'regression', 'one_way_anova_ancova'};
[x,y,z] = ndgrid(1:10,1:10,1:10);
mask = x>=2 & x<=9 & y>=2 & y<=9 & z>=2 & z<=9;
signal = 1 - 2*(x>5);
for k = 1:numel(cases)
    folder = fullfile(root,cases{k});
    req = jsondecode(fileread(fullfile(folder,'config','statistics.json')));
    X = req.design_matrix; n = size(X,1); c = req.contrast(:);
    Y = zeros(n,numel(x));
    for i = 1:n
        noise = 0.2*sin(0.37*i*x+0.21*y+0.13*z) + 0.07*cos(i*0.31*z+0.17*y);
        volume = 4 + X(i,1)*signal + noise;
        if strcmp(cases{k}, 'one_sample_t'), volume = 2 + signal + noise; end
        if strcmp(cases{k}, 'independent_two_sample_t'), volume = volume + 0.4*X(i,3); end
        if strcmp(cases{k}, 'correlation'), volume = 4 + 1.3*X(i,1)*signal + 0.5*X(i,3) + noise; end
        if strcmp(cases{k}, 'regression'), volume = 4 + 1.3*X(i,2)*signal + 0.5*X(i,3) + noise; end
        if strcmp(cases{k}, 'one_way_anova_ancova')
            volume = 4 + 1.2*X(i,1)*signal + 0.8*X(i,2)*signal + 0.3*X(i,4) + noise;
        end
        Y(i,:) = volume(:)';
        paths = {};
        for group_index = 1:numel(req.dependent_groups)
            group_paths = req.dependent_groups(group_index).paths;
            for path_index = 1:numel(group_paths)
                paths{end+1} = group_paths(path_index).value; %#ok<AGROW>
            end
        end
        save_image(paths{i}, volume);
    end
    save_image(req.mask_path, double(mask));
    if strcmp(cases{k}, 'one_sample_t'), Y = Y-req.one_sample_baseline; end
    beta = (X'*X)\(X'*Y); residual = Y-X*beta;
    sigma = sqrt(sum(residual.^2,1)/req.residual_df);
    if strcmp(cases{k}, 'one_way_anova_ancova')
        X0 = X(:,~logical(c'));
        beta0 = (X0'*X0)\(X0'*Y); residual0 = Y-X0*beta0;
        SSE0 = sum(residual0.^2,1); SSE = sum(residual.^2,1);
        df1 = sum(c~=0);
        expected_stat = ((SSE0-SSE)/df1)./(SSE/req.residual_df);
        expected_effect = (SSE0-SSE)./SSE;
    else
        expected_t = (c'*beta)./(sigma*sqrt(c'*((X'*X)\c)));
        if strcmp(cases{k}, 'correlation')
            expected_stat = expected_t./sqrt(req.residual_df + expected_t.^2);
        else
            expected_stat = expected_t;
        end
        if strcmp(cases{k}, 'one_sample_t')
        expected_effect = (c'*beta)./sigma;
        elseif strcmp(cases{k}, 'independent_two_sample_t')
        expected_effect = (1-3/(4*req.residual_df-1))*2*(c'*beta)./sigma;
        elseif strcmp(cases{k}, 'paired_t')
        differences = Y(1:n/2,:)-Y(n/2+1:end,:);
        expected_effect = mean(differences,1)./std(differences,0,1);
        elseif strcmp(cases{k}, 'correlation')
            expected_effect = expected_stat;
        else
            expected_effect = c'*beta;
        end
    end
    save_image(fullfile(folder,'expected_t.nii'), reshape(expected_stat,size(x)).*mask);
    save_image(fullfile(folder,'expected_effect.nii'), reshape(expected_effect,size(x)).*mask);
end
disp('SYNTHETIC_FIXTURES_CREATED');
function save_image(path, data)
header = struct('fname',path,'dim',size(data),'dt',[64 0], ...
    'mat',diag([3 3 3 1]),'pinfo',[1;0;0],'descrip','deterministic synthetic smoke');
spm_write_vol(header,double(data));
end
"""

_VERIFY_SCRIPT = r"""
root = fileparts(mfilename('fullpath'));
cases = {'one_sample_t', 'independent_two_sample_t', 'paired_t', ...
    'correlation', 'regression', 'one_way_anova_ancova'};
for k = 1:numel(cases)
    folder = fullfile(root,cases{k});
    expected = y_ReadRPI(fullfile(folder,'expected_t.nii'));
    actual = y_ReadRPI(fullfile(folder,'output','statistic.nii'));
    assert(max(abs(actual(:)-expected(:))./(1+abs(expected(:)))) < 1e-6);
    expected_effect = y_ReadRPI(fullfile(folder,'expected_effect.nii'));
    effect = y_ReadRPI(fullfile(folder,'output','effect.nii'));
    assert(max(abs(effect(:)-expected_effect(:))./(1+abs(expected_effect(:)))) < 1e-6);
    if ~strcmp(cases{k}, 'one_way_anova_ancova')
        assert(any(actual(:)>0) && any(actual(:)<0));
    else
        assert(all(actual(:)>=0));
    end
    if k==1
        p = 2*tcdf(-abs(expected(expected~=0)),5);
        ps = sort(p); thresholds = (1:numel(ps))'/numel(ps)*0.05;
        q = ps(find(ps<=thresholds,1,'last'));
        corrected = y_ReadRPI(fullfile(folder,'output','statistic_corrected.nii'));
        expected_corrected = expected.*(2*tcdf(-abs(expected),5)<=q);
        assert(max(abs(corrected(:)-expected_corrected(:))) < 1e-4);
    elseif k==2
        correction_info = jsondecode(fileread(fullfile(folder,'output','correction.json')));
        design_info = jsondecode(fileread(fullfile(folder,'output','design_matrix.json')));
        assert(strcmp(correction_info.method, 'fdr'));
        assert(strcmp(correction_info.statistic_type, 'T'));
        assert(isfield(correction_info, 'actual_p_threshold'));
        corrected = y_ReadRPI(fullfile(folder,'output','statistic_corrected.nii'));
        p = 2*(1-tcdf(abs(actual),design_info.residual_df));
        expected_corrected = actual.*(p<=correction_info.actual_p_threshold);
        assert(max(abs(corrected(:)-expected_corrected(:))) < 1e-4);
    elseif k==3
        corrected = y_ReadRPI(fullfile(folder,'output','statistic_corrected.nii'));
        assert(any(corrected(:)<0) && ~any(corrected(:)>0));
        assert(max(abs(corrected(corrected~=0)-expected(corrected~=0))) < 1e-4);
    elseif strcmp(cases{k}, 'correlation')
        correction_info = jsondecode(fileread(fullfile(folder,'output','correction.json')));
        assert(strcmp(correction_info.method, 'fdr'));
        assert(isfield(correction_info, 'actual_p_threshold'));
        corrected = y_ReadRPI(fullfile(folder,'output','statistic_corrected.nii'));
        df = 3;
        % Match DPABI's R-to-p transform and operate on the actual single-
        % precision statistic map so a voxel on the BH cutoff is deterministic.
        p = 2*(1-tcdf(abs(actual).*sqrt(df./(1-actual.^2)),df));
        expected_corrected = actual.*(p<=correction_info.actual_p_threshold);
        assert(max(abs(corrected(:)-expected_corrected(:))) < 1e-4);
    elseif strcmp(cases{k}, 'one_way_anova_ancova')
        correction_info = jsondecode(fileread(fullfile(folder,'output','correction.json')));
        assert(strcmp(correction_info.method, 'grf'));
        assert(strcmp(correction_info.statistic_type, 'F'));
        corrected = y_ReadRPI(fullfile(folder,'output','statistic_corrected.nii'));
        corrected_z = y_ReadRPI(fullfile(folder,'output','statistic_corrected_z.nii'));
        assert(all(corrected(:)>=0) && all(corrected_z(:)>=0));
        design_info = jsondecode(fileread(fullfile(folder,'output','design_matrix.json')));
        assert(strcmp(design_info.statistic_type, 'F') && design_info.df1==2);
        clusters = readtable(fullfile(folder,'output','clusters.tsv'), ...
            'FileType','text','Delimiter','\t');
        assert(all(ismissing(clusters.direction)));
    end
    correction_info = jsondecode(fileread(fullfile(folder,'output','correction.json')));
    assert(isfield(correction_info, 'method'));
    fprintf('%s T/effect/correction numerical checks passed\n', cases{k});
end
disp('REAL_STATISTICS_SMOKE_PASS');
"""
