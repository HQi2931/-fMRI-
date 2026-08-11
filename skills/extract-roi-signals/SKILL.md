---
name: extract-roi-signals
description: Design and validate DPABI ROI signal extraction and deterministic long or wide CSV/XLSX exports without running MATLAB automatically.
---

# Extract ROI signals

1. Require registered functional and atlas or mask Artifact IDs, TR, frequency band, labels, and scrubbing policy.
2. Validate Nyquist, grid, time-point, ROI-index, and label contracts.
3. Map approved parameters to DPABI V8.2 `y_ExtractROISignal` only through a fixed Tool adapter.
4. Produce subject-aligned long and wide tables with hashes and lineage.
5. Ask for approval before executing MATLAB or writing exports to staging.
