# 目标架构

- 状态：Draft
- 文档类型：Architecture
- 维护者：system_architect
- 日期：2026-09-07
- 适用范围：当前架构之后按能力切分的渐进式演进
- 相关决策：[ADR 0009](../adr/0009-capability-oriented-modular-monolith.md)

## 结论

目标架构继续采用 Windows 本地优先的模块化单体、SQLite 和独立 Worker，不拆微服务，也不引入 Redis、消息队列或对象存储。优化重点从“继续添加能力”转为“稳定模块边界、完成真实科研主路径、降低前后端状态复杂度、形成可恢复和可交付的本地产品”。

目标架构按业务能力切分应用服务、端口、API 路由和前端功能，但保留纯领域规则、Skill 编译、受控执行和审批门禁。迁移期间公共 REST/SSE 路径、数据库内容和已批准计划哈希保持兼容。

## 当前架构评估

### 已经做对并应保留的部分

- Agent、Skill、Workflow、Tool 和 Executor 的权限边界明确，模型不能直接执行任意命令。
- 计划、审批、运行、QC 和统计结果均有不可变内容或追加式证据，适合科研审计。
- SQLite 原子领取、幂等租约、独立 attempt、输入哈希和产物完整性检查已经覆盖主要失败路径。
- 领域层包含较完整的预处理、指标、QC 和统计规则，真实 MATLAB 路径由固定模板和结构化 JobSpec 约束。
- 单机、单用户、本地数据的产品定位清楚，没有为尚不存在的分布式需求过度设计。

### 目标架构前需要消除的结构压力

| 现象 | 当前证据 | 影响 |
| --- | --- | --- |
| 应用能力协作仍共享组合根依赖 | `ApplicationServices` 显式持有能力对象；能力对象不再持有组合根，动态兼容转发已删除；Conversation、Skill、Run 与 Statistics 通过能力端口和显式查询协作者工作；Worker host 通过独立窄端口装配 | 继续消除少量组合级查询回调，并让项目级前端状态由 feature 查询 hooks 管理 |
| 仓储仍保留迁移兼容聚合 | 所有持久化能力及 Worker host 都有共享 Unit of Work 的独立 SQLite adapter；应用幂等存储和事件存储独立注入；默认应用用例和 Worker 不依赖 `SqliteRepository`，其仅保留给迁移期调用方。Worker adapter 在单一完成事务中组合 Artifact/QC 持久化 mixin | 收窄兼容聚合的剩余调用方，并为各 JobKind 补齐独立版本化的 command/执行合同 |
| API 路由集中 | 96 个端点已按能力迁入 `api/routers/`，`api/routes.py` 仅聚合并兼容导出；少数共享子服务仍经组合根读取 | 需继续让所有 handler 仅依赖能力接口，并为各子路由建立局部边界验证 |
| 控制面类型位置倒置 | Plan/Workflow 状态已迁入 `domain.lifecycle`，Workflow engine/worker 已去掉 Application DTO 依赖；Execution 编译器/预处理通过执行层 manifest 协议和领域数据集分类协作 | 已消除 Execution 对 Application DTO 的直接反向依赖；后续继续审计 Domain/Execution 中的兼容导入 |
| Worker 路由使用字符串协议 | 新队列记录使用 `JobEnvelope v1` 和受限 `JobKind`；旧 `executor_type` 通过持久化边界适配器读取，组合根按类型注册执行器 | 仍需把每个 kind 的 command、能力、超时、取消和产物合同独立版本化 |
| 前端页面承担过多状态 | Agent 与 Settings 已归入 `features/`，生产页面通过能力 API 模块和独立类型模块依赖后端；Agent 页面仍包含发送流程、恢复、授权与轮询，项目级 feature 目录和服务端查询 hooks 尚未统一 | 表单联动、错误恢复、测试和跨页一致性仍需按能力提取 |
| 浏览器缓存仍持有运行态指针 | `workspace.ts` 只持久化工作区路径和项目选择；计划、运行、QC 与统计指针只在内存中保留，并由服务端会话重新水合 | 运行期仍存在一份方便页面联动的内存投影，需要逐步让 feature hooks 直接读取服务端数据 |
| 测试集中且接近门槛 | Python 总覆盖率约 85.62%；前端分支覆盖率 78.95% | 大规模移动容易制造回归，新增边界需先补契约测试 |

