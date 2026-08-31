# 分析辅助模块（analysis）

`neuroagent.analysis` 是纯领域辅助模块：提供确定性的分析预览、诊断、模板与合同，**不启动进程、不修改原始数据、不读取密钥、不联网**。

服务层（`neuroagent.application`）负责把项目根目录转换为允许的只读路径，再调用本模块；输出模型包含内容哈希、证据或 lineage 所需字段。

## 模块

| 模块 | 职责 |
| --- | --- |
| `models.py` | 稳定的分析合同（Pydantic frozen 模型），不含绝对路径、可执行文本或可变状态 |
| `diagnostics.py` | 确定性 DPABI/MATLAB 日志分类器（fail-closed，含路径/ID/邮箱脱敏） |
| `clusters.py` | DPABI cluster 表解析与 atlas 坐标最近邻匹配（几何启发式，校验坐标空间一致） |
| `roi.py` | ROI 信号合同的 Nyquist/频带校验与长宽表导出 |
| `tables.py` | CSV/TSV/XLSX 只读检查（不修改上传文件，损坏文件失败关闭） |
| `templates.py` | 生成可审查的 Python ML 模板（不执行、不注入命令/绝对路径） |
| `organization.py` | 只读的 DPABI 目录整理预览（不复制文件，拒绝目录穿越） |
| `rag.py` | 本地 rs-fMRI 证据检索与范围拒答（不联网、不调模型） |

## 边界

- 本模块当前为**本地确定性预览**：不启动 MATLAB、不训练模型、不联网检索。
- 真实 `y_ExtractROISignal`、隔离 ML Runner、cluster NIfTI 网格采样与脱敏联网 RAG 均待 v0.2.0 单独接线与授权。
- `write_roi_exports` 与 `parse_cluster_table` 目前仅被测试调用，未接入任何 service/API 路径；若未来接线，必须先经过 PathPolicy 与批准 Artifact Tool 门控。
