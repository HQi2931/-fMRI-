# 文档索引

- 状态：Accepted
- 文档类型：Development
- 维护者：documentation_maintainer
- 创建日期：2026-09-23
- 更新日期：2026-09-23
- 适用范围：`docs/` 全部文档的导航、权威来源与归档登记
- 关联：[文档规范](development/documentation-standard.md)

本文件是 `docs/` 的唯一索引。新增、移动或归档文档时必须在同一变更内更新本文件；由 `scripts/check-docs.ps1` 校验覆盖完整性。

## 权威文档表

- [ADR 0018：Work 选项式确认弹窗](adr/0018-work-confirmation-dialog.md)

每个主题只有一个事实来源。其他文档只能引用，不得复述结论。

| 主题 | 权威文档 | 其他文档的角色 |
| --- | --- | --- |
| 开发代理规则与完成标准 | [`AGENTS.md`](../AGENTS.md) | [`.agents/`](../.agents/README.md) 为角色契约 |
| 产品定位与范围 | [MVP 产品范围](product/mvp-scope.md) | 计划文档不作为范围来源 |
| 系统分层与运行边界 | [系统设计](architecture/system-design.md) | [目标架构](architecture/target-architecture.md) 为演进方向 |
| Skill 层设计 | [静息态 fMRI Skill 层架构](architecture/fmri-skill-layer.md) | 归档中的通用框架草案仅为历史 |
| 项目结构 | [项目结构](architecture/project-structure.md) | |
| 统计设计装配 | [统计设计的取值来源与装配](architecture/statistical-design-assembly.md) | |
| API 契约 | [API v1 契约](api/api-v1.md) | `web/openapi.json` 为机器来源 |
| 数据集输入契约 | [数据集输入与只读契约](domain/dataset-contract.md) | |
| 统计方法学 | [组统计设计](domain/statistics.md) | |
| ROI、ML 与定位契约 | [ROI、机器学习与脑区定位领域合同](domain/roi-ml-localization.md) | |
| MATLAB/DPABI 运行与恢复 | [本地运行、诊断与恢复](operations/local-operations.md) | |
| 长任务与错误诊断 | [长任务等待、恢复与错误诊断](operations/long-running-jobs.md) | |
| Provider 配置 | [Provider 配置与 smoke](operations/provider-setup.md) | |
| 数据外发策略 | [模型数据外发策略](security/data-egress.md) | |
| 验证事实与已知限制 | [MVP 验证与已知限制](development/mvp-verification.md) | 计划文档中的进度描述不得与之冲突 |
| 开发与阶段流程 | [阶段审查与阶段关门](development/release-process.md) | |
| 文档规范 | [文档规范](development/documentation-standard.md) | |
| 不可逆决策 | [`docs/adr/`](adr/) | 计划文档只引用 ADR，不重新决策 |

## 目录导航

### 产品

- [MVP 产品范围](product/mvp-scope.md)

### 架构

- [系统设计](architecture/system-design.md)
- [目标架构](architecture/target-architecture.md)
- [静息态 fMRI Skill 层架构](architecture/fmri-skill-layer.md)
- [扩展分析服务设计](architecture/analysis-services.md)
- [统计设计的取值来源与装配](architecture/statistical-design-assembly.md)
- [项目结构](architecture/project-structure.md)

### 领域

- [数据集输入与只读契约](domain/dataset-contract.md)
- [组统计设计](domain/statistics.md)
- [ROI、机器学习与脑区定位领域合同](domain/roi-ml-localization.md)

### 接口

- [API v1 契约](api/api-v1.md)

### 运维

- [本地运行、诊断与恢复](operations/local-operations.md)
- [长任务等待、恢复与错误诊断](operations/long-running-jobs.md)
- [Provider 配置与 smoke](operations/provider-setup.md)

### 安全

- [模型数据外发策略](security/data-egress.md)

### 开发

- [MVP 验证与已知限制](development/mvp-verification.md)
- [阶段审查与阶段关门](development/release-process.md)
- [文档规范](development/documentation-standard.md)
- [rs-fMRI Chat Mode Phase 1 设计](development/rsfmri-chat-module-design.md)
- [rs-fMRI Chat Mode Phase 1 实施报告](development/rsfmri-chat-phase1-implementation-report.md)

### 计划