以上行数和门禁数据仅用于确定重构优先级，不作为机械拆文件指标。模块是否合理以职责、依赖和事务边界为准。

## 架构原则

1. **科研合同优先于代码布局**：计划哈希、审批、manifest、Artifact lineage 和结果报告语义在重构期间不变。
2. **按能力分片、按层约束依赖**：同一能力的用例、端口、DTO 和路由相邻；纯领域规则仍不依赖 FastAPI、SQLAlchemy、Provider 或 subprocess。
3. **SQLite 是元数据唯一真源**：浏览器只保存选择器和未决请求标识，不保存可替代服务端的计划/运行状态。
4. **控制面与运行面分离**：API 只提交已批准命令；Worker 只消费版本化、类型化、服务端生成的 JobEnvelope。
5. **兼容式迁移**：先增加新边界和适配层，再迁移调用点，最后删除兼容导出；不做一次性目录大搬家。
6. **失败关闭**：任何 schema 版本、输入哈希、工具锁、环境锁、产物角色或执行证据不匹配都不得降级为成功。

## 目标模块

```text
React feature modules
        |
        v
FastAPI routers by capability
        |
        v
Explicit application services
        |
        +--------------------+
        |                    |
        v                    v
Pure domain rules      Capability ports
                             |
                             v
                   SQLite / filesystem / LLM / MATLAB adapters
                             |
                             v
                    Typed job queue + Worker
```

后端能力边界如下：

| 能力 | 负责内容 | 不负责 |
| --- | --- | --- |
| `projects` | 项目、数据集、manifest、人口学、受试者清单和工作区检查 | 计划审批、执行和统计 |
| `protocols` | Skill 解析、科学校验、计划 revision、审批和环境绑定 | 运行状态和 MATLAB 进程 |
| `execution` | Run、Job、attempt、事件、取消、重试、Artifact 注册 | 科学参数选择和 QC 决策 |
| `qc` | QC revision、纳入/排除记录、审批和 QC 报告 | 修改原始数据或隐式排除 |
| `statistics` | 设计、校正、执行命令、结果证据和复现报告 | 自动选择显著方法 |
| `agent` | Profile、对话、RAG、脱敏、结构化建议和工具调用审计 | 直接推进计划或运行状态 |
| `environment` | MATLAB/SPM/DPABI 配置、探测、能力指纹和本机密钥引用 | 科学结果解释 |

跨能力协作只通过显式用例或窄端口完成。例如统计服务读取已批准 QC 和 Artifact 摘要，不直接访问 QC 的 SQLAlchemy Row；对话服务调用 `WorkspaceQuery` 或 `RunQuery`，不依赖另一个 service mixin 的隐式方法。

## 后端结构

目标目录采用“分层不变、层内按能力切分”的渐进形式：

```text
neuroagent/
├─ api/
│  ├─ routers/
│  │  ├─ projects.py
│  │  ├─ protocols.py
│  │  ├─ runs.py
│  │  ├─ qc.py
│  │  ├─ statistics.py
│  │  ├─ agent.py
│  │  └─ environment.py
│  └─ schemas/                 HTTP 专用请求、响应与错误模型
├─ application/
│  ├─ common/                  幂等、事务协调、时钟、ID 和公共错误
│  ├─ projects/
│  ├─ protocols/
│  ├─ execution/
│  ├─ qc/
│  ├─ statistics/
│  ├─ agent/
│  └─ environment/
├─ domain/
│  ├─ shared/                  强类型 ID、hash、不可变引用、状态值对象
│  └─ fmri/                    科学模型和规则
├─ skills/                     Skill 规范、加载、校验和编译
├─ workflow/                   纯状态机、JobEnvelope 和 Worker 协议
├─ execution/                  JobSpec 编译和执行端口模型
├─ infrastructure/
│  ├─ persistence/sqlite/      分能力 repository 实现与 UnitOfWork
│  ├─ filesystem/
│  ├─ matlab/
│  └─ providers/
└─ bootstrap.py                唯一组合根
```

这不是要求立即移动所有现有文件。第一阶段允许旧模块重导出新位置的类型，待所有调用点和 OpenAPI 快照迁移后再删除兼容层。

