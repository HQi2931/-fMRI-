---
name: plan-dpabi-preprocessing
description: Build and review a structured DPABI V8.2 preprocessing plan from a frozen rs-fMRI manifest and an explicit study protocol. Use when Codex must map approved preprocessing choices to DPARSFA capabilities, explain missing parameters, or prepare a dry-run plan without starting MATLAB.
---

# Plan DPABI Preprocessing

1. Read `skill.yaml` and validate structured values with `parameters.schema.json`.
2. Require a frozen manifest, a reviewed base DPARSFA configuration artifact, and provenance for every scientific parameter. Record explicit choices for dummy scans, slice timing/order/reference, realignment, nuisance regression and head motion, GSR, normalization, detrending, filtering, scrubbing, and smoothing; disabled operations are explicit values rather than omitted defaults.
3. Check configured MATLAB/SPM/DPABI entry points and source compatibility, not hardcoded version labels. Treat software validity, technical validity, and scientific suitability as separate results. T1 preparation and coregistration are explicit choices within the same preprocessing run; pair anatomy and functional input per frozen subject. `New Segment` and DARTEL template construction are a bound choice: selecting either requires the other; `Segment` and disabled segmentation require DARTEL off. Check input, output and cohort hashes automatically; do not add intermediate human approvals.
4. When manual T1 reorientation is skipped, use the explicitly approved prepared structural image; retain coregistration and the selected Segment or New Segment/DARTEL pair. `preprocessing.t1.bet` is an explicit optional choice: true enables DPABI's native BET for both T1 and mean functional images before coregistration, using the installed DPABI settings. Omission preserves the legacy disabled behavior, not an inferred recommendation. State this scope in the preview. BET and coregistration must not enable DARTEL or interactive reorientation implicitly.
5. Classify DICOM, BIDS NIfTI, plain NIfTI, and DPABI-ready inputs read-only. Copy only manifest-bound files into staging. For DICOM, emit a typed `DPABI_BIDS_Converter_run` plan but do not execute it.
6. Compile only registered capabilities. Never emit free MATLAB, shell commands, absolute output paths, state transitions, or implicit scientific defaults.
7. Produce a dry-run preview containing inputs, ordered steps, parameter sources, expected artifacts, QC gates, version locks, and unresolved questions.

Do not start MATLAB or approve the plan. Any input, parameter, Skill, Tool, template, or environment change requires a new plan revision.
