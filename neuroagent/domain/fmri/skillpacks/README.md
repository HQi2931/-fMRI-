# fMRI Skill Packs

本目录保存项目内置、版本化的 fMRI `SkillSpec`。`builtin.py` 是当前运行时 Registry 的声明式来源，只定义 Artifact 合同、参数、步骤偏序、能力和 QC gate，不执行 Tool，也不保存运行状态。

## 当前运行时内置 Skill

| Skill ID | 形式 | 目的 |
| --- | --- | --- |
| `rsfmri.dataset.inspect` | 独立可审查包 | 只读检查数据集并生成显式 manifest 合同 |
| `rsfmri.preprocess.common` | `preprocessing_protocol` | 数据合同、公共基础预处理和共享 QC |
| `rsfmri.metric.alff_falff` | `metric` | 从合格输入同时生成 ALFF/fALFF 产物并声明主终点 |
| `rsfmri.metric.reho` | `metric` | 在显式空间、滤波和邻域协议下计算 ReHo |
| `rsfmri.pipeline.alff_reho_combined` | 运行时组合 Skill | 复用公共节点，让 ALFF/fALFF 与 ReHo 消费各自的类型化检查点 |
| `rsfmri.qc.pre_statistics` | 独立可审查包 | 冻结受试者、指标谱系、共同掩膜和统计前门禁 |
| `rsfmri.statistics.ttest` | 独立可审查包 | 声明 t 检验、协变量与未校正图合同 |
| `rsfmri.statistics.fdr` | 运行时组合 Skill | 声明 FDR 校正能力和产物合同 |
| `rsfmri.statistics.grf` | 运行时组合 Skill | 声明 GRF 校正能力和产物合同 |

其中六个主要协议在仓库根目录 `skills/` 下有独立的人机可读包；combined、FDR 和 GRF 由 `builtin.py` 组合或拆分为运行时 Skill，并复用相应指标/统计参数合同。

## 已提交 Skill 包结构

```text
skills/<skill_name>/
├── SKILL.md
├── skill.yaml
├── parameters.schema.json
└── agents/
    └── openai.yaml
```

- `skill.yaml` 是机器事实来源，不包含自由脚本。
- `SKILL.md` 面向 Codex 和开发者说明适用范围、流程、限制和确认项。
- `parameters.schema.json` 约束类型、单位、范围和参数来源。
- `agents/openai.yaml` 提供 Skill 的界面元数据和默认调用提示。

六个已提交包是：`inspect-rsfmri-dataset`、`plan-dpabi-preprocessing`、`plan-alff-falff`、`plan-reho`、`review-rsfmri-qc` 和 `plan-rsfmri-statistics`。`tests/science/test_skill_packages.py` 校验这些包完整、schema 合法，并验证磁盘 `skill.yaml` 与对应内置声明一致。

## 测试位置与执行边界

本项目不在每个 Skill 包内复制 `tests/cases.yaml`。Skill 解析、编译、科学负向用例、DPABI 映射、统计设计和执行边界测试集中位于 `tests/science/`，主要由 `test_skill_packages.py`、`test_skill_compiler.py`、`test_skill_engine_edges.py`、`test_metric_rules.py` 和 `test_statistics.py` 覆盖。

`reviewed` 表示声明、schema、证据和测试达到当前候选基线要求，不表示公共 Worker 已获准执行真实数据。当前公共运行路径仍为 Mock-only；真实 DPABI/统计执行必须完成接线、单独授权和 smoke 验证。
