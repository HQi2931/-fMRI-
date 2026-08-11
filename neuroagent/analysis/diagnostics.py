"""Fail-closed DPABI/MATLAB log classification."""

from __future__ import annotations

import re
from pathlib import Path

from neuroagent.analysis.models import FailureCode, FailureDiagnosis

_RULES: tuple[tuple[FailureCode, str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        FailureCode.INPUT_MISSING,
        r"(file|directory|folder).*(not found|does not exist)|no such file|无法找到|不存在",
        ("输入文件或目录不存在",),
        ("重新扫描数据集并核对冻结 manifest", "确认 staging 包含所有绑定输入"),
    ),
    (
        FailureCode.NIFTI_HEADER,
        (
            r"(dimension|voxel size|TR|header|orientation|mask).*(mismatch|inconsistent|error)"
            r"|头信息|维度不一致|TR"
        ),
        ("NIfTI 头信息、网格、掩膜或 TR 可能不一致",),
        ("检查输入与 mask 的网格和空间方向", "在方案中明确 TR 与有效时间点"),
    ),
    (
        FailureCode.DPABI_CONFIGURATION,
        r"(cfg|configuration|parameter|field).*(invalid|unknown|missing)|字段.*错误|参数.*错误",
        ("DPABI Cfg 参数未通过校验",),
        ("重新生成结构化 Cfg 并查看字段映射", "不要直接编辑 MATLAB 文本"),
    ),
    (
        FailureCode.SOFTWARE_ENVIRONMENT,
        (
            r"(undefined function|function .* undefined|spm|dpabi|matlab).*(not found|undefined|"
            r"cannot find)|undefined function|函数.*未找到"
        ),
        ("MATLAB, SPM12 或 DPABI 入口不可见",),
        ("重新运行环境探测", "核对 MATLAB, SPM12 和 DPABI V8.2 路径"),
    ),
    (
        FailureCode.RESOURCE,
        r"(out of memory|memory|disk space|no space|内存不足|磁盘空间不足)",
        ("运行资源不足",),
        ("检查工作盘剩余空间", "降低并发或分块参数; 修改资源参数需重新审批"),
    ),
    (
        FailureCode.PATH_POLICY,
        r"(permission denied|access is denied|path traversal|权限拒绝|目录越界)",
        ("路径或权限策略阻断了运行",),
        ("将输入放入允许的只读根目录", "将输出改到项目 staging 工作区"),
    ),
    (
        FailureCode.TIMEOUT,
        r"(timed out|timeout|超时)",
        ("作业超过已批准的超时限制",),
        ("检查日志和已生成产物", "如需延长超时, 创建新计划 revision 并重新审批"),
    ),
)


def _safe_line(line: str) -> str:
    value = line.strip()
    value = re.sub(r"(?i)([A-Z]:\\|/)(?:[^\s\\/]+[\\/])*[^\s]+", "<path>", value)
    value = re.sub(r"(?i)(subject|sub|participant)[-_ ]?[A-Za-z0-9]+", r"\1<id>", value)
    return value[:500]


def diagnose_dpabi_log(log: str | Path, *, max_evidence: int = 8) -> FailureDiagnosis:
    """Classify a log without sending it to a model or exposing local paths."""

    text = Path(log).read_text(encoding="utf-8", errors="replace") if isinstance(log, Path) else log
    lines = [_safe_line(line) for line in text.splitlines() if line.strip()]
    lower = text.lower()
    for code, pattern, evidence, suggestions in _RULES:
        if re.search(pattern, lower, flags=re.IGNORECASE):
            matching = tuple(
                line for line in lines if re.search(pattern, line, flags=re.IGNORECASE)
            )
            return FailureDiagnosis(
                code=code,
                severity="error",
                summary=evidence[0],
                evidence=(matching or tuple(lines[-max_evidence:]))[-max_evidence:],
                suggestions=suggestions,
                confidence=0.9,
                requires_new_plan=code not in {FailureCode.INPUT_MISSING, FailureCode.PATH_POLICY},
            )
    if "cancel" in lower or "取消" in text:
        return FailureDiagnosis(
            code=FailureCode.CANCELLED,
            severity="warning",
            summary="作业被用户或进程取消",
            evidence=tuple(lines[-max_evidence:]),
            suggestions=("确认取消原因; 如需继续, 使用相同批准计划重新排队",),
            confidence=0.85,
            requires_new_plan=False,
        )
    return FailureDiagnosis(
        code=FailureCode.UNKNOWN,
        severity="error",
        summary="未识别的 MATLAB/DPABI 错误, 需要人工查看日志",
        evidence=tuple(lines[-max_evidence:]),
        suggestions=("保留本次 attempt 的日志和产物", "确认无敏感信息后提交诊断上下文"),
        confidence=0.1,
        requires_new_plan=True,
    )