- [计划 0001：fMRI Skill 层 MVP](plans/0001-skill-layer-mvp.md)
- [计划 0003：扩展分析与长任务闭环](plans/0003-extensions.md)
- [计划 0005：用户选择本机科学软件环境](plans/0005-user-selected-local-environment.md)
- [计划 0006：真实执行阻断收口](plans/0006-release-blockers.md)
- [计划 0007：能力化模块单体演进路线](plans/0007-capability-modularization-roadmap.md)
- [计划 0008：语义长期记忆](plans/0008-semantic-long-term-memory.md)
- [计划 0009：Agent 控制面与执行授权加固](plans/0009-agent-control-plane-hardening.md)
- [计划 0010：Chat 与 Work 对话流式响应](plans/0010-conversation-streaming.md)
- [Chat / Work 模型理解与参数补全](plans/conversation-model-understanding.md)

### 决策记录（ADR）

- [ADR 0001：运行时 Skill 编译为受控 Workflow](adr/0001-skill-compiles-to-workflow.md)
- [ADR 0006：真实执行范围](adr/0006-v0.1-real-execution.md)
- [ADR 0007：按冻结设计矩阵记录调整后统计效应](adr/0007-adjusted-statistical-effects.md)
- [ADR 0008：持久化对话与显式 DPABI 原位工作区](adr/0008-persistent-conversations-and-in-place-dpabi-workspaces.md)
- [ADR 0009：按能力演进模块化单体](adr/0009-capability-oriented-modular-monolith.md)
- [ADR 0010：持久化上下文工程](adr/0010-persistent-context-engineering.md)
- [ADR 0011：Work 对话作为唯一业务入口（已被 ADR 0012 取代）](adr/0011-work-conversation-cards.md)
- [ADR 0012：前端只保留 Chat 与 Work 对话](adr/0012-chat-work-only.md)
- [ADR 0013：QC 由运行请求显式选择](adr/0013-optional-qc-gate.md)
- [ADR 0014：有界工具调用循环与三级工具契约](adr/0014-bounded-tool-loop.md)
- [ADR 0015：Agent 控制面预算、作用域与一次性执行授权](adr/0015-agent-control-plane-hardening.md)
- [ADR 0017：DPABI 阶段由模型建议、用户确认与服务端冻结](adr/0017-explicit-dpabi-stage-selection.md)
- [ADR 0018：工作区文件授权验证具体模型工具能力](adr/0018-workspace-tool-probe-and-consent-revision.md)
- [ADR 0019：对话正文流式预览与事务收口](adr/0019-conversation-streaming.md)
- [ADR 0020：DPARSF 参数面板与 T1 准备审查](adr/0020-dparsf-draft-and-t1-review.md)
- [DPARSF 面板与 T1 实施计划](plans/0011-dparsf-panel-t1.md)

### 审查记录

- [Phase 19 终审](reviews/phase-19-release-blockers.md)
- [文档标准化与废弃审计](reviews/2026-09-23-docs-standardization-audit.md)

## 归档清单

归档文档仅作历史参考，**不得作为当前实现依据**。除修正链接外不再编辑原文。

| 归档文档 | 原位置 | 归档原因 | 替代文档 |
| --- | --- | --- | --- |
| [计划 0002：rs-fMRI Agent 全量 MVP](archive/plans/0002-full-mvp-roadmap.md) | `docs/plans/` | 阶段编号进度已成历史，环境描述与当前本机事实冲突 | [计划 0006](plans/0006-release-blockers.md)、[验证记录](development/mvp-verification.md) |
| [计划 0004：统一发布路线](archive/plans/0004-release-roadmap.md) | `docs/plans/` | 被 ADR 0006 取代 | [ADR 0006](adr/0006-v0.1-real-execution.md) |
| [NeuroAgent Framework Architecture](archive/architecture/neuroagent-framework-architecture.md) | `docs/architecture/` | 早期通用框架草案，部分被替代 | [Skill 层架构](architecture/fmri-skill-layer.md) |
| [rs-fMRI Chat Mode 模块分析](archive/development/rsfmri-chat-module-analysis.md) | `docs/` | 编码前的原始分析，范围已被后续更新替代 | [Phase 1 设计](development/rsfmri-chat-module-design.md) |
| [Service and Repository Refactor Implementation Plan](archive/development/2026-09-02-service-repository-refactor.md) | `docs/superpowers/plans/` | 重构已完成并通过导入边界测试守护 | [项目结构](architecture/project-structure.md) |
| [服务层与仓储层分层解耦设计](archive/development/2026-09-02-service-repository-refactor-design.md) | `docs/superpowers/specs/` | 同上 | [项目结构](architecture/project-structure.md) |

## 规范入口

- [文档规范](development/documentation-standard.md)：状态块、状态枚举、命名、权威来源、归档规则与校验要求。
- 校验脚本：`scripts/check-docs.ps1`（纳入 `scripts/quality-gate.ps1`）。
