# Skills 层

`neuroagent.skills` 是科研能力的声明、解析、校验和编译层。它将 `SkillRequest` 编译为可预览、可审批、不可变的 `SkillPlan`，但不执行 Tool 或 MATLAB。

处理链如下：

```text
SkillRequest
→ SkillRegistry
→ SkillResolver
→ SkillValidator
→ SkillCompiler
→ immutable SkillPlan
→ Approval
→ Workflow / Tool / Executor
```

核心约束：

- Skill 不携带任意 Shell 或 MATLAB 文本，也不维护运行状态。
- 通用预处理通过 `request_preprocessing`、完整的 `PreprocessingParameters` 和已注册的 `base_cfg_artifact_id` 显式请求。
- 只请求通用预处理时允许指标集合为空；指标请求也可消费已经存在的合格检查点。
- Compiler 按类型化 Artifact 合同连接通用预处理、执行端头信息验证和 ALFF/fALFF、ReHo 分支；指标只消费带实际 TR、保留 volume 数和证据哈希的 verified checkpoint。
- 同一 DAG 仅在有效 volume 数可在运行时确定并核验时成立；CUT scrubbing 后的 ReHo 必须先产生 verified Artifact，再创建第二阶段指标计划。
- ALFF、fALFF 与 ReHo 的所有 scaling 均强制绑定同一已登记 typed 脑掩膜，不提供无 mask 分支。
- 计划哈希绑定冻结输入、参数、基础 Cfg Artifact、Skill、Tool 和环境锁；任何变化必须生成新的 revision 并重新审批。
- 阻断性科学校验不能被 Agent 或 API 绕过。

项目根目录的 `skills/*` 同时保存供开发者阅读的 `SKILL.md` 和机器可读 manifest/schema。测试要求磁盘 Skill 与内置 `SkillSpec` 完全一致，并验证 schema、DAG 与计划哈希稳定性。

完整设计见 [fMRI Skill 层架构](../../docs/architecture/fmri-skill-layer.md)。
