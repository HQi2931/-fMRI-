---
name: plan-rsfmri-statistics
description: Validate and compile an explicitly aligned rs-fMRI group-statistics plan for DPABI V8.2. Use when Codex must plan one-sample, independent two-sample, or paired t tests, correlation or regression with covariates, and an explicitly selected FDR, GRF, or no-correction decision.
---

# Plan rs-fMRI Statistics

1. Read `skill.yaml` and validate the request with `parameters.schema.json`.
2. Require an approved QC revision and exactly reuse its frozen subject order.
3. Bind each image, group, condition, pair, and covariate by `subject_id`; never use filesystem ordering.
4. Require explicit group or condition direction, covariate centering (`none`, `grand_mean`, or `within_group`), missing-value policy, contrast, mask, tail, and one-sample baseline where applicable. Do not infer a centering default.
5. Model the statistical test and multiple-comparison correction separately. Require an explicit correction choice: FDR with `q`, GRF with voxel `p` and cluster `p`, or an explicit no-correction decision. A missing correction value is unresolved, not equivalent to no correction. Also require explicit tail, mask, statistic type, and degrees of freedom.
6. Validate design-matrix and contrast dimensions in both directions. Never search across methods or thresholds for the most significant result.
7. Compile a new immutable plan revision when any design or correction choice changes. Do not run MATLAB or interpret results as clinical findings.
