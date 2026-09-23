# 文档标准化与废弃审计

- 状态：Historical
- 文档类型：Review
- 维护者：documentation_maintainer
- 更新日期：2026-09-23
- 审计范围：`docs/` 全部 43 个 Markdown 文件，以及根级 `README.md`、`AGENTS.md`、`CHANGELOG.md` 中指向 `docs/` 的入口
- 执行方式：只读。审计阶段未修改、移动或删除任何既有文档；仅新增本报告与规范草案两个文件
- 备注：本报告记录的是**执行前**的状态，其中的路径为当时路径。标准化执行后的当前路径以 [文档索引](../README.md) 为准；执行结果见第 8 节

## 1. 结论摘要

1. `docs/` 共 43 个文件、11 个主题目录，**没有索引文件**，也**没有成文的文档规范**；治理规则目前分散在 `AGENTS.md` 第 9 节和 `.agents/documentation.md`。这是"标准化"最需要补的基础缺口。
2. 状态标注严重不统一：约一半文件（22 个）**完全没有状态声明**；有声明的文件混用了 4 种形式（`- 状态：` 列表项、`## 状态` 标题、纯文本行、`decision:` 字段），并且中英文枚举值混用（`已接受` 与 `Accepted`）。
3. `状态` 字段被同时用于两种语义：**文档生命周期**（Draft/Accepted/Superseded）和**实现进度**（"9 个运行时 Skill 已实现…"）。同一字段承载两种含义会让读者无法判断某句话是不是当前有效结论。
4. 存在 **1 个已正式被替代**的计划、**3 个已完成的阶段过程记录**、**1 个明确"部分被替代"的历史草案**，它们混在现行文档目录里，是废弃处理的主要对象。
5. 文档路径不只是人读的链接：`neuroagent/` 代码、9 个 `skills/*/skill.yaml` 以及 `scripts/repository-safety.ps1` 中**硬编码了 doc 路径与锚点**。归档动作必须同步改这些引用，否则会静默产生失效证据引用。

## 2. 现状盘点

### 2.1 文件分布

| 目录 | 文件数 | 说明 |
| --- | --- | --- |
| `docs/`（根） | 3 | Chat 模块分析与设计、Phase 1 实施报告，均未归类 |
| `docs/adr/` | 10 | 0001、0006–0014（0002–0005 号位空缺） |
| `docs/api/` | 1 | `api-v1.md` |
| `docs/architecture/` | 7 | 含 1 个历史草案 |
| `docs/development/` | 2 | 验证记录、发布流程 |
| `docs/domain/` | 3 | |
| `docs/operations/` | 3 | |
| `docs/plans/` | 9 | 0001–0008 + `conversation-model-understanding.md` |
| `docs/product/` | 1 | `mvp-scope.md` |
| `docs/reviews/` | 1 | 阶段终审 |
| `docs/security/` | 1 | |
| `docs/superpowers/` | 2 | 第三方工作流产出的重构计划与设计 |

### 2.2 状态标注现状

| 形式 | 数量 | 代表文件 |
| --- | --- | --- |
| `- 状态：…` 列表项 | 9 | 各 ADR、`plans/0001`、`plans/0004`～`0007` |
| `## 状态` 标题 | 5 | `adr/0008`、`adr/0011`、`adr/0012` 等 |
| 纯文本行（无列表标记） | 5 | `adr/0010`、`plans/0003`、`plans/0008`、`system-design.md`、`mvp-verification.md` |
| 完全无状态声明 | 22 | `api/api-v1.md`、`domain/*`、`operations/*`、`security/*`、`architecture/project-structure.md`、`adr/0013` 等 |

同一文件内也存在自相矛盾：`rsfmri-chat-module-design.md` 顶部横幅写"下文保留历史设计／验证记录"，但第 25 行的 `- 状态：Implementation-ready` 仍是 Phase 1 编码前的值。

### 2.3 指向 `docs/` 的引用点（归档必须同步修改）

