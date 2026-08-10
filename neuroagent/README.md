# NeuroAgent

`neuroagent` 是 rs-fMRI Agent 的模块化单体后端。当前候选基线已经实现数据检查、Skill 计划、审批、SQLite Worker、Mock 执行、QC、统计设计、模型路由和受控 MATLAB 适配器；公共 Worker 仍明确为 Mock-only，真实 MATLAB 小数据 smoke 必须另行授权。

## 运行模块

| 模块 | 当前职责 |
| --- | --- |
| `agent` | Provider 配置、能力路由、外发脱敏和结构化建议 |
| `api` | FastAPI REST/SSE、统一错误和前端静态入口 |
| `application` | 用例编排、公共契约、环境锁和端口 |
| `domain/fmri` | 预处理、指标、Artifact、QC 和统计纯领域规则 |
| `skills` | Skill Registry、Resolver、Validator 和 Compiler |
| `tools` | 输入 staging、DPABI V8.2 映射和类型化 Tool Registry |
| `workflow` | 状态机、SQLite 任务领取、租约、取消、重试和恢复 |
| `execution` | Mock 与默认禁止真实运行的 MATLAB Executor |
| `infrastructure` | 数据检查、路径策略、SQLite/Alembic 和环境探测 |
| `observability` | 持久化审计事件、SSE 游标和 trace ID |

`core`、`context`、`memory`、`retrieval`、`multi_agent` 和 `plugins` 来自早期通用框架草案，当前只保留边界说明，不是本 MVP 的运行依赖。当前结构以根目录 `AGENTS.md`、系统设计、fMRI Skill 设计和 ADR 为准。

## 依赖规则

API 和 Worker 调用应用服务；应用服务依赖领域模型与端口；基础设施和执行模块实现端口。领域层不得导入 FastAPI、SQLAlchemy 或进程执行代码。Agent 只能返回结构化建议，不能推进 Workflow 或启动 MATLAB。

完整目录说明见[项目结构](../docs/architecture/project-structure.md)。
