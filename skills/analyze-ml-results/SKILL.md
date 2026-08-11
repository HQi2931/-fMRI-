---
name: analyze-ml-results
description: Review approved classical ML outputs and generate reproducible ROC, PR, calibration, confusion-matrix, and feature-importance figures without changing the analysis design.
---

# Analyze ML results

1. Verify metrics and figures match the frozen design, subject split, and environment.
2. Report uncertainty and class balance alongside ROC/AUC; do not treat a single metric as proof.
3. Create figures from registered result artifacts and retain their hashes.
4. Flag leakage, overfitting, missing calibration, and unvalidated external performance.
