---
name: review-rsfmri-qc
description: Review resting-state fMRI metric provenance and quality-control evidence before group statistics. Use when Codex must summarize ALFF/fALFF or ReHo QC, verify one primary map per frozen subject, identify blocking evidence gaps, or prepare an inclusion/exclusion review for a human decision.
---

# Review rs-fMRI QC

1. Read `skill.yaml` and validate the review request with `parameters.schema.json`.
2. Match every metric map to the frozen manifest by explicit `subject_id`; never rely on directory or filename ordering.
3. Verify grid, mask, space, frequency band, scaling, ReHo neighborhood, smoothing, scrubbing, software versions, and producer hashes from Artifact provenance.
4. Mark missing or inconsistent required evidence as blocking. Keep warnings separate from blockers.
5. Present proposed inclusions and exclusions with evidence, but never change the subject list or approve QC on the user's behalf.
6. Produce a new immutable QC review revision. Statistics remain blocked until a human approves that exact revision and inclusion order.
