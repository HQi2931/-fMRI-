---
name: run-tabular-ml
description: Render and execute an approved classical Python table-model template in an isolated workspace, preserving design, environment, logs, and figures.
---

# Run tabular ML

1. Accept only an approved immutable ML design and registered table Artifact IDs.
2. Render the fixed Python template; never accept free Python or shell text from a model.
3. Run in staging with a fixed seed, bounded resources, and subject-level split checks.
4. Register metrics, ROC/PR/calibration figures, feature importance, logs, and environment provenance.
5. Do not retry with alternate models or thresholds to improve significance.