### 显式应用服务

`ApplicationServices` 已移除多重继承和旧方法动态转发，并显式组合能力对象。当前形态为：

```text
ApplicationServices
├─ projects: ProjectService
├─ protocols: ProtocolService
├─ runs: RunService
├─ qc: QcService
├─ statistics: StatisticsService
├─ agent: AgentService
└─ environment: EnvironmentService
```

拆出的 router 通过 `capability_from()` 获取自身能力对象；跨能力调用使用显式绑定的 Query/Command 协作者。`ApplicationServices` 只作为组合容器，不包含业务方法，也不模拟一个全能对象。剩余耦合集中在聚合仓储和少量组合级服务回调。

### 端口和事务

仓储能力协议已落在 `application/repository_ports.py`，各用例使用对应数据端口；事务/幂等通过独立 `IdempotencyStorePort` 注入，审计事件通过独立 `EventRepositoryPort` 注入。每项持久化能力与 Worker host 都有共享 `SqliteUnitOfWork` 的独立 adapter；Conversation、Run、Statistics 使用分离的能力端口和查询协作者，Skills 只依赖 Plan 写端口。Worker 通过 `WorkerHostRepositoryPort` 与 `SqliteWorkerRepository` 独立于 HTTP 用例组合，同时在单一 Unit of Work 内完成 Artifact/QC 原子登记。旧 `RepositoryPort` 仅留给迁移兼容调用。目标仍为：

- `UnitOfWork`：事务、提交和回滚；应用幂等存储是独立端口并共享该 Unit of Work。
- `ProjectRepository`、`PlanRepository`、`RunRepository`、`QcRepository`、`ResultRepository`。
- `ConversationRepository`、`ModelProfileRepository`。
- `JobQueue`、`EventStore`、`IdempotencyStore`、`ArtifactCatalog`。

同一个 `SqliteUnitOfWork` 可以提供全部实现，因此不会增加部署组件。一个用例只拿到所需数据端口、幂等存储和事件存储，事务在应用服务入口明确开启。SQLAlchemy Row 到领域/应用模型的 mapper 归属于对应 SQLite adapter，不继续堆积在总仓储类中。

### 内部类型与公共 DTO

- 应用契约已迁至 `application/contracts/` 能力模块；HTTP 请求 DTO 由 `api/schemas/` 负责、响应 DTO 由 `api/responses/` 负责，两侧均通过显式 mapper 转换。所有 Python 调用点已改为从具体能力模块导入，`contracts/__init__.py` 不再重导出 DTO。
- `PlanState`、`WorkflowState`、Artifact 引用和执行结果属于领域/Workflow 内部类型，不放在 HTTP contracts 中。
- API schema 负责序列化和向后兼容；应用命令与查询结果负责用例边界；两者通过显式 mapper 转换。
- 所有公共请求继续 `extra="forbid"`，OpenAPI 作为前端类型的唯一来源，不再手写重复的 Conversation/Workspace 类型。

## 控制面与运行面

### 类型化任务协议

队列负载统一为版本化 `JobEnvelope`：

```json
{
  "schema_version": 1,
  "job_id": "...",
  "run_id": "...",
  "attempt": 1,
  "kind": "matlab.preprocessing",
  "command": {},
  "approval_binding": {},
  "execution_policy": {},
  "trace_id": "..."
}
```

`kind` 只能映射到已注册且声明 schema 版本、能力、超时、取消和产物合同的 executor。旧 `executor_type` 在迁移期由适配器读取，所有新任务只写 `JobEnvelope`。

### 原子状态和事件

- Run/Job 状态变更、attempt 建立和事件追加在同一 SQLite 事务中完成。
- 事件类型使用注册表和版本化 payload，前端只依赖稳定字段。
- Worker 心跳与用户进度分开；没有可靠测量时显示阶段和已完成步骤，不制造虚假 ETA。
- 重试创建新 attempt；已登记历史 Artifact 不覆盖，只标注来源 attempt 和 superseded 关系。

### Artifact 服务

目标架构增加受控 Artifact 内容访问层：

