---
name: plan-reho
description: Validate and compile a ReHo metric plan for DPABI V8.2. Use when Codex must enforce unsmoothed ReHo input, validate the 7/19/27 neighborhood and temporal filter lineage, prevent duplicate result smoothing, or prepare a reviewable ReHo plan without executing MATLAB.
---

# Plan ReHo

1. Read `skill.yaml` and validate the request with `parameters.schema.json`.
2. Require explicit TR, filter state, neighborhood, mask, output scaling, smoothing route, FWHM when enabled, and provenance.
3. Permit only `7`, `19`, or `27` neighboring voxels. Validate any filter band against Nyquist.
4. Block spatially smoothed input. ReHo must consume an unsmoothed time series.
5. Require an explicitly unfiltered input checkpoint. The reviewed DAG owns any approved
   temporal-filter step, so even an already filtered checkpoint with the same band is blocked.
6. Block simultaneous `CalReHo.SmoothReHo` and global `Smooth.Timing='OnResults'`; require a valid mask when SmoothReHo or scaled products are requested.
7. Compile an immutable plan and QC gate. Never choose neighborhood, band, smoothing, or exclusion rules for the user, and never start MATLAB.
