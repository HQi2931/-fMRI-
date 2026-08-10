# rs-fMRI Agent 系统设计

状态：MVP 候选架构；公共运行路径当前为 Mock-only
版本：0.1

## 目标

目标系统在本机完成静息态 fMRI 数据检查、方案编译、人工审批、MATLAB/DPABI 执行、QC 和组统计。Agent 用于解释和补全结构化请求，所有科学规则、状态变更和外部执行由确定性组件控制。

当前候选基线已经完成元数据、Skill、审批、SQLite 队列、Mock 执行、人工 QC、统计设计/校正参数校验、结果完整性合同和确定性复现报告。公共 API 与 Worker 尚未接入真实 MATLAB/DPABI 或真实统计执行，不会生成真实效应量或显著簇表。

## 运行拓扑

```text
React Web (localhost)
  -> FastAPI /api/v1
     -> Application Services
        -> fMRI Domain + Skill Resolver/Validator/Compiler
        -> PlanRevision + ApprovalRecord
        -> SQLite Workflow Queue
  <- REST + SSE

Worker (current public run path)
  -> atomic job claim
  -> injected MockJobExecutor
  -> run state + persisted events
  -> mock.result Artifact metadata

Target scientific execution path (not wired to the public Worker)
  -> approved SkillPlan DAG
  -> ToolRegistry / Policy / ToolRuntime
  -> controlled MatlabExecutor
  -> isolated run workspace
  -> scientific Artifact + Provenance + AuditEvent

Synthetic verification seam (test-only; not a public Worker/API path)
  -> explicitly marked placeholder result artifacts
  -> StatisticalResultManifest + deterministic Markdown/JSON report
```

SQLite 保存元数据、计划、审批、运行和事件。受控执行目标是在文件系统保存配置、日志和衍生产物，并在数据库中只保存 Artifact 引用、校验和与谱系；当前 Mock 路径只登记 `mock.result` 元数据，不生成科学影像。API 进程不启动 MATLAB，Worker 不接受自由 Shell/MATLAB 文本。

当前 Web/API 只创建 Mock 作业，用于安全地验证审批、排队、运行状态、事件续传和通用 Artifact 登记闭环。Skill 编译器会解析并锁定工具能力，但公共 Worker 尚不遍历 `SkillPlan` DAG，也不通过 `ToolRegistry` 或 `ToolRuntime` 分派步骤。受控 MATLAB 执行器接受类型化 `MatlabJobSpec`，默认拒绝真实执行；在 DAG/Tool 运行时接线、真实小数据 smoke 和单独运行授权完成前，不把它隐式接入公共 Worker。

## 分层和依赖方向

- API：HTTP、请求校验、受控错误 envelope、SSE；只调用应用服务。统一 envelope 覆盖应用错误、请求校验、框架 HTTP 错误、未匹配路径和不暴露异常详情的意外 500。
- Application：用例、事务、幂等键、revision、审批边界和确定性复现报告编排。
- Domain：数据、Artifact、Skill、QC、统计结果和状态机规则；不依赖 Web、数据库或 subprocess。
- Skills：加载、解析、校验和编译声明式协议；输出不可变 SkillPlan。
- Workflow/Tools：Workflow 状态机已经接入公共 Mock 闭环；Tool 合同、Registry 和能力锁用于编译与独立验证，运行时 DAG/Tool 分派仍待接线。
- Execution/Infrastructure：SQLite、文件、模型 Provider 和 MATLAB 适配器。
- Agent：只生成结构化建议；不能直接推进 PlanRevision 或 WorkflowInstance。

目标依赖方向为 API/Application -> Domain/Ports，Infrastructure 实现 Ports。领域层不得导入 FastAPI、SQLAlchemy、httpx 或 subprocess；候选树必须通过架构检查并在发布审查中确认没有反向依赖。

## 控制面和运行面

`SkillPlan` 内容不可变。`PlanRevision` 管理 `draft -> validating -> awaiting_approval -> approved/superseded`；`ApprovalRecord` 绑定计划、输入清单和环境哈希。任何绑定内容改变都会使审批失效。

批准后才能创建 `WorkflowInstance`。运行状态唯一真源为：

```text
queued -> running -> qc_review -> succeeded
   |         |           |
   +-------> failed <-----+
   +-------> cancelled
```

重试创建新的 attempt 并复用不可变计划，不覆盖历史事件或产物记录。图中是需要人工 QC 的常规/指标 Mock 路径；`statistics_mock` 是协议占位路径，按 `queued -> running -> succeeded` 完成，不产生科学统计结果。

当前通用 Mock 作业可以进入 `qc_review`，但 `mock.result` 不是可用于科学 QC 或统计的指标图。统计运行端点同样创建 `statistics_mock` 作业；它验证运行协议，不执行 t 检验、FDR 或 GRF。纯合成演示只能通过内部测试 seam 注入醒目标记的占位结果，再验证完整性合同和报告生成器；该 seam 不是公共 API。

## 数据安全

- 数据源注册为允许根目录，扫描只读，先规范化再检查父子关系。
- DICOM/NIfTI 转换和 DPABI 运行只写入 `runs/{run_id}`。
- 客户端使用 Artifact ID 查询元数据，不提交任意产物路径。当前 API 没有 Artifact 文件下载端点。
- 外部模型调用前执行 OutboundContextPolicy；无法证明安全则拒绝调用。
- 原始数据、人口学表、运行数据库、日志和结果由 `.gitignore` 与安全门禁双重阻止。

## 部署

MVP 为 Windows 本机单用户应用，监听 `127.0.0.1`。开发时 Web 和 API 分离；构建后由 FastAPI 提供 `web/dist`。不引入容器、云队列、Redis 或多用户权限系统。真实科研执行接线、实际统计产物发现/登记和结果查询仍是后续实现/发布决定，不应从确定性报告合同推断为真实结果已可用。
