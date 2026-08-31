---
name: run-tabular-ml
description: Render an approved classical Python table-model template in an isolated workspace; real execution is a later, separately authorized step.
---

# Run tabular ML

> 状态：契约/预览包，未注册运行时；本 MVP 只渲染固定模板，真实执行与产物登记待 v0.2.0 单独接线与批准。

1. Accept only an approved immutable ML design and registered table Artifact IDs.
2. Render the fixed Python template; never accept free Python or shell text from a model.
3. Render with a fixed seed and subject-level split checks; running the template is a later, separately authorized step.
4. Metrics, ROC/PR/calibration figures, feature importance, logs, and environment provenance are registered only after that authorized execution.
5. Do not retry with alternate models or thresholds to improve significance.
