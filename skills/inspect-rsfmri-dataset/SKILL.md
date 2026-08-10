---
name: inspect-rsfmri-dataset
description: Inspect a resting-state fMRI dataset or frozen manifest for DICOM, BIDS NIfTI, generic NIfTI, or DPABI-ready compatibility. Use when Codex must produce a read-only input report, identify subject/T1/fMRI pairing problems, or list conversion prerequisites before any DPABI workflow is planned.
---

# Inspect an rs-fMRI Dataset

1. Treat every source root as read-only; never rename, move, delete, convert, or repair source data.
2. Read `skill.yaml` and validate the request against `parameters.schema.json`.
3. Require an allowed source root, explicit dataset reference, and frozen subject manifest hash.
4. Report detected layout, subject/session pairing, required metadata, grid/TR inconsistencies, duplicate IDs, missing scans, and path-boundary failures.
5. Separate format compatibility from scientific suitability. Do not infer slice order, TR, exclusion rules, or preprocessing choices.
6. Return structured `blocking`, `warning`, and `information` findings plus a proposed staging plan. Do not execute conversion.

Stop and request clarification when data format, subject identity, or source ownership is ambiguous.