| 引用位置 | 形式 | 受影响文档 |
| --- | --- | --- |
| `neuroagent/domain/fmri/skillpacks/builtin.py` 第 35、424 行 | `evidence_ref` 硬编码路径 + 锚点 | `architecture/fmri-skill-layer.md` |
| `neuroagent/skills/validation.py` 第 319 行 | `evidence_ref` 硬编码路径 + 锚点 | `architecture/fmri-skill-layer.md` |
| 9 个 `skills/*/skill.yaml` | `evidence_refs` 路径 | `domain/roi-ml-localization.md`、`operations/long-running-jobs.md`、`architecture/fmri-skill-layer.md`、`AGENTS.md#9` |
| `README.md` 文档入口表（第 77–92 行） | 相对链接 | 13 个文档 |
| `CHANGELOG.md` 第 7、11 行 | 相对链接 | `adr/0014`、`architecture/statistical-design-assembly.md` |
| `AGENTS.md` 第 109、137 行 | 相对链接 | `architecture/fmri-skill-layer.md`、`adr/0001`、`adr/0014` |
| 各模块 `README.md` | 相对链接 | `development/mvp-verification.md`、`architecture/project-structure.md`、`architecture/fmri-skill-layer.md`、`adr/0007` |
| `scripts/repository-safety.ps1` 第 13 行 | 允许路径白名单 `docs/examples/` | 指向一个**不存在**的目录 |

注意 `fmri-skill-layer.md#7-alfffalff-and-reho` 与 `#74-qc-gate` 是**标题锚点**（对应 `## 7. ALFF/fALFF 与 ReHo 的首期设计` 和 `### 7.4 QC gate`）。任何标题改写都会静默破坏这些引用，因此该文件的标题在本轮标准化中应视为**冻结**。

### 2.4 仓库工作区当前状态

本次审计通过 `git status --porcelain` 观察到工作区处于**大规模未提交状态**：约 70 个文件被修改、40 余个文件未跟踪，其中包含 `neuroagent/**`、`tests/**`、`web/**` 和全部 16 个 `skills/*/skill.yaml`。文档相关的未提交项为：

```text
 M README.md            M AGENTS.md          M CHANGELOG.md
 M docs/api/api-v1.md   M docs/architecture/system-design.md
 M docs/development/mvp-verification.md     M docs/operations/provider-setup.md
?? docs/adr/0014-bounded-tool-loop.md
?? docs/architecture/statistical-design-assembly.md
?? docs/plans/conversation-model-understanding.md
```

含义：

- 标准化必须在当前工作树之上叠加，**不得回退或覆盖这些改动**。
- 2.3 表中的引用点已在当前工作树重新核验（`skills/*/skill.yaml` 8 处、`neuroagent/**` 4 处），结论对该状态成立。
- 由于改动面巨大，建议文档标准化单独成一个变更，不与这些代码改动混在同一次提交里。

## 3. 问题清单

| 编号 | 问题 | 证据 | 影响 |
| --- | --- | --- | --- |
| P1 | 无文档索引，43 个文件的入口只有 `README.md` 里 13 条链接 | `docs/` 下无 README/index | 读者与 Agent 都无法判断某主题的权威文档是哪一个 |
| P2 | 状态枚举中英文混用 | `adr/0001` 用"已接受"，`adr/0006` 用"Accepted"；`plans/0001` 用"已实现"，`plans/0007` 用"Draft" | 无法机械校验，跨文档比较需要人读 |
| P3 | 22 个文件无状态声明 | 见 2.2 表 | 读者无法判断内容是否仍然有效 |
| P4 | `状态` 字段一字段两义 | `architecture/fmri-skill-layer.md` 状态行写的是实现进度；`plans/0002` 状态行混入"候选基线" | 文档生命周期与实现进度混淆 |
| P5 | 已废弃/已完成的文档混在现行目录 | `plans/0004` 自述 Superseded、`superpowers/*` 重构已完成、`rsfmri-chat-module-analysis.md` 自述"已被本更新替代" | 搜索与阅读时命中的是过时结论 |
| P6 | 3 个 Chat 文档散落在 `docs/` 根目录 | `docs/rsfmri-chat-*.md` | 无法按主题发现；与 `architecture/`、`development/` 的分类规则冲突 |
| P7 | 缺少模块 README | `neuroagent/` 下 `agent`、`api`、`application`、`chat`、`literature` 无 README，而其余 15 个模块有 | 不满足 `AGENTS.md` 第 9 节"各模块 README"的要求 |
| P8 | 文档内描述与代码现状不一致（示例） | `rsfmri-chat-module-analysis.md` 第 29–31 行称 `retrieval/`、`context/`、`memory/`"只有边界 README，未实现"，而 AGENTS.md 称 context/retrieval 已接入 | 历史文档内容被当作当前事实 |
| P9 | 失效引用 | `scripts/repository-safety.ps1` 白名单含不存在的 `docs/examples/` | 低风险，但说明引用没有被校验 |
| P10 | 无链接/状态校验机制 | 无任何脚本或测试检查 `.md` 链接与状态枚举 | 上述问题会持续复发 |

