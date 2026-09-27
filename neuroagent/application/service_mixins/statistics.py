"""Statistical design creation and plan-current validation use cases."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from neuroagent.application.contracts import (
    ArtifactView,
    CorrectionCapabilityView,
    PlanRevisionView,
    StatisticalDesignCreate,
    StatisticalDesignValidationRequest,
    StatisticalDesignView,
)
from neuroagent.application.errors import ApplicationError, ConflictError, InputValidationError
from neuroagent.application.hashing import content_hash
from neuroagent.application.service_mixins._base import BaseServiceMixin
from neuroagent.application.statistics_draft import (
    ArtifactRef,
    DesignIssue,
    QcBinding,
    StatisticalDesignProposal,
    StatisticsChoices,
    StatisticsDraft,
    assemble_statistical_design,
    build_correction,
    collect_design_sources,
    proposal_hash,
    render_statistics_draft_review,
    statistics_draft_hash,
)
from neuroagent.domain.fmri.artifacts import ArtifactKind, ArtifactLineage
from neuroagent.domain.fmri.qc import assert_statistics_ready
from neuroagent.domain.fmri.statistics import (
    FdrCorrection,
    GrfCorrection,
    MissingValuePolicy,
    StatisticalDesignRevision,
    StatisticalQcSource,
    StatisticalTest,
    Tail,
    design_matrix,
    validate_correction_for_design,
)
from neuroagent.skills.models import SkillPlan
from neuroagent.skills.registry import SkillRegistryError
from neuroagent.tools.registry import ToolRegistryError

#: Mask artifact type produced by the controlled preprocessing path.
_BRAIN_MASK_TYPE = "preprocessing.brain_mask"


def _design_issue(code: str, message: str) -> DesignIssue:
    return DesignIssue(code=code, message=message)


def _artifact_ref(artifact: ArtifactView) -> ArtifactRef:
    """Read subject identity from the registered lineage, never from the path."""

    lineage = artifact.provenance.get("lineage")
    subject_id = lineage.get("subject_id") if isinstance(lineage, dict) else None
    return ArtifactRef(
        artifact_id=artifact.artifact_id,
        artifact_type=artifact.artifact_type,
        subject_id=subject_id if isinstance(subject_id, str) and subject_id else None,
    )


def _choices_from_draft(draft: StatisticsDraft) -> StatisticsChoices:
    return StatisticsChoices(
        test=draft.test or StatisticalTest.ONE_SAMPLE_T,
        grouping_column=draft.grouping_column,
        group_order=tuple(draft.group_order),
        condition_column=draft.condition_column,
        condition_order=tuple(draft.condition_order),
        covariates=tuple(draft.covariates),
        tail=draft.tail or Tail.TWO_SIDED,
        one_sample_baseline=draft.one_sample_baseline,
        missing_value_policy=draft.missing_value_policy or MissingValuePolicy.ERROR,
    )


def _validate_statistical_locks(
    service: Any, plan: PlanRevisionView, reasons: list[dict[str, str]]
) -> None:
    """Append drift reasons for frozen statistical Skill and Tool locks."""

    expected_hash = content_hash(
        {
            "plan": plan.plan,
            "manifest_hash": plan.manifest_hash,
            "environment_hash": plan.environment_hash,
        }
    )
    if expected_hash != plan.plan_hash:
        reasons.append(
            {
                "code": "statistical_plan_hash_changed",
                "message": "stored statistical design no longer matches its approved hash",
            }
        )
    for lock_data in plan.plan.get("skill_locks", []):
        try:
            current = service.skill_registry.resolve(
                str(lock_data["skill_id"]), str(lock_data["version"])
            )
            if current.content_hash != str(lock_data["content_hash"]):
                raise SkillRegistryError("content hash changed")
        except (KeyError, TypeError, SkillRegistryError):
            reasons.append(
                {
                    "code": "statistics_skill_lock_changed",
                    "message": str(lock_data),
                }
            )
    for lock_data in plan.plan.get("tool_locks", []):
        try:
            capability = str(lock_data["capability"])
            current_tool = service.tool_registry.resolve_capability(capability)
            if current_tool.model_dump(mode="json") != lock_data:
                raise ToolRegistryError("content hash changed")
        except (KeyError, TypeError, ToolRegistryError):
            reasons.append(
                {
                    "code": "statistics_tool_lock_changed",
                    "message": str(lock_data),
                }
            )
    if not plan.plan.get("skill_locks") or not plan.plan.get("tool_locks"):
        reasons.append(
            {
                "code": "statistics_execution_locks_missing",
                "message": "statistical plan lacks frozen Skill/Tool locks",
            }
        )


class StatisticsMixin(BaseServiceMixin):
    @staticmethod
    def _validate_correction(
        design: StatisticalDesignRevision,
        correction: FdrCorrection | GrfCorrection | None,
    ) -> None:
        try:
            validate_correction_for_design(design, correction)
        except ValueError as exc:
            raise InputValidationError(
                "invalid_correction_for_design",
                "多重比较校正参数与冻结的统计设计不一致。",
                reason=str(exc),
            ) from exc

    def _require_skill_parameter_schema(self, skill_directory: str, payload: object) -> None:
        report = self.skill_validator.validate_parameter_payload(skill_directory, payload)
        if not report.has_blockers:
            return
        raise InputValidationError(
            "skill_parameter_schema_invalid",
            "请求参数不符合内置 Skill 的运行时 Schema。",
            skill_directory=skill_directory,
            issues=[issue.model_dump(mode="json") for issue in report.issues],
        )

    @staticmethod
    def _statistics_view(plan: PlanRevisionView) -> StatisticalDesignView:
        if plan.plan.get("kind") != "statistical_design":
            raise InputValidationError(
                "not_statistical_design",
                "指定的计划版本不是统计设计。",
                plan_revision_id=plan.plan_revision_id,
            )
        design = StatisticalDesignRevision.model_validate(plan.plan.get("design"))
        correction_data = plan.plan.get("correction")
        correction: FdrCorrection | GrfCorrection | None = None
        if isinstance(correction_data, dict):
            if correction_data.get("method") == "fdr":
                correction = FdrCorrection.model_validate(correction_data)
            elif correction_data.get("method") == "grf":
                correction = GrfCorrection.model_validate(correction_data)
            else:
                raise InputValidationError(
                    "unsupported_correction", "统计设计包含不受支持的校正方法。"
                )
        matrix = design_matrix(design)
        stored_matrix = plan.plan.get("design_matrix")
        if stored_matrix != [list(row) for row in matrix]:
            raise InputValidationError(
                "design_matrix_integrity_failed",
                "冻结的设计矩阵与统计设计不一致。",
            )
        StatisticsMixin._validate_correction(design, correction)
        return StatisticalDesignView(
            design=design,
            correction=correction,
            design_matrix=matrix,
            plan_revision=plan,
        )

    def _statistics_execution_locks(
        self, correction: FdrCorrection | GrfCorrection | None
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        skill_ids = ["rsfmri.statistics.ttest"]
        if correction is not None:
            skill_ids.append(f"rsfmri.statistics.{correction.method}")
        specs = [self.skill_registry.resolve(skill_id) for skill_id in skill_ids]
        skill_locks = [
            {
                "skill_id": spec.skill_id,
                "version": spec.version,
                "content_hash": spec.content_hash,
            }
            for spec in sorted(specs, key=lambda item: item.skill_id)
        ]
        capabilities = sorted(
            {capability for spec in specs for capability in spec.required_capabilities}
        )
        tool_locks = [
            self.tool_registry.resolve_capability(capability).model_dump(mode="json")
            for capability in capabilities
        ]
        return skill_locks, tool_locks

    @staticmethod
    def _registered_lineage(artifact: ArtifactView) -> ArtifactLineage:
        lineage_data = artifact.provenance.get("lineage")
        if not isinstance(lineage_data, dict):
            raise InputValidationError(
                "artifact_lineage_missing",
                "统计输入必须使用带有服务端登记类型化 lineage 的 Artifact。",
                artifact_id=artifact.artifact_id,
            )
        try:
            lineage = ArtifactLineage.model_validate(lineage_data)
        except ValidationError as exc:
            raise InputValidationError(
                "artifact_lineage_invalid",
                "统计输入 Artifact 的登记 lineage 无效。",
                artifact_id=artifact.artifact_id,
            ) from exc
        if lineage.artifact_id != artifact.artifact_id:
            raise InputValidationError(
                "artifact_lineage_identity_mismatch",
                "统计输入 Artifact ID 与登记 lineage 不一致。",
                artifact_id=artifact.artifact_id,
            )
        return lineage

    def _validate_statistical_artifact_lineage(
        self,
        design: StatisticalDesignRevision,
        *,
        manifest_hash: str,
        source_run_id: str,
    ) -> None:
        metric_kinds = {
            ArtifactKind.ALFF_MAP,
            ArtifactKind.FALFF_MAP,
            ArtifactKind.REHO_MAP,
        }
        mask_artifact = self.repository.get_artifact(design.mask_artifact_id)
        if mask_artifact.run_id != source_run_id:
            raise ConflictError(
                "statistics_mask_run_mismatch",
                "统计 mask 必须属于已批准 QC 的来源运行。",
            )
        mask = self._registered_lineage(mask_artifact)
        if (
            mask.kind is not ArtifactKind.BRAIN_MASK
            or not mask.metadata_verified
            or mask.subject_manifest_hash != manifest_hash
            or mask.subject_id is not None
        ):
            raise InputValidationError(
                "statistics_mask_lineage_invalid",
                "统计 mask 必须是同 manifest、元数据已验证的组级 BRAIN_MASK Artifact。",
            )

        reference_signature: tuple[object, ...] | None = None
        for image in design.images:
            artifact = self.repository.get_artifact(image.artifact_id)
            if artifact.run_id != source_run_id:
                raise ConflictError(
                    "statistics_artifact_run_mismatch",
                    "统计影像必须属于已批准 QC 的来源运行。",
                    artifact_id=artifact.artifact_id,
                )
            lineage = self._registered_lineage(artifact)
            if lineage.kind not in metric_kinds or not lineage.metadata_verified:
                raise InputValidationError(
                    "statistics_metric_lineage_invalid",
                    "统计输入必须是元数据已验证的受试者级 ALFF/fALFF/ReHo Artifact。",
                    artifact_id=artifact.artifact_id,
                )
            if lineage.subject_manifest_hash != manifest_hash:
                raise ConflictError(
                    "statistics_artifact_manifest_mismatch",
                    "统计 Artifact lineage 与冻结 manifest 不一致。",
                    artifact_id=artifact.artifact_id,
                )
            if lineage.subject_id != image.subject_id:
                raise InputValidationError(
                    "statistics_artifact_subject_mismatch",
                    "AnalysisImage.subject_id 必须等于 Artifact lineage.subject_id。",
                    artifact_id=artifact.artifact_id,
                    expected=lineage.subject_id,
                    received=image.subject_id,
                )
            if lineage.condition != image.condition:
                raise InputValidationError(
                    "statistics_artifact_condition_mismatch",
                    "AnalysisImage.condition 必须等于 Artifact lineage.condition。",
                    artifact_id=artifact.artifact_id,
                    expected=lineage.condition,
                    received=image.condition,
                )
            if lineage.mask_artifact_id != design.mask_artifact_id:
                raise InputValidationError(
                    "statistics_metric_mask_mismatch",
                    "所有统计指标图必须引用统计设计中冻结的同一 mask Artifact。",
                    artifact_id=artifact.artifact_id,
                )
            if lineage.grid_signature != mask.grid_signature:
                raise InputValidationError(
                    "statistics_metric_grid_mismatch",
                    "统计指标图与 mask 的网格签名不一致。",
                    artifact_id=artifact.artifact_id,
                )
            band = (
                None
                if lineage.frequency_band is None
                else (lineage.frequency_band.low_hz, lineage.frequency_band.high_hz)
            )
            signature = (
                lineage.kind,
                lineage.metric_scaling,
                # Paired conditions may deliberately come from two sessions;
                # the explicit condition order and per-image lineage carry
                # that pairing while all processing attributes must match.
                None if design.test is StatisticalTest.PAIRED_T else lineage.session_id,
                lineage.space,
                lineage.grid_signature,
                lineage.voxel_size_mm,
                lineage.mask_artifact_id,
                lineage.mask_grid_signature,
                lineage.temporally_filtered,
                band,
                lineage.spatially_smoothed,
                lineage.smoothing_fwhm_mm,
                lineage.scrubbed,
            )
            if reference_signature is None:
                reference_signature = signature
            elif signature != reference_signature:
                raise InputValidationError(
                    "statistics_metric_series_mismatch",
                    "同一统计设计中的指标类型、缩放、空间、网格与处理谱系必须一致。",
                    artifact_id=artifact.artifact_id,
                )

    def create_statistical_design(
        self, request: StatisticalDesignCreate, idempotency_key: str
    ) -> StatisticalDesignView:
        def prepare() -> tuple[tuple[tuple[float, ...], ...], dict[str, Any], str]:
            self._require_project_version(request.project_id, request.expected_project_version)
            environment = self.environment_provider.current().snapshot
            image_artifact_ids = tuple(image.artifact_id for image in request.design.images)
            source_run_id = self._validate_statistics_qc_source(request, image_artifact_ids)
            self.repository.assert_artifacts_belong_to_run(
                (*image_artifact_ids, request.design.mask_artifact_id),
                source_run_id,
            )
            self._validate_statistical_artifact_lineage(
                request.design,
                manifest_hash=request.input_manifest_hash,
                source_run_id=source_run_id,
            )
            self._validate_correction(request.design, request.correction)
            self._require_skill_parameter_schema(
                "plan-rsfmri-statistics",
                {
                    **request.design.model_dump(mode="json"),
                    "correction": (
                        None
                        if request.correction is None
                        else request.correction.model_dump(mode="json")
                    ),
                },
            )
            matrix = design_matrix(request.design)
            skill_locks, tool_locks = self._statistics_execution_locks(request.correction)
            plan_payload = {
                "kind": "statistical_design",
                "design": request.design.model_dump(mode="json"),
                "correction": (
                    request.correction.model_dump(mode="json")
                    if request.correction is not None
                    else None
                ),
                "design_matrix": [list(row) for row in matrix],
                "skill_locks": skill_locks,
                "tool_locks": tool_locks,
            }
            return matrix, plan_payload, environment.environment_hash

        def finalize(
            prepared: tuple[tuple[tuple[float, ...], ...], dict[str, Any], str],
        ) -> StatisticalDesignView:
            matrix, plan_payload, environment_hash = prepared
            lock = {
                "plan": plan_payload,
                "manifest_hash": request.input_manifest_hash,
                "environment_hash": environment_hash,
            }
            plan = self.repository.create_plan(
                project_id=request.project_id,
                expected_project_version=request.expected_project_version,
                plan=plan_payload,
                plan_hash=content_hash(lock),
                manifest_hash=request.input_manifest_hash,
                environment_hash=environment_hash,
                supersedes_id=request.supersedes_plan_revision_id,
            )
            self.repository.append_event(
                project_id=request.project_id,
                run_id=None,
                event_type="StatisticalDesignCreated",
                severity="info",
                payload={
                    "plan_revision_id": plan.plan_revision_id,
                    "plan_hash": plan.plan_hash,
                    "test": request.design.test.value,
                    "correction": (
                        request.correction.method if request.correction is not None else None
                    ),
                },
            )
            return StatisticalDesignView(
                design=request.design,
                correction=request.correction,
                design_matrix=matrix,
                plan_revision=plan,
            )

        return self._idempotent_prepared(
            scope=f"projects:{request.project_id}:statistical-designs:create",
            key=idempotency_key,
            request=request,
            response_type=StatisticalDesignView,
            prepare=prepare,
            finalize=finalize,
        )

    def prepare_statistical_design_proposal(
        self,
        *,
        run_id: str,
        draft: StatisticsDraft,
        covariate_values: Mapping[str, Mapping[str, float | None]] | None = None,
        label_values: Mapping[str, Mapping[str, str | None]] | None = None,
    ) -> tuple[StatisticalDesignProposal | None, tuple[DesignIssue, ...]]:
        """Assemble a reviewable design from a run's frozen QC decision.

        The conversation never supplies identifiers: this method reads the run's
        approval state, the QC inclusion order and the registered artifacts, then
        hands them to the pure assembler.  Everything the model chose is method
        and column names only.

        Categorical labels (grouping, conditions) and numeric covariates are
        separate arguments on purpose: a grouping column is a string label, while
        a covariate is a number, and conflating them would let a numeric column be
        used as a group code.
        """

        run = self.repository.get_run(run_id)
        if run.state.value != "succeeded":
            return None, (
                _design_issue(
                    "source_run_not_succeeded",
                    f"运行当前状态为 {run.state.value}。统计设计必须在已成功完成的运行上建立。",
                ),
            )

        binding, included, allowed_metric_ids, manifest_hash, issue = (
            self._statistics_source_binding(run_id)
        )
        if issue is not None:
            return None, (issue,)

        artifacts_by_id = {
            item.artifact_id: item for item in self.repository.list_artifacts(run_id)
        }
        refs: list[ArtifactRef] = []
        for artifact_id, artifact in artifacts_by_id.items():
            if allowed_metric_ids is not None and artifact_id not in allowed_metric_ids:
                continue
            refs.append(_artifact_ref(artifact))
        mask_refs = [
            _artifact_ref(artifact)
            for artifact in artifacts_by_id.values()
            if artifact.artifact_type == (draft.mask_artifact_type or _BRAIN_MASK_TYPE)
        ]

        sources, source_issues = collect_design_sources(
            artifacts=(*refs, *mask_refs),
            included_subject_ids=included,
            metric_artifact_type=draft.metric_artifact_type or "",
            mask_artifact_type=draft.mask_artifact_type or _BRAIN_MASK_TYPE,
            label_column=draft.grouping_column,
            label_values=(
                (label_values or {}).get(draft.grouping_column, {})
                if draft.grouping_column
                else None
            ),
        )
        if sources is None:
            return None, source_issues

        design, design_issues = assemble_statistical_design(
            choices=_choices_from_draft(draft),
            subjects=sources.subjects,
            covariate_values=covariate_values or {},
            mask_artifact_id=sources.mask_artifact_id,
            revision_id=f"conversation-{run_id}-{statistics_draft_hash(draft)[:12]}",
            qc=binding,
        )
        if design is None:
            return None, design_issues

        correction, correction_issues = build_correction(draft, design)
        if correction_issues:
            return None, correction_issues
        try:
            validate_correction_for_design(design, correction)
        except ValueError as exc:
            return None, (_design_issue("correction_rejected_by_domain", str(exc)),)

        project = self.repository.get_project(run.project_id)
        return (
            StatisticalDesignProposal(
                design=design,
                correction=correction,
                project_id=run.project_id,
                project_version=project.version,
                input_manifest_hash=manifest_hash,
                source_run_id=run_id,
                frozen_subject_order=design.subject_order,
                metric_artifact_ids=tuple(image.artifact_id for image in design.images),
                review=render_statistics_draft_review(draft),
                proposal_hash=proposal_hash(design, correction),
            ),
            (),
        )

    def _statistics_source_binding(
        self, run_id: str
    ) -> tuple[QcBinding, tuple[str, ...], frozenset[str] | None, str, DesignIssue | None]:
        """Resolve the QC binding, frozen inclusion order and manifest to use.

        An approved QC review is preferred, because its inclusion order and metric
        artifact list are the frozen scientific decision.  A recorded QC skip is
        accepted, but then there is no reviewed artifact list to inherit, so every
        registered metric artifact of the run is eligible.
        """

        review = self.repository.get_latest_qc_review_for_run(run_id)
        if review is not None and review.state.value == "approved":
            return (
                QcBinding(
                    source=StatisticalQcSource.APPROVED_REVIEW,
                    qc_review_revision_id=review.review.review_revision_id,
                    qc_review_hash=review.review.content_hash,
                ),
                tuple(review.review.included_subject_ids),
                frozenset(review.review.metric_artifact_ids),
                review.review.input_manifest_hash,
                None,
            )

        skip = self.repository.get_qc_skip_for_run(run_id)
        if skip is not None:
            run = self.repository.get_run(run_id)
            plan = self.repository.get_plan(run.plan_revision_id)
            approved = (
                self.repository.get_approved_qc_review(skip.qc_review_revision_id)
                if skip.qc_review_revision_id
                else None
            )
            included = tuple(approved.review.included_subject_ids) if approved is not None else ()
            if not included:
                return (
                    QcBinding(source=StatisticalQcSource.SKIPPED),
                    (),
                    None,
                    plan.manifest_hash,
                    _design_issue(
                        "qc_inclusion_order_unavailable",
                        "该运行的 QC 跳过记录没有可继承的受试者纳入顺序。"
                        "无法建立显式对齐的统计设计。",
                    ),
                )
            return (
                QcBinding(
                    source=StatisticalQcSource.SKIPPED,
                    qc_skip_id=skip.qc_skip_id,
                    qc_skip_hash=skip.decision_hash,
                    qc_source_run_id=run_id,
                ),
                included,
                None,
                plan.manifest_hash,
                None,
            )

        return (
            QcBinding(source=StatisticalQcSource.APPROVED_REVIEW),
            (),
            None,
            "",
            _design_issue(
                "statistics_source_unapproved",
                "该运行既无已批准的 QC review 也无 QC 跳过记录。统计设计必须建立在其中之一之上。",
            ),
        )

    def _validate_statistics_qc_source(
        self,
        request: StatisticalDesignCreate,
        image_artifact_ids: tuple[str, ...],
    ) -> str:
        design = request.design
        if design.qc_source is StatisticalQcSource.APPROVED_REVIEW:
            assert design.qc_review_revision_id is not None and design.qc_review_hash is not None
            qc_review = self.repository.get_approved_qc_review(design.qc_review_revision_id)
            if qc_review.project_id != request.project_id:
                raise ConflictError("cross_project_qc_review", "QC review 不属于当前项目。")
            if qc_review.review.content_hash != design.qc_review_hash:
                raise ConflictError(
                    "qc_review_hash_mismatch", "统计设计引用的 QC review hash 与已批准版本不一致。"
                )
            if qc_review.review.input_manifest_hash != request.input_manifest_hash:
                raise ConflictError(
                    "qc_manifest_hash_mismatch", "统计设计输入清单与已批准 QC review 不一致。"
                )
            try:
                assert_statistics_ready(qc_review.review, design.subject_order)
            except ValueError as exc:
                raise InputValidationError(
                    "statistics_qc_alignment_failed", "统计受试者顺序必须精确等于 QC 冻结纳入顺序。"
                ) from exc
            if not set(image_artifact_ids).issubset(qc_review.review.metric_artifact_ids):
                raise InputValidationError(
                    "statistics_artifact_not_qc_approved",
                    "统计影像必须全部来自 QC review 冻结的指标 Artifact 清单。",
                )
            return qc_review.run_id

        assert (
            design.qc_source_run_id is not None
            and design.qc_skip_id is not None
            and design.qc_skip_hash is not None
        )
        skip = self.repository.get_qc_skip_for_run(design.qc_source_run_id)
        if (
            skip is None
            or skip.qc_skip_id != design.qc_skip_id
            or skip.decision_hash != design.qc_skip_hash
        ):
            raise ConflictError(
                "qc_skip_binding_mismatch", "统计设计引用的 QC 跳过记录无效或已失配。"
            )
        source_run = self.repository.get_run(design.qc_source_run_id)
        if source_run.project_id != request.project_id or source_run.state.value != "succeeded":
            raise ConflictError(
                "qc_skip_source_run_invalid", "QC 跳过来源运行不属于当前项目或尚未成功完成。"
            )
        source_plan = self.repository.get_plan(source_run.plan_revision_id)
        if source_plan.manifest_hash != request.input_manifest_hash:
            raise ConflictError(
                "qc_skip_manifest_hash_mismatch", "QC 跳过来源运行的输入清单不匹配。"
            )
        return source_run.run_id

    def get_statistical_design(self, plan_revision_id: str) -> StatisticalDesignView:
        return self._statistics_view(self.repository.get_plan(plan_revision_id))

    def validate_statistical_design(
        self,
        plan_revision_id: str,
        request: StatisticalDesignValidationRequest,
        idempotency_key: str,
    ) -> StatisticalDesignView:
        def action() -> StatisticalDesignView:
            current = self.repository.get_plan(plan_revision_id)
            self._statistics_view(current)
            validated = self.repository.validate_plan(
                plan_revision_id,
                expected_version=request.expected_version,
                issues=[],
            )
            self.repository.append_event(
                project_id=validated.project_id,
                run_id=None,
                event_type="StatisticalDesignValidated",
                severity="info",
                payload={
                    "plan_revision_id": validated.plan_revision_id,
                    "plan_hash": validated.plan_hash,
                },
            )
            return self._statistics_view(validated)

        return self._idempotent(
            scope=f"statistical-designs:{plan_revision_id}:validate",
            key=idempotency_key,
            request=request,
            response_type=StatisticalDesignView,
            action=action,
        )

    @staticmethod
    def correction_capabilities() -> list[CorrectionCapabilityView]:
        return [
            CorrectionCapabilityView.model_validate(
                {
                    "method": "fdr",
                    "skill_id": "rsfmri.statistics.fdr",
                    "description": "FDR correction with an explicit q threshold and mask.",
                    "schema": FdrCorrection.model_json_schema(),
                }
            ),
            CorrectionCapabilityView.model_validate(
                {
                    "method": "grf",
                    "skill_id": "rsfmri.statistics.grf",
                    "description": (
                        "GRF correction with explicit voxel, cluster, tail, and mask settings."
                    ),
                    "schema": GrfCorrection.model_json_schema(),
                }
            ),
        ]

    def _plan_staleness_reasons(
        self,
        plan: PlanRevisionView,
        *,
        current_environment_hash: str,
        verify_source_files: bool = True,
        seen: set[str] | None = None,
    ) -> list[dict[str, str]]:
        kind = plan.plan.get("kind")
        if kind is None:
            # Legacy/internal mock plans are retained for lightweight executor tests.
            return []
        reasons: list[dict[str, str]] = []
        if kind not in {"skill_plan", "statistical_design"}:
            return [{"code": "unsupported_plan_kind", "message": str(kind)}]
        if plan.environment_hash != current_environment_hash:
            reasons.append(
                {
                    "code": "environment_lock_changed",
                    "message": "MATLAB/SPM/DPABI/adapter environment lock changed",
                }
            )
        if kind == "skill_plan":
            return self._skill_plan_staleness_reasons(plan, reasons, verify_source_files)
        return self._statistical_design_staleness_reasons(
            plan,
            reasons,
            current_environment_hash=current_environment_hash,
            verify_source_files=verify_source_files,
            seen=seen,
        )

    def _skill_plan_staleness_reasons(
        self,
        plan: PlanRevisionView,
        reasons: list[dict[str, str]],
        verify_source_files: bool,
    ) -> list[dict[str, str]]:
        try:
            skill_plan = SkillPlan.model_validate(plan.plan.get("skill_plan"))
        except ValidationError:
            return [
                *reasons,
                {"code": "skill_plan_invalid", "message": "stored SkillPlan is invalid"},
            ]
        if skill_plan.plan_hash != plan.plan_hash:
            reasons.append(
                {
                    "code": "skill_plan_hash_changed",
                    "message": "embedded SkillPlan hash differs from the approved revision",
                }
            )
        if (
            skill_plan.input_manifest_hash != plan.manifest_hash
            or skill_plan.environment.environment_hash != plan.environment_hash
        ):
            reasons.append(
                {
                    "code": "skill_plan_lock_mismatch",
                    "message": "stored SkillPlan locks differ from revision locks",
                }
            )
        reasons.extend(self._dataset_lock_staleness_reasons(plan, skill_plan, verify_source_files))
        reasons.extend(self._skill_and_tool_lock_staleness_reasons(skill_plan))
        return reasons

    def _dataset_lock_staleness_reasons(
        self,
        plan: PlanRevisionView,
        skill_plan: SkillPlan,
        verify_source_files: bool,
    ) -> list[dict[str, str]]:
        reasons: list[dict[str, str]] = []
        try:
            dataset = self.repository.get_dataset(skill_plan.dataset_ref)
            manifest_id = plan.plan.get("dataset_manifest_id")
            if dataset.project_id != plan.project_id:
                reasons.append(
                    {
                        "code": "dataset_project_changed",
                        "message": "dataset no longer belongs to the approved project",
                    }
                )
            if not isinstance(manifest_id, str) or dataset.current_manifest_id != manifest_id:
                reasons.append(
                    {
                        "code": "dataset_manifest_revision_changed",
                        "message": "dataset current manifest revision changed",
                    }
                )
            current_manifest = None
            if isinstance(manifest_id, str) and dataset.current_manifest_id == manifest_id:
                current_manifest = self.repository.get_manifest(manifest_id)
            if current_manifest is not None and current_manifest.content_hash != plan.manifest_hash:
                reasons.append(
                    {
                        "code": "dataset_manifest_hash_changed",
                        "message": "dataset current manifest hash changed",
                    }
                )
            if verify_source_files:
                project = self.repository.get_project(dataset.project_id)
                source = self.path_policy.validate_read_path(
                    dataset.source_path,
                    project_roots=project.source_roots,
                    expect_directory=True,
                )
                current_source_hash = content_hash(
                    self._inspect_source(
                        source,
                        report_only=False,
                        selected_input_stage=(
                            current_manifest.selected_input_stage if current_manifest else None
                        ),
                    )
                )
                if current_source_hash != plan.manifest_hash:
                    reasons.append(
                        {
                            "code": "source_manifest_content_changed",
                            "message": (
                                "dataset source files no longer match the approved manifest hash"
                            ),
                        }
                    )
        except (ApplicationError, OSError):
            reasons.append(
                {
                    "code": "dataset_lock_unavailable",
                    "message": "approved dataset or manifest lock is unavailable",
                }
            )
        return reasons

    def _skill_and_tool_lock_staleness_reasons(self, skill_plan: SkillPlan) -> list[dict[str, str]]:
        reasons: list[dict[str, str]] = []
        for lock in skill_plan.skill_locks:
            try:
                current = self.skill_registry.resolve(lock.skill_id, lock.version)
            except SkillRegistryError:
                reasons.append(
                    {
                        "code": "skill_lock_unavailable",
                        "message": f"{lock.skill_id}@{lock.version}",
                    }
                )
                continue
            if current.content_hash != lock.content_hash:
                reasons.append(
                    {
                        "code": "skill_lock_changed",
                        "message": f"{lock.skill_id}@{lock.version}",
                    }
                )
        for step in skill_plan.steps:
            try:
                current_tool = self.tool_registry.resolve_capability(step.tool.capability)
            except ToolRegistryError:
                reasons.append(
                    {
                        "code": "tool_lock_unavailable",
                        "message": step.tool.capability,
                    }
                )
                continue
            if current_tool != step.tool:
                reasons.append(
                    {
                        "code": "tool_lock_changed",
                        "message": step.tool.capability,
                    }
                )
        return reasons

    def _statistical_design_staleness_reasons(
        self,
        plan: PlanRevisionView,
        reasons: list[dict[str, str]],
        *,
        current_environment_hash: str,
        verify_source_files: bool,
        seen: set[str] | None,
    ) -> list[dict[str, str]]:
        _validate_statistical_locks(self, plan, reasons)
        try:
            design_view = self._statistics_view(plan)
            design = design_view.design
            if design.qc_source is StatisticalQcSource.APPROVED_REVIEW:
                assert (
                    design.qc_review_revision_id is not None and design.qc_review_hash is not None
                )
                qc_review = self.repository.get_approved_qc_review(design.qc_review_revision_id)
                if (
                    qc_review.project_id != plan.project_id
                    or qc_review.review.content_hash != design.qc_review_hash
                    or qc_review.review.input_manifest_hash != plan.manifest_hash
                ):
                    reasons.append(
                        {
                            "code": "qc_lock_changed",
                            "message": "approved QC review no longer matches design",
                        }
                    )
                assert_statistics_ready(qc_review.review, design.subject_order)
                source_run = self.repository.get_run(qc_review.run_id)
            else:
                assert design.qc_source_run_id and design.qc_skip_id and design.qc_skip_hash
                skip = self.repository.get_qc_skip_for_run(design.qc_source_run_id)
                if (
                    skip is None
                    or skip.qc_skip_id != design.qc_skip_id
                    or skip.decision_hash != design.qc_skip_hash
                ):
                    reasons.append(
                        {
                            "code": "qc_skip_lock_changed",
                            "message": "QC skip record no longer matches the statistical design",
                        }
                    )
                source_run = self.repository.get_run(design.qc_source_run_id)
            source_plan = self.repository.get_plan(source_run.plan_revision_id)
            if source_plan.manifest_hash != plan.manifest_hash:
                reasons.append(
                    {
                        "code": "qc_source_manifest_changed",
                        "message": "QC source plan manifest differs from statistical input",
                    }
                )
            visited = set() if seen is None else set(seen)
            if plan.plan_revision_id not in visited:
                visited.add(plan.plan_revision_id)
                reasons.extend(
                    self._plan_staleness_reasons(
                        source_plan,
                        current_environment_hash=current_environment_hash,
                        verify_source_files=verify_source_files,
                        seen=visited,
                    )
                )
        except (ApplicationError, ValidationError, ValueError):
            reasons.append(
                {
                    "code": "qc_lock_unavailable",
                    "message": "approved QC lineage is unavailable or invalid",
                }
            )
        return reasons

    def _require_plan_current(
        self, plan: PlanRevisionView, *, verify_source_files: bool = True
    ) -> None:
        """Fail closed on execution locks; optionally reuse the just-completed source scan."""

        current_environment_hash = self.environment_provider.current().snapshot.environment_hash
        reasons = self._plan_staleness_reasons(
            plan,
            current_environment_hash=current_environment_hash,
            verify_source_files=verify_source_files,
        )
        if reasons:
            raise ConflictError(
                "plan_stale",
                "计划的 manifest、环境、Skill、Tool 或 QC 锁已变化, 请重新生成并审批。",
                reasons=reasons,
            )