- 客户端只提交 Artifact ID，不提交文件路径。
- 服务端校验项目、运行、允许根目录、文件类型、大小和 checksum 后再预览或下载。
- NIfTI、JSON、TSV、Markdown、PNG 和日志分别使用白名单响应策略。
- 下载、导出和报告打包写审计事件；原始输入默认不提供下载代理。

## 前端结构

前端按功能组织，并把表单、服务端数据和视图分离：

```text
web/src/
├─ app/                        路由、布局、错误边界
├─ api/                        生成类型、基础 transport、幂等与 SSE
├─ features/
│  ├─ agent/                    AgentPage、会话展示模型、历史、草稿和样式
│  ├─ settings/                 Provider 与环境设置页面及测试
│  ├─ projects/
│  ├─ protocol-builder/
│  ├─ runs/
│  ├─ qc/
│  ├─ statistics/
│  ├─ agent/
│  └─ settings/
└─ shared/                     可访问性组件、格式化与通用 hooks
```

- `PlanPage` 改为以一个类型化 draft/reducer 为核心的分段表单，字段组件从 schema 和能力清单获得约束；不再为每个输入维护独立页面级状态。
- 每个 feature 提供查询/命令 hooks、表单模型和小组件，页面只负责组合。
- 浏览器的 workspace 持久化只保存工作区路径和项目选择；计划、运行、QC、统计指针仅作为内存投影，并由服务端会话恢复。对话选择与幂等键按各自短期用途存储。
- API client 已按 Agent、项目、计划、运行、统计、工作区、环境与 Literature 拆分，生产页面直接引用能力模块；`client.ts` 仅为迁移期测试和旧调用点保留兼容 facade，公共 transport 统一错误 envelope、取消、幂等重试与 SSE。
- 先使用项目内类型化 hooks；只有当缓存失效、并发查询和重试逻辑仍重复时，再通过独立 ADR 评估 TanStack Query，避免把库迁移和架构重构绑定在同一阶段。

## 关键运行链路

```text
注册只读数据 -> 冻结 manifest
             -> 编译 SkillPlan -> 校验 -> 人工审批
             -> 创建版本化 JobEnvelope
             -> Worker 原子领取 -> 受控 MATLAB/Mock
             -> 登记 Artifact + lineage -> QC revision/审批
             -> 冻结统计设计 -> 受控统计执行
             -> 结果证据校验 -> 确定性报告/导出包
```

Agent 可以在每个节点读取允许的摘要、解释阻断和提出结构化草稿，但所有箭头对应的状态变化仍由确定性应用服务完成。

## 质量目标

目标架构的验收至少满足：

- 不存在 Application 对 Infrastructure、SQLAlchemy、FastAPI 或 subprocess 的导入。
- 不存在 Workflow 状态机对 API/Application DTO 的反向依赖。
- 单个 API router、应用服务和 repository 只覆盖一个能力边界。
- 新增能力不需要修改总服务基类、总仓储协议和单一巨型路由。
- 崩溃重启后，已领取任务可以按租约恢复，历史 attempt、日志和 Artifact 可追踪。
- 真实结果包可离线复核 manifest、输入/参数/环境哈希和报告。
- Python 与 Web 门禁全部通过；关键用例按风险测试，不以总覆盖率替代分支验证。

## 迁移顺序

1. 冻结当前行为和 OpenAPI 快照，修复当前门禁，不改变科学语义。
2. 抽出内部状态/ID/Artifact 类型和窄端口，增加兼容重导出。
3. 拆分显式应用服务和 API routers，保留 URL 与响应兼容。
4. 拆分 SQLite adapters 和 mapper，引入 `JobEnvelope v1`。
5. 迁移前端 feature、服务端状态恢复和协议表单 reducer。
6. 完成真实主路径、Artifact 访问、QC/统计报告和发布验证。

任何一步都必须保持可运行、可迁移、可回滚；禁止把目录移动、公共 API 改版和科学行为变化放在同一个 PR。

## 非目标

- 微服务、云部署、多租户权限、PACS 集成和分布式调度。
- 通用 Agent 平台、任意插件执行或模型直接生成并运行代码。
- 自动选择受试者排除、统计方向、阈值、频段或“最显著”结果。
- 在目标架构内承诺所有 DPABI 版本、所有数据布局或所有多会话协议兼容。
