# skills

## 模块职责

`skills` 管理可加载的专业知识、行为规范、提示模板、操作准则和能力声明，使 Agent 能在不同任务中加载受控的技能上下文。

## 模块边界

Skill 不等于 Plugin。Skill 不应携带不可审计的执行能力，也不直接注册外部工具；工具、工作流和领域实现应由插件提供。

## 依赖关系

`skills` 被 `context` 和 `core` 调用，用于选择和组织 Agent 所需的行为规范；它可由 `plugins` 提供扩展来源，并通过 `observability` 记录加载事件。

## 当前阶段

仅建立 registry、loader 和 schemas 的目录边界，未实现技能加载器。

## 后续核心接口

- `SkillSpec`
- `SkillRegistry`
- `SkillLoader`
- `SkillContext`
- `SkillSchema`