## 4. 权威文档表（建议的唯一事实来源）

| 主题 | 唯一权威文档 | 其他文档的角色 |
| --- | --- | --- |
| 开发代理规则与完成标准 | `AGENTS.md` | `.agents/*.md` 为角色契约 |
| 产品定位与范围 | `docs/product/mvp-scope.md` | 计划文档不作为范围来源 |
| 系统分层与运行边界 | `docs/architecture/system-design.md` | `mvp-1.0-target-architecture.md` 为未来目标 |
| Skill 层设计 | `docs/architecture/fmri-skill-layer.md` | `neuroagent-framework-architecture.md` 仅为历史 |
| API 契约 | `docs/api/api-v1.md` | OpenAPI 为机器来源 |
| DPABI 参数映射与执行边界 | `docs/domain/*` + `docs/operations/local-operations.md` | |
| 验证事实与已知限制 | `docs/development/mvp-verification.md` | 计划文档中的进度描述不得与之冲突 |
| 不可逆决策 | `docs/adr/` | 计划文档只引用 ADR，不重新决策 |
| 版本决策 | `docs/adr/0006` | `plans/0004` 已被其替代 |

## 5. 废弃处理方案

处理方式按已确认的方针：**加废弃横幅 + 移入 `docs/archive/` + 修正全部指向链接**，不删除内容以便追溯。

| 文件 | 建议动作 | 依据 | 归档时必须同步修改 |
| --- | --- | --- | --- |
| `docs/plans/0004-release-roadmap.md` | 归档 | 自述 `Superseded by ADR 0006`；ADR 0006 第 5 行明确 `Supersedes` | `plans/0004` 自身指向 0005 的链接；`adr/0006` 第 5 行路径 |
| `docs/superpowers/plans/2026-09-02-service-repository-refactor.md` | 归档 | 任务已落地：`SecretWriterPort` 存在于 `application/ports.py` 第 146 行、`bootstrap.py` 第 37 行，且有 `tests/backend/test_application_import_boundaries.py` 守护 | 无外部引用 |
| `docs/superpowers/specs/2026-09-02-service-repository-refactor-design.md` | 归档 | 同上；`tests/backend/test_repository_split.py` 的 AST 基线校验已存在 | 无外部引用 |
| `docs/rsfmri-chat-module-analysis.md` | 归档 | 第 4 行自述"下文为编码前的原始分析…已被本更新替代"；第 29–31 行对 `retrieval/context/memory` 的描述与现状不符 | 无外部引用 |
| `docs/architecture/neuroagent-framework-architecture.md` | 归档 | 第 3–5 行自述"历史设计草案（部分被替代）"，第 228、553 行有两处禁止依据本文实现的声明 | 其自身指向 `fmri-skill-layer.md`、`adr/0001`、`neuroagent/README.md` 的相对链接需在归档后校验 |

**保留但必须改写状态横幅**（不归档，因为仍是现行事实来源）：

| 文件 | 现在 | 建议 |
| --- | --- | --- |
| `docs/rsfmri-chat-module-design.md` | 横幅称"历史记录"，正文第 25 行仍写 `Implementation-ready` | 统一为 `状态：Implemented` + 历史范围说明，并保留"当前接口以 API 文档为准" |
| `docs/rsfmri-chat-phase1-implementation-report.md` | 同样横幅/正文不一致 | 统一为 `状态：Implemented` |
| `docs/plans/0002-full-mvp-roadmap.md` | "候选基线；…尚未完成"，环境写 MATLAB R2023b/DPABI V8.2 | 明确降级为 v0.1.0 历史路线（当前路线以 0006/0007 为准），并移除与环境现状冲突的版本表述 |
| `docs/plans/0001`、`0003`、`0008` | 已完成/实现中混用 | 按新枚举改为 `Implemented`／`Implementing` |
| `docs/architecture/system-design.md`、`docs/development/mvp-verification.md`、`docs/product/mvp-scope.md` | 纯文本状态行 | 改为统一字段形式 |

