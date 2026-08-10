---
name: plan-dpabi-preprocessing
description: Build and review a structured DPABI V8.2 preprocessing plan from a frozen rs-fMRI manifest and an explicit study protocol. Use when Codex must map approved preprocessing choices to DPARSFA capabilities, explain missing parameters, or prepare a dry-run plan without starting MATLAB.
---

# Plan DPABI Preprocessing

1. Read `skill.yaml` and validate structured values with `parameters.schema.json`.
2. Require a frozen manifest, a reviewed base DPARSFA configuration artifact, and provenance for every scientific parameter. Record explicit choices for dummy scans, slice timing/order/reference, realignment, nuisance regression and head motion, GSR, normalization, detrending, filtering, scrubbing, and smoothing; disabled operations are explicit values rather than omitted defaults.
3. Check MATLAB R2023b, SPM12, and DPABI V8.2 compatibility. Treat software validity, technical validity, and scientific suitability as separate results.
4. Classify DICOM, BIDS NIfTI, plain NIfTI, and DPABI-ready inputs read-only. Copy only manifest-bound files into staging. For DICOM, emit a typed `DPABI_BIDS_Converter_run` plan but do not execute it.
5. Compile only registered capabilities. Never emit free MATLAB, shell commands, absolute output paths, state transitions, or implicit scientific defaults.
6. Produce a dry-run preview containing inputs, ordered steps, parameter sources, expected artifacts, QC gates, version locks, and unresolved questions.

Do not start MATLAB or approve the plan. Any input, parameter, Skill, Tool, template, or environment change requires a new plan revision.
