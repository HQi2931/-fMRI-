"""Deterministic Skill selection for metric requests."""

from __future__ import annotations

from neuroagent.domain.fmri.metrics import MetricKind
from neuroagent.skills.models import (
    EnvironmentSnapshot,
    IssueSeverity,
    SkillRequest,
    SkillResolution,
    SkillValidationIssue,
)
from neuroagent.skills.registry import SkillRegistry, SkillRegistryError


class SkillResolver:
    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    def resolve(self, request: SkillRequest, environment: EnvironmentSnapshot) -> SkillResolution:
        metric_set = set(request.requested_metrics)
        alff_requested = bool(metric_set & {MetricKind.ALFF, MetricKind.FALFF})
        ids: list[str] = []
        if request.request_preprocessing:
            ids.append("rsfmri.preprocess.common")
        if alff_requested and MetricKind.REHO in metric_set:
            ids.append("rsfmri.pipeline.alff_reho_combined")
        elif alff_requested:
            ids.append("rsfmri.metric.alff_falff")
        elif MetricKind.REHO in metric_set:
            ids.append("rsfmri.metric.reho")

        selected = []
        issues: list[SkillValidationIssue] = []
        for skill_id in ids:
            try:
                selected.append(self._registry.resolve(skill_id))
            except SkillRegistryError as exc:
                issues.append(
                    SkillValidationIssue(
                        code="SKILL_NOT_AVAILABLE",
                        severity=IssueSeverity.BLOCKING,
                        message=str(exc),
                        path="requested_metrics",
                    )
                )
        if not ids:
            issues.append(
                SkillValidationIssue(
                    code="UNSUPPORTED_METRIC_REQUEST",
                    severity=IssueSeverity.BLOCKING,
                    message="No reviewed Skill matches the requested metric set",
                    path="requested_metrics",
                )
            )
        return SkillResolution(
            request=request,
            environment=environment,
            selected_specs=tuple(selected),
            issues=tuple(issues),
        )
