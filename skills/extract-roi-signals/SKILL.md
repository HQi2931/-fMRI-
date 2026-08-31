---
name: extract-roi-signals
description: Design and validate DPABI ROI signal extraction and deterministic long or wide CSV/XLSX exports without running MATLAB automatically.
---

# Extract ROI signals

> 状态：契约/预览包，未注册运行时；本 MVP 只校验与导出已登记记录，真实 `y_ExtractROISignal` 执行待 v0.2.0 单独接线与批准。

1. Require registered functional and atlas or mask Artifact IDs, TR, frequency band (band_low_hz / band_high_hz), labels, and scrubbing policy.
2. Validate Nyquist and frequency-band consistency, scrubbing timing, and ROI-index de-duplication; grid, time-point, and label contracts are declared lineage and not yet enforced in the deterministic preview.
3. Map approved parameters to DPABI V8.2 `y_ExtractROISignal` only through a fixed Tool adapter (not yet wired).
4. Produce subject-aligned long and wide tables with hashes and lineage.
5. Ask for approval before executing MATLAB or writing exports to staging.
