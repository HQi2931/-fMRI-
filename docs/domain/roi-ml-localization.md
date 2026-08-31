# ROI、机器学习与脑区定位领域合同

## ROI

DPABI V8.2 的 `y_ExtractROISignal` 接受 4D 功能像、ROI mask/多标签 atlas、去趋势、频带、TR、时间掩码和 scrubbing 参数。系统先校验 Nyquist、频带一致性、scrubbing 时序和 ROI 索引去重；网格、时间点和标签作为合同声明（`required_lineage`），当前确定性预览尚未逐项校验。校验通过后生成：

- 长表：`subject_id, session_id, metric, atlas_id, roi_index, roi_label, value`
- 宽表：每个 subject/session 一行，每个 ROI 一个稳定列名

## 表格机器学习

首期只支持经典表格模型。特征处理必须在每个交叉验证折内完成；分割以 subject 为最小单位，固定随机种子，报告 ROC/AUC 与 PR；平衡准确率、校准信息与科研图生成属后续隔离 Runner 边界，当前模板未产出。target、group（显式 subject/participant 标识列，与临床分组标签解耦）、缺失策略和模型需要用户确认，不能通过多次试验追求显著结果。

## cluster 定位

DPABI cluster 表中的峰值坐标、体素数和统计量先保存原样。只有用户提供 atlas 和坐标/标签合同后才匹配脑区；没有 atlas 时只返回坐标与 cluster 信息，不猜测脑区，也不输出临床诊断。

## 方法学问答

问答只覆盖 rs-fMRI、DPABI、SPM、指标、预处理、QC 和统计设计。答案基于本地证据片段并标注研究用途限制与来源；证据的版本锁定与脱敏联网检索仍在后续阶段，本地证据不足时明确说明。
