# 计划 0004：统一发布路线（Superseded）

- 状态：Superseded by ADR 0006
- 更新日期：2026-08-31
- 前置决策：原“真实科学执行推迟到 v0.2.0”的决定已由 [ADR 0006](../adr/0006-v0.1-real-execution.md) 替代。

> 本文保留为历史路线记录；当前 v0.1.0 范围以 ADR 0006 和最新发布计划为准。

本机科学软件环境的当前实施计划见 [计划 0005：用户选择本机科学软件环境](0005-user-selected-local-environment.md)。

## 背景

当前 `main` 已含 Phase 10–17 扩展（`neuroagent/analysis/` 确定性预览、诊断、模板与 API 合同、10 个新 Skill、统计结果登记/只读查询闭环），但整体仍处于「v0.1.0 候选基线未发布」状态：公共 Worker 仍是 Mock-only，真实 MATLAB/DPABI 与真实统计执行未接线，首次 push/CI/终审未完成。

本计划把从当前状态到「可发布平台」的剩余工作合并为一条统一路线：先收口 v0.1.0，再按风险序推进 v0.2.0 真实执行轨。每段独立可交付、独立可验证。

## 发布策略

- **v0.1.0** = 可审核 Mock 平台 + 确定性报告合同 + 合成结果；**不含真实科学执行**。
- **v0.2.0** = 真实执行轨：DAG→Tool 运行时接线 → 受控 MATLAB 执行 → 真实统计 → Phase 10–17 真实分析。

## 阶段一：v0.1.0 收口

目标：把当前 `main` 发布为 v0.1.0，边界明确为「Mock 平台 + 确定性报告 + 合成结果」。

| # | 步骤 | 说明 | 依赖 |
| --- | --- | --- | --- |
| 1 | 质量门禁跑绿 | `scripts/quality-gate.ps1`（Python strict mypy + ruff + pytest ≥85% + 依赖审计；Web ESLint/TS/Vitest ≥80% + build + Playwright Mock E2E；科研契约/执行/恢复/安全门禁）。刚合并的 Phase 10–17 代码（analysis 模块、新 Skill、migration 0005、AnalysisPage、新测试）必须在门禁内，不绿先修。 | — |
| 2 | 多角色终审 | `.agents/` 八角色对最终树出 `decision: pass`、无 P0–P2，报告绑定最终暂存树哈希。 | 1 |
| 3 | 首次 push + CI + 分支保护 | `gh auth login` 后 push `main`、建保护规则、验证 Actions 跑 `quality-gate`。 | 1、2 |
| 4 | Provider smoke（可选） | 本地 `.env` key 做一次轻量真实 Provider 调用，验证外发脱敏、key 不入库/日志。属 Agent 功能非科学执行；如不想产生费用可降级为「记录未执行」。 | 1 |
| 5 | 文档收口 | README/CHANGELOG/mvp-scope/system-design 正式移除「真实执行」承诺并标记 v0.2.0；`0002` 阻断项 #6 落成「已决策：推迟」；补 `neuroagent/analysis/README.md`。 | 1 |
| 6 | 打 tag `v0.1.0` | | 1–5 |

验收 = v0.1.0 发布条件全满足（门禁绿 + 终审 pass + push/CI 成功 + Provider smoke 或记录未执行 + 范围收口文档）。

## 阶段二：v0.2.0 真实执行轨

按风险/依赖序，每段独立可交付、可验证，沿用仓库完成定义（代码+测试+文档+CHANGELOG 同步、多角色审查、门禁绿）。

| 段 | 内容 | 现状 → 目标 | 依赖 | 主要角色 |
| --- | --- | --- | --- | --- |
| 2a | `SkillPlan` DAG → 类型化 Tool 运行时接线 + Artifact 收口 | 编译器已产出不可变 DAG+Tool 锁，但 Worker 只调 MockExecutor，不遍历 DAG/不调 Tool | 无（地基） | `skill_workflow_engineer` + `backend_service_engineer` + `system_architect` |
| 2b | `ControlledMatlabExecutor` 接入 Worker + 真实预处理 smoke | 执行器/模板/DPABI V8.2 投影已实现但未接 Worker、未 smoke | 2a | `matlab_dpabi_engineer` + `fmri_methodologist` |
| 2c | 真实统计执行 + 结果登记/查询闭环 | 统计只建 `statistics_mock`；登记/查询 API 已接但只收合成结果 | 2b | `fmri_methodologist` + `backend_service_engineer` + `qa_reviewer` |
| 2d | Phase 10–17 真实分析（ROI / ML / cluster / RAG） | 确定性预览/模板/合同已交付，真实执行未接 | 2b | 按子项分：ROI/cluster→`matlab_dpabi`+`fmri_methodologist`；ML→`skill_workflow`+`backend_service`；RAG→`backend_service`+`system_architect`+`fmri_methodologist`；前端→`frontend_ux_engineer` |

### 2d 子项边界

- 真实 `y_ExtractROISignal`、人口学 XLSX 模板服务、DPABI 整理复制执行。
- 受批准隔离 Runner 上的 subject 级 ML 与科研图生成。
- cluster 表 NIfTI 网格采样、空间坐标系校验、完整报告 Artifact。
- 本地 rs-fMRI 证据检索 + 范围拒答 + 来源片段；**脱敏联网检索与引用缓存是独立授权项**。

## 横切约束（授权门）

以下不自动执行，均需用户单独批准，角色委派不扩大权限：

- 真实 MATLAB/DPABI 长作业（含小数据 smoke）。
- 真实 Provider 调用（可能产生费用）。
- 原始数据写入、受试者排除、统计方法/阈值变更。
- 2d 的脱敏联网检索。

## 角色委派映射

- v0.1.0 步骤 2（多角色终审）：全部八角色并行只读审核（首个并行子 Agent 派发）。
- v0.1.0 步骤 5（文档收口）：`documentation_maintainer`。
- 阶段二各段：见上表「主要角色」列。
- 发布前检查：`qa_reviewer` + `documentation_maintainer`。

## 完成定义与验收

沿用 `AGENTS.md` 完成标准：需求与范围明确、分层边界与原始数据只读、关键失败路径有测试、状态/错误/产物可追踪、文档与 CHANGELOG 同步、无敏感数据或大产物入 Git、对未真实验证行为作出明确说明。

## 未解决 / 需用户授权项

1. `gh auth login` 与首次 push（需仓库维护者执行）。
2. 真实 Provider smoke（需本机有效 key，可能产生费用）。
3. 真实 MATLAB/DPABI smoke（需单独授权小型合成/脱敏作业）。
4. 2d 脱敏联网检索（需单独授权）。
