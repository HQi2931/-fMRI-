# ROI、机器学习与脑区定位领域合同

## ROI

DPABI V8.2 的 `y_ExtractROISignal` 接受 4D 功能像、ROI mask/多标签 atlas、去趋势、频带、TR、时间掩码和 scrubbing 参数。系统先校验 Nyquist、网格、时间点、ROI 索引和标签，再生成：

- 长表：`subject_id, session_id, metric, atlas_id, roi_index, roi_label, value`
- 宽表：每个 subject/session 一行，每个 ROI 一个稳定列名

## 表格机器学习

首期只支持经典表格模型。特征处理必须在每个交叉验证折内完成；分割以 subject 为最小单位，固定随机种子，报告 ROC/AUC、PR、平衡准确率和校准信息。target、group、缺失策略和模型需要用户确认，不能通过多次试验追求显著结果。

## cluster 定位

DPABI cluster 表中的峰值坐标、体素数和统计量先保存原样。只有用户提供 atlas 和坐标/标签合同后才匹配脑区；没有 atlas 时只返回坐标与 cluster 信息，不猜测脑区，也不输出临床诊断。

## 方法学问答

问答只覆盖 rs-fMRI、s-fMRI、DPABI、SPM、指标、预处理、QC 和统计设计。答案必须带本地证据片段与版本限制；本地证据不足时明确说明，联网检索尚未在当前 MVP 默认启用。
