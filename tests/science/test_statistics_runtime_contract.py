"""Runtime contracts for the checked DPABI signatures and statistical evidence."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def statistics_template() -> str:
    return Path("matlab/templates/run_statistics.m.tmpl").read_text(encoding="utf-8")


def test_statistics_runtime_records_observed_versions_and_signed_clusters() -> None:
    template = statistics_template()
    assert "'R2023b'" not in template
    assert "'V8.2_240510'" not in template
    assert "version('-release')" in template
    assert "[spm_version, spm_revision] = spm('Ver');" in template
    assert "fileread(which('dpabi'))" in template
    assert "nargin(request.function)" in template
    assert "cluster_inference = 'descriptive_unthresholded';" in template
    assert "for direction = [1, -1]" in template
    assert "bwconncomp(finite_values & (direction .* values > 0), 26)" in template


def test_statistics_uses_frozen_design_variance_for_effects() -> None:
    template = statistics_template()
    assert "design_matrix = double(request.design_matrix);" in template
    assert "contrast = reshape(double(request.contrast), [], 1);" in template
    assert "contrast_standard_error = norm(design_r' \\ contrast);" in template
    assert "effect_scale = correction_factor * 2 * contrast_standard_error;" in template
    assert "'effect_definition', effect_definition" in template
    assert "'one_sample_baseline', request.one_sample_baseline" in template


@pytest.mark.skipif(
    os.getenv("RSFMRI_RUN_MATLAB_STATISTICS_TESTS") != "1",
    reason="explicit opt-in required for real MATLAB algebra checks",
)
def test_real_matlab_effect_algebra_matches_adjusted_and_unadjusted_designs(
    tmp_path: Path,
) -> None:
    """Exercise the actual template block, without images or installed toolboxes.

    The confounded design has c'(X'X)^-1c = 5/12; the unadjusted
    design must reduce to sqrt(1/n1 + 1/n2). One-sample centering and
    the paired +1/-1 plus subject-indicator design are checked separately.
    """
    executable = os.getenv("RSFMRI_MATLAB_EXECUTABLE")
    if not executable:
        pytest.fail("RSFMRI_MATLAB_EXECUTABLE is required for the opted-in check")
    template = statistics_template()
    effect_block = template.split("design_matrix = double(request.design_matrix);", 1)[1]
    effect_block = (
        "design_matrix = double(request.design_matrix);"
        + effect_block.split(
            "write_json(fullfile(run_directory, 'output', 'design_matrix.json')", 1
        )[0]
    )
    script = tmp_path / "verify_effect_algebra.m"
    script.write_text(
        """
request.test = 'independent_two_sample_t';
request.design_matrix = [1 1 -2; 1 1 -1; 1 1 0; -1 1 0; -1 1 1; -1 1 2];
request.contrast = [1 0 0]; request.residual_df = 3;
[actual, definition] = effect_from_template(request);
assert(abs(actual - (1-3/11)*sqrt(5/3)) < 1e-12);
assert(contains(definition, 'adjusted group difference'));
request.design_matrix = [ones(3,1) ones(3,1); -ones(4,1) ones(4,1)];
request.contrast = [1 0]; request.residual_df = 5;
actual = effect_from_template(request);
assert(abs(actual - (1-3/19)*sqrt(1/3+1/4)) < 1e-12);
request.test = 'one_sample_t'; request.design_matrix = [ones(4,1) [-3;-1;1;3]];
request.contrast = [1 0]; request.residual_df = 2;
actual = effect_from_template(request);
assert(abs(actual - 1/sqrt(4)) < 1e-12);
request.test = 'paired_t';
request.design_matrix = [ones(3,1) eye(3); -ones(3,1) eye(3)];
request.contrast = [1 0 0 0]; request.residual_df = 2;
actual = effect_from_template(request);
assert(abs(actual - 1/sqrt(3)) < 1e-12);
disp('STATISTICS_EFFECT_ALGEBRA_PASS');
function [effect_scale, effect_definition] = effect_from_template(request)
"""
        + effect_block
        + "\nend\n",
        encoding="utf-8",
    )
    quoted_path = script.as_posix().replace("'", "''")
    result = subprocess.run(
        [executable, "-nodisplay", "-nosplash", "-nodesktop", "-batch", f"run('{quoted_path}');"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STATISTICS_EFFECT_ALGEBRA_PASS" in result.stdout
