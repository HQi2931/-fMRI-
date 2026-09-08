# ADR 0009：按能力演进模块化单体

- 状态：Proposed
- 日期：2026-09-07
- 目标里程碑：MVP1.0 / `v1.0.0`

## 背景

MVP0.1 已形成 FastAPI、SQLite、独立 Worker、React、Skill/审批、受控 MATLAB、QC、统计和 Agent 的本地闭环。随着持久化对话、工作区检查、真实执行和扩展分析进入同一应用，现有横向分层中的总服务、总 contracts、总 repository port、总 router 和页面级状态开始成为变更热点。

当前工作树已经出现多重继承 mixin 的同名方法类型冲突。该问题不是通过拆微服务解决的理由：产品仍是单用户本地应用，数据和事务主要位于同一个 SQLite 数据库，真实执行也依赖同一台 Windows 主机上的 MATLAB/SPM/DPABI。

## 决定

MVP1.0 采用按业务能力组织的模块化单体，并进行以下渐进式调整：

1. 保留单一 FastAPI 进程、独立 Worker、SQLite 和本地文件系统，不引入网络型基础设施。
2. 将 `NeuroAgentService` 多重继承门面替换为显式应用服务集合；跨能力调用使用窄 Query/Command 端口。
3. 将 `RepositoryPort` 拆为能力 repository、`JobQueue`、`EventStore`、`ArtifactCatalog`、`IdempotencyStore` 和 `UnitOfWork`；SQLite adapter 可以同时实现这些端口。
4. 将 API contracts、routers、SQLite mapper 和前端代码按 `projects`、`protocols`、`execution`、`qc`、`statistics`、`agent`、`environment` 切分。
5. 将状态机、强类型 ID、Artifact 引用和执行结果移出 HTTP DTO，消除 Workflow/Execution 对 Application contracts 的反向依赖。
6. 队列采用版本化、类型化 `JobEnvelope`；executor 按 kind、schema 版本和能力注册，不接受客户端提供的任意执行负载。
7. SQLite 保持服务端事实来源。浏览器只持久化选择器、事件游标和未决幂等 key，不持久化可替代服务端的业务快照。
8. 保持 `/api/v1` URL、响应语义、数据库内容和既有计划哈希兼容；通过重导出和读旧写新适配器分阶段迁移。

详细边界和迁移顺序见 [MVP1.0 目标架构](../architecture/mvp-1.0-target-architecture.md)。

## 后果

正面影响：

- 新能力可以在单一边界内增加 service、router、repository 和前端 feature，不再修改多个总文件。
- 应用用例的依赖、事务和测试替身更小，避免 mixin MRO 和隐式 `self` 协议。
- 控制面、运行面和 HTTP 传输模型解耦，任务升级与失败关闭规则更清晰。
- 保留本地部署和 SQLite 的运维优势，不增加用户安装负担。

代价：

- 迁移期会存在兼容重导出和新旧目录并存，需要明确删除窗口。
- DTO 与领域/应用模型分离后需要维护 mapper。
- repository 拆分必须配合 UnitOfWork，避免把一次原子用例错误拆成多个事务。
- 前端从页面级状态迁移到 feature hooks/reducer 会触及现有测试，需要先建立行为快照。

## 未采用方案

- **立即拆微服务**：没有多用户、独立扩缩容或跨节点需求，会引入部署、鉴权、网络失败和分布式事务成本。
- **继续扩大 mixin 总门面**：短期移动最少，但类型冲突和隐式耦合已经出现，无法支撑 1.0 的功能增长。
- **完全按技术层继续拆小文件**：只能降低文件行数，不能明确业务所有权和跨模块协议。
- **一次性重写或目录大迁移**：科研合同、审批哈希和真实执行风险过高，难以证明行为等价。
- **立即引入完整前端状态框架**：依赖选择不应与后端边界重构绑定；先通过 feature hooks、reducer 和服务端真源收敛问题，再单独评估。

## 接受条件

本 ADR 在以下内容经维护者确认后改为 `Accepted`：

- MVP1.0 范围和非目标获得确认。
- 第一个架构切片能在不改变 `/api/v1` 和科学合同的前提下通过全部门禁。
- 数据库迁移、旧 Job payload 和浏览器工作区状态均有兼容与回滚方案。

