# 组统计设计

## 目标能力

MVP 目标支持单样本 t 检验、两独立样本 t 检验、配对 t 检验和显式协变量。每次分析保存不可变 `StatisticalDesignRevision`。

设计至少包含：冻结 subject 顺序、组编码、配对键、图像 Artifact、协变量列、缺失策略、中心化方法、contrast、mask 和 tail。软件版本不属于 `StatisticalDesignRevision` 本体，而由外层 `PlanRevision` 的环境哈希、Skill 锁和 Tool 锁绑定，并随审批一起校验。任何设计字段变化都创建新的统计设计 revision；环境或软件锁变化会使旧计划审批失效，必须重新生成并审批计划。

多重比较与检验分开配置：

- FDR：显式 `q`、tail 和 mask。
- GRF：显式 voxel p、cluster p、tail、mask 与平滑度来源。

目标结果合同包含设计矩阵、contrast、未校正统计图、校正结果、效应量、簇表、参数、日志和完整 provenance。系统不得自动尝试多个方向、阈值或方法后选择更显著结果。

## 当前已接线能力

当前候选基线已经接线统计设计 revision 的创建、结构校验、显式审批和审批失效规则，也能列出并校验 FDR/GRF 参数合同。受试者顺序、组方向、配对关系、协变量列和 contrast 由领域测试覆盖。

公共 `POST /api/v1/statistics/runs` 当前只创建 `statistics_mock` 作业。Worker 不调用 DPABI 统计函数，也不生成未校正统计图、校正结果、效应量或簇表。

当前已提供 `StatisticalResultManifest` 完整性合同和不调用模型的确定性 Markdown/JSON 复现报告生成器。真实模式要求设计矩阵、contrast、未校正图、按需校正图、effect map、簇表、执行日志与软件版本证据全部存在，且簇连通性必须显式声明；缺失时失败关闭。合成模式只允许醒目标记的非科学占位证据，不能用于推断。只有在真实统计执行器和产物登记链路完成并验证后，才能把报告作为真实结果能力声明。