**需要合并的重叠**：

| 文档 A | 文档 B | 重叠主题 | 建议 |
| --- | --- | --- | --- |
| `plans/0006-release-blockers.md` | `development/mvp-verification.md` | 真实 smoke 的环境、结果与范围限制 | 验证事实只留在 `mvp-verification.md`，0006 改为引用 |
| `plans/0007-capability-modularization-roadmap.md` | `architecture/target-architecture.md` | 目标阶段的范围与边界 | 架构留在架构文档，路线留在计划文档，互相引用 |
| `rsfmri-chat-module-design.md` | `rsfmri-chat-phase1-implementation-report.md` | 两文件顶部有几乎相同的更新横幅 | 横幅只保留在报告，设计文档改为指向报告 |

## 6. 已确认的决策与执行结果

审计结论经用户确认后执行，采用以下口径：

| 项 | 决定 | 执行结果 |
| --- | --- | --- |
| 范围 | `docs/` 全部文件 + 根级 `README.md`/`AGENTS.md`/`CHANGELOG.md` + 模块 `README.md` | 已执行 |
| 废弃方式 | 加横幅 + 移入 `docs/archive/`（保留原子目录结构）+ 修全部入链，不删除内容 | 5 个文档归档，`docs/superpowers/` 目录移除 |
| 版本包装 | 全部清掉，连未来 `v1.0.0` 目标路线也不提版本号 | 见第 7 节 |
| ADR 0006 | 标题与内容保持原样，不改历史记录 | 仅补 `决策日期` 与 `替代关系` 字段 |
| 归档目录 | `docs/archive/<原目录>/` | 已建立 |
| `fmri-skill-layer.md` | 标题层级冻结，只在文首补状态块 | 已执行，未改任何标题 |
| 规范与索引 | 新增 `docs/development/documentation-standard.md`（Accepted）与 `docs/README.md` | 已建立 |
| 校验 | 新增 `scripts/check-docs.ps1` 并接入 `quality-gate.ps1` | 已建立 |
| 提交范围 | 文档标准化单独成一个提交，不混入工作区既有代码改动 | 未提交（按约定不自行提交） |

## 7. 去版本发布包装

从现行文档中移除 `v0.1.0`／`v0.2.0`／`v1.0.0`／`MVP1.0` 发布框架，改为按能力状态与阶段描述。两个文件因此重命名：

| 原路径 | 新路径 |
| --- | --- |
| `docs/plans/0007-mvp-1.0-roadmap.md` | `docs/plans/0007-capability-modularization-roadmap.md` |
| `docs/architecture/mvp-1.0-target-architecture.md` | `docs/architecture/target-architecture.md` |

同时更新：`README.md`、`AGENTS.md` 第 2 节、`CHANGELOG.md`、`docs/product/mvp-scope.md`、`docs/development/mvp-verification.md`、`docs/architecture/system-design.md`、`docs/plans/0006`、`docs/plans/0005`、`docs/adr/0009`、`docs/development/release-process.md`、`neuroagent/README.md`、`neuroagent/analysis/README.md`、`neuroagent/domain/fmri/skillpacks/README.md`、两个 `skills/*/SKILL.md`。

**保留的例外**（按已确认决定）：

- `docs/adr/0006-v0.1-real-execution.md` 的标题与正文：ADR 是历史决策记录，不改写。
- `docs/archive/plans/0004-release-roadmap.md`：归档历史文档。
- `CHANGELOG.md` 的历史条目：变更记录不可改写。
- `neuroagent/api/app.py` 的 `version="0.1.0"` 与 `service_mixins/runs.py` 的 `statistics_test_not_in_mvp`：机器可读 API 版本与错误码，改名会破坏契约。

## 8. 执行后的校验结果

见 `scripts/check-docs.ps1` 的输出与本报告末尾的验证记录。
