# execution

## 模块职责

`execution` 负责 JobSpec、JobExecutor、受控执行器、沙箱边界和 ArtifactRef 注册。它把工具请求转换为可审计、可取消、可超时的执行任务。

## 模块边界

Agent 不直接调用任意 Shell。`execution` 不决定任务目标，不修改工作流状态，不把大文件直接塞进模型上下文，也不绕过策略审批。

## 依赖关系

`execution` 被 `tools` 和 `workflow` 间接调用；它依赖 `infrastructure/object_store` 或文件存储适配保存产物，依赖 `observability` 记录 Job 与 Artifact 事件。

## 当前阶段

仅建立 jobs、executors、sandbox 和 artifacts 的目录边界，未实现执行器或产物管理器。

## 后续核心接口

- `JobSpec`
- `JobResult`
- `JobExecutor`
- `SandboxPolicy`
- `ArtifactRef`
- `ArtifactManager`

