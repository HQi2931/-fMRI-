---
name: design-tabular-ml
description: Produce an approval-gated classical tabular ML design with subject-level grouping, fixed seeds, preprocessing, metrics, and leakage controls.
---

# Design tabular ML

1. Require explicit target, feature, subject/group, missing-value, class encoding, and evaluation choices.
2. Prefer a Pipeline and ColumnTransformer so preprocessing stays inside each fold.
3. Use grouped stratified cross-validation or a clearly frozen train/validation/test split.
4. Return a versioned design and do not run model selection or threshold fishing automatically.
