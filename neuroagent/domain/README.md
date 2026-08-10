# domain

## 模块职责

`domain` 保存具体科研领域的模型、约束和内置能力。通用 Agent、Skill、Workflow、Tool 与 Execution 机制保留在各自顶层模块，领域包通过明确接口贡献 Artifact 类型、校验规则、SkillSpec、工作流模板和工具适配。

## 当前领域

- [fmri](fmri/README.md)：静息态 fMRI 数据、预处理、指标、QC 和统计分析。

## 边界

- 领域包不依赖 FastAPI、数据库适配器或 subprocess。
- 领域包不直接执行 MATLAB；执行通过 Tool 和 Execution。
- 科学参数必须有 schema、来源和审核状态。
- 原始数据访问默认只读，派生产物写入隔离运行目录。
