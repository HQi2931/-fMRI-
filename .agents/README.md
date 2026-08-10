# 开发协作 Agent 角色库

本目录保存项目专用的 Markdown 角色说明。根目录 `AGENTS.md` 是 Codex 自动读取的持久规则；本目录中的文件由主 Agent 根据任务需要读取，并作为委派子任务时的角色契约。

> 说明：Codex 原生自定义 Agent 使用 `.codex/agents/*.toml`。本目录遵循用户要求采用 Markdown，因此它是项目级角色库，而不是原生 Agent 配置目录。主 Agent 必须通过根 `AGENTS.md` 的路由规则显式使用这些角色。

## 角色索引

| 角色 | 文件 | 主要职责 | 默认权限 |
| --- | --- | --- | --- |
| 系统架构 Agent | [system-architect.md](system-architect.md) | 架构边界、数据流、ADR、跨模块设计 | 只读建议 |
| 后端服务 Agent | [backend-service.md](backend-service.md) | API、应用服务、领域模型、工作流和持久化 | 工作区写入 |
| Skill/Workflow Agent | [skill-workflow.md](skill-workflow.md) | SkillSpec、顺序约束、解析校验、编译和 Workflow 映射 | 工作区写入 |
| MATLAB/DPABI Agent | [matlab-dpabi.md](matlab-dpabi.md) | DPABI V8.2 接口、脚本模板、执行器和日志 | 工作区写入；真实运行需批准 |
| fMRI 方法学 Agent | [fmri-methodology.md](fmri-methodology.md) | 预处理、QC、统计设计和科学有效性审核 | 只读审核 |
| 前端体验 Agent | [frontend-ux.md](frontend-ux.md) | 本地 Web UI、长作业交互、QC 和结果展示 | 工作区写入 |
| QA/代码审查 Agent | [qa-review.md](qa-review.md) | 缺陷、安全、测试覆盖、回归和可恢复性 | 只读审核 |
| 文档维护 Agent | [documentation.md](documentation.md) | README、架构、ADR、领域方法、计划和变更记录 | 文档写入 |

## 路由建议

- 新功能设计：`system_architect` + 对应实现角色 + `qa_reviewer`。
- DPABI 预处理：`matlab_dpabi_engineer` + `fmri_methodologist`。
- fMRI Skill 或多指标工作流：`skill_workflow_engineer` + `matlab_dpabi_engineer` + `fmri_methodologist`。
- 统计分析：`fmri_methodologist` + `backend_service_engineer` + `qa_reviewer`。
- 前后端联调：`backend_service_engineer` + `frontend_ux_engineer`。
- 架构或接口变更：`system_architect` + `documentation_maintainer`。
- 发布前检查：`qa_reviewer` + `documentation_maintainer`。

## 协作规则

1. 主 Agent 始终负责需求确认、任务拆分、最终决策、冲突处理和结果整合。
2. 只把独立、边界清晰的任务委派给子 Agent；简单任务不启动子 Agent。
3. 默认最多并行启动三个子 Agent，并服从客户端实际并发上限。
4. 优先并行只读探索、方法审核、测试分析和文档检查。
5. 不允许两个写入型 Agent 同时编辑同一个文件或同一紧密耦合模块。
6. 子 Agent 不得自行继续生成下级 Agent，除非主 Agent 明确授权。
7. 委派消息必须包含：角色文件、具体目标、允许修改的文件、禁止事项、验证要求和返回格式。
8. 子 Agent 返回结论、证据、改动文件、验证结果和未解决问题，不返回无关的原始日志。
9. 主 Agent 在交付前检查实际差异并运行最终验证，不能直接拼接子 Agent 的结论。
10. 真实 MATLAB/DPABI 长作业、原始数据写入、受试者排除和统计方案改变仍需用户批准，角色委派不能扩大权限。

## 标准委派模板

```text
你是 {role_name}。开始前阅读：
1. /AGENTS.md
2. /.agents/{role_file}.md
3. 与任务直接相关的项目文档

目标：{bounded_goal}
允许修改：{file_scope}
禁止修改：{excluded_scope}
验证：{required_checks}

返回：
- 结论或完成内容
- 证据与文件路径
- 已运行的验证
- 风险和未解决问题
```
