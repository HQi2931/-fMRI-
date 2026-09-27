# ADR 0017：DPABI 阶段由模型建议、用户确认与服务端冻结

- 状态：Accepted
- 文档类型：ADR
- 维护者：matlab_dpabi_engineer
- 决策日期：2026-09-27
- 关联：[数据集输入与只读契约](../domain/dataset-contract.md)、[ADR 0008](0008-persistent-conversations-and-in-place-dpabi-workspaces.md)、[ADR 0014](0014-bounded-tool-loop.md)

## 背景

DPABI 会在预处理后生成 `FunImgA`、`FunImgAR`、`FunImgARW`、`FunImgARWS` 等 checkpoint。
仅凭 `FunRaw`/`FunImg` 前缀或目录排序无法判断用户说的“已经做完头动校正”对应哪个输入，
而 `R` 之后的步骤还可能依赖 `RealignParameter`、FD、T1 和掩膜等伴随证据。单阶段工作区也
不能因此跳过确认，否则会把目录命名误解为用户授权并重复处理。

## 决定

1. `DPARSFA_run.m` 的已核验版本生成版本化阶段规则表。只登记的阶段才可能成为候选；未知后缀
   只进入 inventory，不能因前缀匹配而运行。首批覆盖 `FunRaw`、`FunImg`、`FunImgAR`、
   `FunImgARW`、`FunImgARWS`，并保留扩展规则的显式入口。
2. Work 先调用只读阶段检查，返回所有候选、处理含义、覆盖数、规则版本和阻断原因。模型可读取
   这些服务端摘要并依据用户意图提出建议，但不得把建议写入请求、计划或启动动作；文件内容仍是
   不可信数据。
3. 用户确认阶段后，服务端重新检查候选令牌、工作区绑定和规则版本，把 `selected_input_stage`
   与候选摘要写入 manifest。即使只有一个 `FunRaw` 或 `FunImg`，也必须确认；工作区、阶段或规则
   变化会使此前确认、计划和审批失效。缺少显式阶段的旧请求失败关闭。
4. `in_place` 续跑只接受冻结 manifest 的阶段。编译/执行会关闭已完成步骤，禁止再次删除初始时间点，
   对后续操作检查 `RealignParameter` 等伴随证据，并在 Worker 启动前重验 manifest 全部文件哈希。
   目标输出碰撞、步骤冲突、哈希变化或未核验 DPABI 版本均阻断；执行入口仍属于服务端专属 EXECUTE。

## 后果

- 模型具备自然语言阶段意图识别能力，但模型建议永远不是裁决；用户能看到候选与服务端校验结果。
- manifest 的科学输入从“目录推断”变为可审计的阶段选择，计划哈希也因此绑定阶段规则和检查结果。
- `FunRaw`/`FunImg` 的历史隐式行为不再兼容，调用方必须在 UI 或确认口令中提供阶段。
- 续跑安全性依赖伴随文件和源码版本证据；缺失证据时宁可不运行，也不把中间目录当作完整输入。

## 取舍

- 不建立全工作区全文索引，阶段检查按轮次读取目录并保存摘要，降低敏感数据外发和陈旧索引风险。
- 不让模型调用应用 EXECUTE 工具；模型只可通过结构化建议与 READ 阶段工具参与对话。
- 不在首版扩展 DICOM 转换或扫描版识别；仅 DICOM 的 `FunRaw` 可展示为 inventory，但不能运行。
