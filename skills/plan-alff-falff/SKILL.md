---
name: plan-alff-falff
description: Validate and compile an ALFF/fALFF metric plan for DPABI V8.2. Use when Codex must check TR and Nyquist limits, prevent filtered fALFF input, select explicit output scaling and mask contracts, explain DPARSFA ordering, or produce a reviewable plan without executing MATLAB.
---

# Plan ALFF/fALFF

1. Read `skill.yaml` and validate the request with `parameters.schema.json`.
2. Require explicit TR, frequency band, requested ALFF/fALFF endpoints, scaling, mask choice, smoothing choice, and provenance.
3. Enforce `0 <= low < high <= 1/(2*TR)`. Block standard fALFF when input lineage is already temporally filtered.
4. Block `BeforeNormalize` filtering for this reviewed protocol. DPABI V8.2 calculates ALFF and fALFF together before `AfterNormalize` filtering.
5. Require a grid-compatible mask for global-mean or z-score outputs. Do not infer a mask from filenames.
6. Compile an immutable plan containing both companion products when `IsCalALFF=1`, while marking only the user-selected outputs as primary endpoints.
7. Return blockers and confirmation items; never start a Tool, Workflow, or MATLAB process.
