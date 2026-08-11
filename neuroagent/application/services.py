"""Application use cases for projects, immutable plans, and local runs."""

from __future__ import annotations

import asyncio
import math
import random
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from neuroagent.agent.gateway import ModelGateway, ModelGatewayError
from neuroagent.agent.models import (
    AgentSummaryPurpose,
    AgentTaskRequest,
    GatewayResult,
    SafeAgentSummary,
    TaskType,
)
from neuroagent.agent.providers import ModelProvider, ProviderError
from neuroagent.agent.redaction import OutboundContextPolicy, OutboundPolicyError
from neuroagent.agent.router import ModelRouter, ModelRoutingError
from neuroagent.agent.secrets import SecretResolver
from neuroagent.analysis.clusters import localize_clusters
from neuroagent.analysis.diagnostics import diagnose_dpabi_log
from neuroagent.analysis.organization import build_dpabi_preview
from neuroagent.analysis.rag import answer_rsfmri_question, search_evidence
from neuroagent.analysis.roi import build_roi_tables, validate_roi_request
from neuroagent.analysis.tables import inspect_table
from neuroagent.analysis.templates import generate_ml_template
from neuroagent.application.contracts import (
    AgentTaskCreate,
    AgentTaskView,
    ApprovalCreate,
    ApprovalView,
    ArtifactView,
    ClusterLocalizationRequest,
    ClusterLocalizationView,
    CorrectionCapabilityView,
    DatasetCreate,
    DatasetKind,
    DatasetSplitCreate,
    DatasetSplitView,
    DatasetView,
    DemographicsImportRequest,
    DemographicsRevisionView,
    EnvironmentProbeView,
    HealthView,
    ManifestRevisionView,
    ManifestScanRequest,
    MlTableInspectRequest,
    MlTableInspectView,
    MlTemplateCreateRequest,
    MlTemplateView,
    ModelProfileInput,
    ModelProfileView,
    OrganizationPreviewRequest,
    OrganizationPreviewView,
    PlanRevisionCreate,
    PlanRevisionView,
    PlanValidationRequest,
    ProjectCreate,
    ProjectView,
    ProviderTestRequest,
    ProviderTestView,
    QcReviewApprove,
    QcReviewCreate,
    QcReviewView,
    RoiTableCreateRequest,
    RoiTableView,
    RsFmriAnswerView,
    RsFmriQuestionRequest,
    RunAction,
    RunCreate,
    RunDiagnosisRequest,
    RunDiagnosisView,
    RuntimeEventView,
    RunView,
    SkillPlanIntent,
    SkillPlanResolveRequest,
    SkillPlanResolveView,
    StatisticalDesignCreate,
    StatisticalDesignValidationRequest,
    StatisticalDesignView,
    StatisticalResultDetailView,
    StatisticalResultView,
    StatisticsRunCreate,
    ValidationIssue,
    WorkflowState,
)
from neuroagent.application.environment_lock import EnvironmentLockProvider
from neuroagent.application.errors import ApplicationError, ConflictError, InputValidationError
from neuroagent.application.hashing import content_hash
from neuroagent.application.ports import (
    DatabaseLifecyclePort,
    DatasetInspectorPort,
    DemographicsReaderPort,
    PathPolicyPort,
    RepositoryPort,
)
from neuroagent.application.reporting import build_statistical_reproducibility_report
from neuroagent.application.settings import Settings
from neuroagent.domain.fmri.artifacts import ArtifactKind, ArtifactLineage
from neuroagent.domain.fmri.preprocessing import NormalizationMode
from neuroagent.domain.fmri.qc import assert_statistics_ready
from neuroagent.domain.fmri.results import StatisticalResultManifest
from neuroagent.domain.fmri.skillpacks.builtin import build_builtin_registry
from neuroagent.domain.fmri.statistics import (
    CorrectionSpec,
    FdrCorrection,
    GrfCorrection,
    StatisticalDesignRevision,
    StatisticalTest,
    design_matrix,
    validate_correction_for_design,
)
from neuroagent.skills.compiler import SkillCompileError, SkillCompiler
from neuroagent.skills.models import SkillPlan, SkillRequest, SkillSpec
from neuroagent.skills.registry import SkillRegistry, SkillRegistryError
from neuroagent.skills.resolver import SkillResolver
from neuroagent.skills.validation import SkillValidator
from neuroagent.tools.registry import ToolRegistryError, build_default_tool_registry

T = TypeVar("T", bound=BaseModel)
U = TypeVar("U")


class NeuroAgentService:
    """A transport-neutral facade around transactional use cases."""

    def __init__(
        self,
        settings: Settings,
        database: DatabaseLifecyclePort,
        repository: RepositoryPort,
        *,
        path_policy: PathPolicyPort,
        dataset_inspector: DatasetInspectorPort,
        demographics_reader: DemographicsReaderPort,
        environment_provider: EnvironmentLockProvider,
        secret_resolver: SecretResolver,
        providers: Mapping[str, ModelProvider],
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.repository = repository
        self.path_policy = path_policy
        self.dataset_inspector = dataset_inspector
        self.demographics_reader = demographics_reader
        self.skill_registry = skill_registry or build_builtin_registry()
        self.skill_validator = SkillValidator()
        self.skill_resolver = SkillResolver(self.skill_registry)
        self.tool_registry = build_default_tool_registry()
        self.skill_compiler = SkillCompiler(self.tool_registry, self.skill_validator)
        self.environment_provider = environment_provider
        self.secret_resolver = secret_resolver
        self.providers = dict(providers)

    def close(self) -> None:
        self.database.dispose()

    def health(self) -> HealthView:
        self.database.ping()
        return HealthView()

    def environment_probe(self) -> EnvironmentProbeView:
        return self.environment_provider.current().probe

    def _idempotent(
        self,
        *,
        scope: str,
        key: str,
        request: BaseModel,
        response_type: type[T],
        action: Callable[[], T],
    ) -> T:
        request_hash = content_hash(request.model_dump(mode="json"))
        owner_token = str(uuid4())
        with self.repository.atomic():
            stored = self.repository.begin_idempotent_request(
                scope,
                key,
                request_hash,
                owner_token,
                self.settings.idempotency_lease_seconds,
            )
            if stored is not None:
                return response_type.model_validate(stored)
            response = action()
            self.repository.complete_idempotent_request(
                scope, key, request_hash, owner_token, response
            )
        return response

    def _idempotent_prepared(
        self,
        *,
        scope: str,
        key: str,
        request: BaseModel,
        response_type: type[T],
        prepare: Callable[[], U],
        finalize: Callable[[U], T],
    ) -> T:
        """Run slow/read-only preparation outside SQLite's write transaction."""

        request_hash = content_hash(request.model_dump(mode="json"))
        owner_token = str(uuid4())
        stored = self.repository.begin_idempotent_request(
            scope,
            key,
            request_hash,
            owner_token,
            self.settings.idempotency_lease_seconds,
        )
        if stored is not None:
            return response_type.model_validate(stored)
        try:
            prepared = prepare()
            with self.repository.atomic():
                response = finalize(prepared)
                self.repository.complete_idempotent_request(
                    scope, key, request_hash, owner_token, response
                )
            return response
        except BaseException:
            # The durable reservation is a short transaction. A handled
            # preparation/finalization failure releases it so the exact same
            # request can safely retry; business writes and response storage
            # are committed (or rolled back) together above.
            self.repository.release_idempotent_request(scope, key, request_hash, owner_token)
            raise

    async def _idempotent_async(
        self,
        *,
        scope: str,
        key: str,
        request: BaseModel,
        response_type: type[T],
        prepare: Callable[[], Awaitable[U]],
        finalize: Callable[[U], T],
    ) -> T:
        request_hash = content_hash(request.model_dump(mode="json"))
        owner_token = str(uuid4())
        stored = self.repository.begin_idempotent_request(
            scope,
            key,
            request_hash,
            owner_token,
            self.settings.idempotency_lease_seconds,
        )
        if stored is not None:
            return response_type.model_validate(stored)
        operation_task = asyncio.create_task(
            self._complete_idempotent_async_operation(
                scope=scope,
                key=key,
                request_hash=request_hash,
                owner_token=owner_token,
                prepare=prepare,
                finalize=finalize,
            )
        )
        try:
            return await asyncio.shield(operation_task)
        except asyncio.CancelledError:
            # A disconnected HTTP caller must not cancel an already-dispatched
            # billable Provider request and then make the same key retryable.
            # Keep the supervised operation alive until its response or error
            # has been durably resolved, then preserve caller cancellation.
            while not operation_task.done():
                try:
                    await asyncio.shield(operation_task)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if operation_task.done() and not operation_task.cancelled():
                operation_task.exception()
            raise

    async def _complete_idempotent_async_operation(
        self,
        *,
        scope: str,
        key: str,
        request_hash: str,
        owner_token: str,
        prepare: Callable[[], Awaitable[U]],
        finalize: Callable[[U], T],
    ) -> T:
        stop_heartbeat = asyncio.Event()
        prepare_task = asyncio.ensure_future(prepare())
        heartbeat_task = asyncio.create_task(
            self._renew_idempotency_lease(
                scope=scope,
                key=key,
                request_hash=request_hash,
                owner_token=owner_token,
                stop=stop_heartbeat,
            )
        )
        try:
            done, _pending = await asyncio.wait(
                (prepare_task, heartbeat_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                await heartbeat_task
                raise ConflictError(
                    "idempotency_lease_lost",
                    "幂等请求处理所有权已丢失, 已停止外部调用。",
                    scope=scope,
                )
            prepared = prepare_task.result()
            stop_heartbeat.set()
            await heartbeat_task
            with self.repository.atomic():
                if not self.repository.renew_idempotent_request(
                    scope,
                    key,
                    request_hash,
                    owner_token,
                    self.settings.idempotency_lease_seconds,
                ):
                    raise ConflictError(
                        "idempotency_lease_lost",
                        "幂等请求处理所有权已丢失, 未保存外部调用结果。",
                        scope=scope,
                    )
                response = finalize(prepared)
                self.repository.complete_idempotent_request(
                    scope, key, request_hash, owner_token, response
                )
            return response
        except asyncio.CancelledError:
            stop_heartbeat.set()
            for task in (prepare_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(prepare_task, heartbeat_task, return_exceptions=True)
            # Cancellation here means service shutdown or explicit lease-loss
            # handling, not an HTTP disconnect (the outer task is shielded).
            # Preserve the reservation because the remote acceptance state may
            # be indeterminate.
            raise
        except BaseException:
            stop_heartbeat.set()
            for task in (prepare_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(prepare_task, heartbeat_task, return_exceptions=True)
            # The reservation is deliberately committed before awaiting a
            # provider so SQLite writers are never blocked by network latency.
            # A handled failure releases it, allowing an explicit retry with
            # the same request and key.
            self.repository.release_idempotent_request(scope, key, request_hash, owner_token)
            raise

    async def _renew_idempotency_lease(
        self,
        *,
        scope: str,
        key: str,
        request_hash: str,
        owner_token: str,
        stop: asyncio.Event,
    ) -> None:
        lease_seconds = self.settings.idempotency_lease_seconds
        interval_seconds = min(max(lease_seconds / 3, 0.1), 30.0)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
                return
            except TimeoutError:
                renewed = await asyncio.to_thread(
                    self.repository.renew_idempotent_request,
                    scope,
                    key,
                    request_hash,
                    owner_token,
                    lease_seconds,
                )
                if not renewed:
                    raise ConflictError(
                        "idempotency_lease_lost",
                        "幂等请求处理所有权已丢失, 已停止外部调用。",
                        scope=scope,
                    ) from None

    def _require_project_version(self, project_id: str, expected_version: int) -> ProjectView:
        project = self.repository.get_project(project_id)
        if project.version != expected_version:
            raise ConflictError(
                "revision_conflict",
                "项目版本已变化, 请刷新后重试。",
                expected=expected_version,
                actual=project.version,
            )
        return project

    # -- project / dataset ---------------------------------------------------
    def create_project(self, request: ProjectCreate, idempotency_key: str) -> ProjectView:
        def prepare() -> tuple[list[str], str]:
            roots = [
                str(self.path_policy.validate_project_source_root(path))
                for path in request.source_roots
            ]
            work_root = str(self.path_policy.validate_work_root(request.work_root))
            return roots, work_root

        def finalize(prepared: tuple[list[str], str]) -> ProjectView:
            roots, work_root = prepared
            result = self.repository.create_project(request.name, roots, work_root)
            self.repository.append_event(
                project_id=result.project_id,
                run_id=None,
                event_type="ProjectCreated",
                severity="info",
                payload={"version": result.version, "source_root_count": len(roots)},
            )
            return result

        return self._idempotent_prepared(
            scope="projects:create",
            key=idempotency_key,
            request=request,
            response_type=ProjectView,
            prepare=prepare,
            finalize=finalize,
        )

    def list_projects(self) -> list[ProjectView]:
        return self.repository.list_projects()

    def get_project(self, project_id: str) -> ProjectView:
        return self.repository.get_project(project_id)

    def create_dataset(
        self, project_id: str, request: DatasetCreate, idempotency_key: str
    ) -> DatasetView:
        def prepare() -> str:
            project = self.repository.get_project(project_id)
            source = self.path_policy.validate_read_path(
                request.source_path,
                project_roots=project.source_roots,
                expect_directory=True,
            )
            return str(source)

        def finalize(source_path: str) -> DatasetView:
            result = self.repository.create_dataset(
                project_id,
                name=request.name,
                source_path=source_path,
                expected_project_version=request.expected_project_version,
            )
            self.repository.append_event(
                project_id=project_id,
                run_id=None,
                event_type="DatasetRegistered",
                severity="info",
                payload={"dataset_id": result.dataset_id, "version": result.version},
            )
            return result

        return self._idempotent_prepared(
            scope=f"projects:{project_id}:datasets:create",
            key=idempotency_key,
            request=request,
            response_type=DatasetView,
            prepare=prepare,
            finalize=finalize,
        )

    def get_dataset(self, dataset_id: str) -> DatasetView:
        return self.repository.get_dataset(dataset_id)

    def inspect_dataset(
        self, dataset_id: str, request: ManifestScanRequest, idempotency_key: str
    ) -> ManifestRevisionView:
        def prepare() -> tuple[str, dict[str, Any]]:
            dataset = self.repository.get_dataset(dataset_id)
            project = self.repository.get_project(dataset.project_id)
            source = self.path_policy.validate_read_path(
                dataset.source_path,
                project_roots=project.source_roots,
                expect_directory=True,
            )
            content = self.dataset_inspector.inspect(source)
            return dataset.project_id, content

        def finalize(prepared: tuple[str, dict[str, Any]]) -> ManifestRevisionView:
            project_id, content = prepared
            result = self.repository.create_manifest(
                dataset_id,
                expected_version=request.expected_dataset_version,
                content=content,
            )
            self.repository.append_event(
                project_id=project_id,
                run_id=None,
                event_type="DatasetInspected",
                severity="warning" if result.profile.warnings else "info",
                payload={
                    "dataset_id": dataset_id,
                    "manifest_id": result.manifest_id,
                    "manifest_hash": result.content_hash,
                    "subject_count": result.profile.subject_count,
                    "warnings": result.profile.warnings,
                },
            )
            return result

        return self._idempotent_prepared(
            scope=f"datasets:{dataset_id}:inspect",
            key=idempotency_key,
            request=request,
            response_type=ManifestRevisionView,
            prepare=prepare,
            finalize=finalize,
        )

    def get_manifest(self, manifest_id: str) -> ManifestRevisionView:
        return self.repository.get_manifest(manifest_id)

    def import_demographics(
        self,
        dataset_id: str,
        request: DemographicsImportRequest,
        idempotency_key: str,
    ) -> DemographicsRevisionView:
        def prepare() -> tuple[str, dict[str, Any]]:
            dataset = self.repository.get_dataset(dataset_id)
            if dataset.current_manifest_id is None:
                raise ConflictError(
                    "manifest_required", "导入人口学信息前必须冻结一个受试者清单版本。"
                )
            project = self.repository.get_project(dataset.project_id)
            source = self.path_policy.validate_read_path(
                request.source_path,
                project_roots=project.source_roots,
                expect_directory=False,
            )
            manifest = self.repository.get_manifest(dataset.current_manifest_id)
            manifest_subject_ids = {entry.subject_id for entry in manifest.subjects}
            content = self.demographics_reader(
                source,
                subject_id_column=request.subject_id_column,
                column_mapping=request.column_mapping,
                encoding=request.encoding,
                manifest_subject_ids=manifest_subject_ids,
            )
            return dataset.project_id, content

        def finalize(prepared: tuple[str, dict[str, Any]]) -> DemographicsRevisionView:
            project_id, content = prepared
            result = self.repository.create_demographics(
                dataset_id,
                expected_version=request.expected_dataset_version,
                content=content,
            )
            self.repository.append_event(
                project_id=project_id,
                run_id=None,
                event_type="DemographicsAligned",
                severity=(
                    "warning" if result.missing_subject_ids or result.extra_subject_ids else "info"
                ),
                payload={
                    "dataset_id": dataset_id,
                    "demographics_id": result.demographics_id,
                    "row_count": result.row_count,
                    "missing_count": len(result.missing_subject_ids),
                    "extra_count": len(result.extra_subject_ids),
                },
            )
            return result

        return self._idempotent_prepared(
            scope=f"datasets:{dataset_id}:demographics:import",
            key=idempotency_key,
            request=request,
            response_type=DemographicsRevisionView,
            prepare=prepare,
            finalize=finalize,
        )

    def get_demographics(self, demographics_id: str) -> DemographicsRevisionView:
        return self.repository.get_demographics(demographics_id)

    def create_split(
        self, dataset_id: str, request: DatasetSplitCreate, idempotency_key: str
    ) -> DatasetSplitView:
        def prepare() -> tuple[str, str, dict[str, Any]]:
            dataset = self.repository.get_dataset(dataset_id)
            if dataset.current_manifest_id is None:
                raise ConflictError("manifest_required", "数据集划分前必须冻结受试者清单。")
            manifest = self.repository.get_manifest(dataset.current_manifest_id)
            subject_ids = sorted({entry.subject_id for entry in manifest.subjects})
            if not subject_ids:
                raise InputValidationError("manifest_empty", "受试者清单为空, 无法划分数据集。")
            strata: dict[str, list[str]] = {"__all__": subject_ids}
            if request.stratify_by:
                assert request.demographics_revision_id is not None
                demographics_dataset_id, demographics = self.repository.get_demographics_content(
                    request.demographics_revision_id
                )
                if demographics_dataset_id != dataset_id:
                    raise ConflictError(
                        "cross_dataset_demographics", "人口学版本不属于当前数据集。"
                    )
                by_subject = {row["subject_id"]: row for row in demographics["rows"]}
                missing = [
                    subject_id
                    for subject_id in subject_ids
                    if subject_id not in by_subject
                    or by_subject[subject_id].get(request.stratify_by) in {None, ""}
                ]
                if missing:
                    raise InputValidationError(
                        "stratification_values_missing",
                        "部分受试者缺少分层字段, 无法安全划分。",
                        missing_subject_ids=missing,
                    )
                grouped: dict[str, list[str]] = defaultdict(list)
                for subject_id in subject_ids:
                    grouped[str(by_subject[subject_id][request.stratify_by])].append(subject_id)
                strata = dict(grouped)

            train: list[str] = []
            validation: list[str] = []
            test: list[str] = []
            for stratum, members in sorted(strata.items()):
                rng = random.Random(f"{request.seed}:{stratum}")
                shuffled = sorted(members)
                rng.shuffle(shuffled)
                counts = self._allocation_counts(
                    len(shuffled),
                    [request.train_ratio, request.validation_ratio, request.test_ratio],
                )
                train.extend(shuffled[: counts[0]])
                validation.extend(shuffled[counts[0] : counts[0] + counts[1]])
                test.extend(shuffled[counts[0] + counts[1] :])
            train.sort()
            validation.sort()
            test.sort()
            assigned = train + validation + test
            if len(assigned) != len(set(assigned)) or set(assigned) != set(subject_ids):
                raise ConflictError(
                    "subject_split_leakage", "数据集划分出现受试者重复或遗漏, 已阻止保存。"
                )
            content = {
                "seed": request.seed,
                "stratify_by": request.stratify_by,
                "ratios": {
                    "train": request.train_ratio,
                    "validation": request.validation_ratio,
                    "test": request.test_ratio,
                },
                "manifest_hash": manifest.content_hash,
                "train_subject_ids": train,
                "validation_subject_ids": validation,
                "test_subject_ids": test,
            }
            return dataset.project_id, manifest.content_hash, content

        def finalize(prepared: tuple[str, str, dict[str, Any]]) -> DatasetSplitView:
            project_id, manifest_hash, content = prepared
            result = self.repository.create_split(
                dataset_id,
                expected_version=request.expected_dataset_version,
                content=content,
            )
            self.repository.append_event(
                project_id=project_id,
                run_id=None,
                event_type="DatasetSplitCreated",
                severity="info",
                payload={
                    "dataset_id": dataset_id,
                    "split_id": result.split_id,
                    "manifest_hash": manifest_hash,
                    "counts": [
                        len(result.train_subject_ids),
                        len(result.validation_subject_ids),
                        len(result.test_subject_ids),
                    ],
                },
            )
            return result

        return self._idempotent_prepared(
            scope=f"datasets:{dataset_id}:splits:create",
            key=idempotency_key,
            request=request,
            response_type=DatasetSplitView,
            prepare=prepare,
            finalize=finalize,
        )

    def get_split(self, split_id: str) -> DatasetSplitView:
        return self.repository.get_split(split_id)

    @staticmethod
    def _allocation_counts(size: int, ratios: list[float]) -> list[int]:
        raw = [size * ratio for ratio in ratios]
        counts = [math.floor(value) for value in raw]
        remainder = size - sum(counts)
        order = sorted(range(len(ratios)), key=lambda index: (-(raw[index] - counts[index]), index))
        for index in order[:remainder]:
            counts[index] += 1
        return counts

    # -- plans and approvals -------------------------------------------------
    def create_plan(self, request: PlanRevisionCreate, idempotency_key: str) -> PlanRevisionView:
        def action() -> PlanRevisionView:
            lock = {
                "plan": request.plan,
                "manifest_hash": request.manifest_hash,
                "environment_hash": request.environment_hash,
            }
            result = self.repository.create_plan(
                project_id=request.project_id,
                expected_project_version=request.expected_project_version,
                plan=request.plan,
                plan_hash=content_hash(lock),
                manifest_hash=request.manifest_hash,
                environment_hash=request.environment_hash,
                supersedes_id=request.supersedes_plan_revision_id,
            )
            self.repository.append_event(
                project_id=result.project_id,
                run_id=None,
                event_type="PlanRevisionCreated",
                severity="info",
                payload={
                    "plan_revision_id": result.plan_revision_id,
                    "plan_hash": result.plan_hash,
                    "revision": result.revision,
                },
            )
            return result

        return self._idempotent(
            scope=f"projects:{request.project_id}:plans:create",
            key=idempotency_key,
            request=request,
            response_type=PlanRevisionView,
            action=action,
        )

    def get_plan(self, plan_revision_id: str) -> PlanRevisionView:
        return self.repository.get_plan(plan_revision_id)

    def validate_plan(
        self,
        plan_revision_id: str,
        request: PlanValidationRequest,
        idempotency_key: str,
    ) -> PlanRevisionView:
        def action() -> PlanRevisionView:
            result = self.repository.validate_plan(
                plan_revision_id,
                expected_version=request.expected_version,
                issues=request.issues,
            )
            self.repository.append_event(
                project_id=result.project_id,
                run_id=None,
                event_type="PlanValidated",
                severity=(
                    "error"
                    if any(issue.severity == "blocking" for issue in request.issues)
                    else "info"
                ),
                payload={
                    "plan_revision_id": plan_revision_id,
                    "plan_hash": result.plan_hash,
                    "state": result.state.value,
                    "issue_count": len(request.issues),
                },
            )
            return result

        return self._idempotent(
            scope=f"plans:{plan_revision_id}:validate",
            key=idempotency_key,
            request=request,
            response_type=PlanRevisionView,
            action=action,
        )

    def approve_plan(
        self, plan_revision_id: str, request: ApprovalCreate, idempotency_key: str
    ) -> ApprovalView:
        def action() -> ApprovalView:
            approval, plan = self.repository.record_approval(plan_revision_id, request)
            self.repository.append_event(
                project_id=plan.project_id,
                run_id=None,
                event_type="PlanApprovalRecorded",
                severity="info" if request.decision.value == "approved" else "warning",
                payload={
                    "plan_revision_id": plan_revision_id,
                    "approval_id": approval.approval_id,
                    "decision": approval.decision.value,
                    "plan_hash": approval.plan_hash,
                },
            )
            return approval

        return self._idempotent(
            scope=f"plans:{plan_revision_id}:approve",
            key=idempotency_key,
            request=request,
            response_type=ApprovalView,
            action=action,
        )

    # -- reviewed Skills ----------------------------------------------------
    def list_skills(self) -> list[SkillSpec]:
        return list(self.skill_registry.list())

    @staticmethod
    def _validate_manifest_for_skill_intent(
        intent: SkillPlanIntent,
        manifest: ManifestRevisionView,
    ) -> None:
        if manifest.profile.kind is DatasetKind.DICOM:
            raise InputValidationError(
                "manifest_dicom_inventory_only",
                "DICOM 清单当前仅用于只读 inventory; "
                "完成显式序列角色映射和受控转换后才能创建预处理或指标计划。",
            )
        if manifest.profile.kind is DatasetKind.DPABI_READY:
            supported_stages = {"funraw", "funimg"}
            functional_stage_roots = sorted(
                {
                    relative_path.split("/", maxsplit=1)[0].lower()
                    for entry in manifest.subjects
                    for relative_path in entry.functional_files
                }
            )
            if (
                len(functional_stage_roots) != 1
                or functional_stage_roots[0] not in supported_stages
            ):
                raise InputValidationError(
                    "manifest_dpabi_input_stage_invalid",
                    "DPABI-ready manifest 必须只绑定一个受支持的功能输入 stage, "
                    "不能混合中间 checkpoint。",
                    functional_stage_roots=functional_stage_roots,
                    supported_input_stage_roots=sorted(supported_stages),
                )
            expected_anatomical_stage = {
                "funraw": "t1raw",
                "funimg": "t1img",
            }[functional_stage_roots[0]]
            invalid_anatomical_paths = sorted(
                relative_path
                for entry in manifest.subjects
                for relative_path in entry.anatomical_files
                if relative_path.split("/", maxsplit=1)[0].lower() != expected_anatomical_stage
            )
            if invalid_anatomical_paths:
                raise InputValidationError(
                    "manifest_dpabi_anatomical_stage_mixed",
                    "DPABI-ready manifest 的结构像不能来自未配对的 T1 checkpoint。",
                    expected_anatomical_stage=expected_anatomical_stage,
                    invalid_relative_paths=invalid_anatomical_paths,
                )
        missing_functional = [
            {
                "subject_id": entry.subject_id,
                "session_id": entry.session_id,
            }
            for entry in manifest.subjects
            if not entry.functional_files
        ]
        if not manifest.subjects or missing_functional:
            raise InputValidationError(
                "manifest_functional_input_missing",
                "冻结 manifest 中每个受试者/会话都必须包含明确识别的功能 BOLD 输入; "
                "DICOM inventory 不能替代角色映射。",
                missing=missing_functional,
            )

        if manifest.profile.kind in {DatasetKind.BIDS, DatasetKind.NIFTI, DatasetKind.MIXED}:
            ambiguous_functional = [
                {
                    "subject_id": entry.subject_id,
                    "session_id": entry.session_id,
                    "candidate_relative_paths": list(entry.functional_files),
                }
                for entry in manifest.subjects
                if len(entry.functional_files) > 1
            ]
            if ambiguous_functional:
                raise InputValidationError(
                    "manifest_functional_input_ambiguous",
                    "受试者/会话存在多个功能候选, 当前计划未提供显式 run/文件选择, "
                    "禁止按文件排序隐式选取。",
                    ambiguous=ambiguous_functional,
                )

        preprocessing = intent.preprocessing
        if preprocessing is None or preprocessing.normalization.mode not in {
            NormalizationMode.T1_SEGMENT,
            NormalizationMode.DARTEL,
        }:
            return
        missing_anatomical = [
            {
                "subject_id": entry.subject_id,
                "session_id": entry.session_id,
                "candidate_relative_paths": list(entry.anatomical_files),
            }
            for entry in manifest.subjects
            if not entry.anatomical_files
        ]
        if missing_anatomical:
            raise InputValidationError(
                "manifest_anatomical_input_missing",
                "T1 分割或 DARTEL 要求每个受试者/会话都有明确识别的结构像。",
                missing=missing_anatomical,
            )
        ambiguous_anatomical = [
            {
                "subject_id": entry.subject_id,
                "session_id": entry.session_id,
                "candidate_relative_paths": list(entry.anatomical_files),
            }
            for entry in manifest.subjects
            if len(entry.anatomical_files) > 1
        ]
        if ambiguous_anatomical:
            raise InputValidationError(
                "manifest_anatomical_input_ambiguous",
                "T1 分割或 DARTEL 要求每个受试者/会话精确匹配一份结构像。",
                ambiguous=ambiguous_anatomical,
            )
        expected_structural_ref = f"manifest:{manifest.manifest_id}:anatomical-source"
        if preprocessing.normalization.structural_artifact_id != expected_structural_ref:
            raise InputValidationError(
                "structural_artifact_binding_invalid",
                "结构像引用必须由服务端冻结 manifest 绑定, 不能使用任意客户端 Artifact ID。",
                expected_structural_artifact_id=expected_structural_ref,
            )

    def _materialize_skill_request(
        self,
        intent: SkillPlanIntent,
        manifest: ManifestRevisionView,
    ) -> SkillRequest:
        if intent.request_preprocessing:
            source_ref = f"manifest:{manifest.manifest_id}:functional-source"
            input_artifact = ArtifactLineage(
                artifact_id=source_ref,
                kind=ArtifactKind.FUNCTIONAL_TIMESERIES,
                metadata_verified=False,
                subject_manifest_hash=manifest.content_hash,
                space="source-uninspected",
                grid_signature=f"manifest-{manifest.content_hash[:16]}",
                voxel_size_mm=(1.0, 1.0, 1.0),
                mask_artifact_id=None,
                mask_grid_signature=None,
                temporally_filtered=False,
                frequency_band=None,
                spatially_smoothed=False,
                smoothing_fwhm_mm=None,
                scrubbed=False,
                producer_step_hash=content_hash(
                    {
                        "kind": "frozen_manifest_functional_source",
                        "manifest_id": manifest.manifest_id,
                        "manifest_hash": manifest.content_hash,
                    }
                ),
            )
            base_cfg_artifact_id = f"builtin:dpabi-v82-base-cfg:{self.settings.adapter_version}"
        else:
            assert intent.input_artifact_id is not None
            artifact = self.repository.get_artifact(intent.input_artifact_id)
            if artifact.project_id != intent.project_id:
                raise ConflictError(
                    "cross_project_artifact",
                    "Skill 输入 Artifact 不属于当前项目。",
                )
            lineage_data = artifact.provenance.get("lineage")
            if not isinstance(lineage_data, dict):
                raise InputValidationError(
                    "artifact_lineage_missing",
                    "指标规划只能使用带有服务端登记类型化 lineage 的 Artifact。",
                    artifact_id=artifact.artifact_id,
                )
            try:
                input_artifact = ArtifactLineage.model_validate(lineage_data)
            except ValidationError as exc:
                raise InputValidationError(
                    "artifact_lineage_invalid",
                    "输入 Artifact 的登记 lineage 无效。",
                    artifact_id=artifact.artifact_id,
                ) from exc
            if input_artifact.artifact_id != artifact.artifact_id:
                raise InputValidationError(
                    "artifact_lineage_identity_mismatch",
                    "输入 Artifact ID 与登记 lineage 不一致。",
                )
            if input_artifact.subject_manifest_hash != manifest.content_hash:
                raise ConflictError(
                    "artifact_manifest_mismatch",
                    "输入 Artifact lineage 不属于当前冻结 manifest。",
                )
            source_run = self.repository.get_run(artifact.run_id)
            source_plan = self.repository.get_plan(source_run.plan_revision_id)
            if source_plan.manifest_hash != manifest.content_hash:
                raise ConflictError(
                    "artifact_plan_manifest_mismatch",
                    "输入 Artifact 的来源计划未绑定当前冻结 manifest。",
                )
            base_cfg_artifact_id = None

        return SkillRequest(
            project_id=intent.project_id,
            dataset_ref=intent.dataset_ref,
            input_manifest_hash=manifest.content_hash,
            requested_metrics=intent.requested_metrics,
            primary_outputs=intent.primary_outputs,
            input_artifact=input_artifact,
            alff_falff=intent.alff_falff,
            reho=intent.reho,
            study_protocol_ref=intent.study_protocol_ref,
            request_preprocessing=intent.request_preprocessing,
            preprocessing=intent.preprocessing,
            base_cfg_artifact_id=base_cfg_artifact_id,
        )

    def resolve_skill_plan(
        self, request: SkillPlanResolveRequest, idempotency_key: str
    ) -> SkillPlanResolveView:
        def prepare() -> tuple[str, SkillPlan, list[ValidationIssue]]:
            self._require_project_version(
                request.request.project_id, request.expected_project_version
            )
            dataset = self.repository.get_dataset(request.request.dataset_ref)
            if dataset.project_id != request.request.project_id:
                raise ConflictError(
                    "cross_project_dataset",
                    "Skill 请求引用的数据集不属于当前项目。",
                )
            if dataset.current_manifest_id is None:
                raise ConflictError(
                    "dataset_manifest_missing",
                    "Skill 请求引用的数据集尚未生成冻结 manifest。",
                )
            manifest = self.repository.get_manifest(dataset.current_manifest_id)
            if manifest.content_hash != request.request.input_manifest_hash:
                raise ConflictError(
                    "manifest_hash_mismatch",
                    "Skill 请求的输入清单哈希不是数据集当前 manifest。",
                    expected=manifest.content_hash,
                    received=request.request.input_manifest_hash,
                )
            self._validate_manifest_for_skill_intent(request.request, manifest)
            skill_request = self._materialize_skill_request(request.request, manifest)
            environment = self.environment_provider.current().snapshot
            resolution = self.skill_resolver.resolve(skill_request, environment)
            report = self.skill_validator.validate_resolution(resolution)
            if report.has_blockers:
                raise InputValidationError(
                    "skill_plan_blocked",
                    "Skill 方案存在阻断问题, 不能编译或进入审批。",
                    issues=[issue.model_dump(mode="json") for issue in report.issues],
                )
            try:
                skill_plan = self.skill_compiler.compile(resolution)
            except SkillCompileError as exc:
                raise InputValidationError(
                    "skill_compile_failed", "Skill 方案编译失败。", reason=str(exc)
                ) from exc
            warnings = [
                ValidationIssue(
                    code=issue.code,
                    message=issue.message,
                    severity=issue.severity.value,
                    path=issue.path,
                )
                for issue in report.issues
            ]
            return manifest.manifest_id, skill_plan, warnings

        def finalize(
            prepared: tuple[str, SkillPlan, list[ValidationIssue]],
        ) -> SkillPlanResolveView:
            manifest_id, skill_plan, warnings = prepared
            dataset = self.repository.get_dataset(request.request.dataset_ref)
            if dataset.current_manifest_id != manifest_id:
                raise ConflictError(
                    "dataset_manifest_changed_during_prepare",
                    "Skill 方案准备期间数据集 manifest 已变化, 请重新生成方案。",
                )
            manifest = self.repository.get_manifest(manifest_id)
            if manifest.content_hash != skill_plan.input_manifest_hash:
                raise ConflictError(
                    "manifest_hash_changed_during_prepare",
                    "Skill 方案准备期间冻结 manifest 已失配。",
                )
            stored = self.repository.create_plan(
                project_id=skill_plan.project_id,
                expected_project_version=request.expected_project_version,
                plan={
                    "kind": "skill_plan",
                    "dataset_manifest_id": manifest_id,
                    "skill_plan": skill_plan.model_dump(mode="json"),
                },
                plan_hash=skill_plan.plan_hash,
                manifest_hash=skill_plan.input_manifest_hash,
                environment_hash=skill_plan.environment.environment_hash,
                supersedes_id=request.supersedes_plan_revision_id,
            )
            stored = self.repository.validate_plan(
                stored.plan_revision_id,
                expected_version=stored.version,
                issues=warnings,
            )
            self.repository.append_event(
                project_id=stored.project_id,
                run_id=None,
                event_type="SkillPlanCompiled",
                severity="warning" if warnings else "info",
                payload={
                    "plan_revision_id": stored.plan_revision_id,
                    "plan_hash": stored.plan_hash,
                    "skill_ids": [lock.skill_id for lock in skill_plan.skill_locks],
                    "warning_count": len(warnings),
                },
            )
            return SkillPlanResolveView(skill_plan=skill_plan, plan_revision=stored)

        return self._idempotent_prepared(
            scope=f"projects:{request.request.project_id}:skill-plans:resolve",
            key=idempotency_key,
            request=request,
            response_type=SkillPlanResolveView,
            prepare=prepare,
            finalize=finalize,
        )

    # -- statistical designs ------------------------------------------------
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
        NeuroAgentService._validate_correction(design, correction)
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
            qc_review = self.repository.get_approved_qc_review(request.design.qc_review_revision_id)
            if qc_review.project_id != request.project_id:
                raise ConflictError(
                    "cross_project_qc_review",
                    "QC review 不属于当前项目。",
                )
            if qc_review.review.content_hash != request.design.qc_review_hash:
                raise ConflictError(
                    "qc_review_hash_mismatch",
                    "统计设计引用的 QC review hash 与已批准版本不一致。",
                    expected=qc_review.review.content_hash,
                    received=request.design.qc_review_hash,
                )
            if qc_review.review.input_manifest_hash != request.input_manifest_hash:
                raise ConflictError(
                    "qc_manifest_hash_mismatch",
                    "统计设计输入清单与已批准 QC review 不一致。",
                )
            try:
                assert_statistics_ready(qc_review.review, request.design.subject_order)
            except ValueError as exc:
                raise InputValidationError(
                    "statistics_qc_alignment_failed",
                    "统计受试者顺序必须精确等于 QC 冻结纳入顺序。",
                ) from exc
            image_artifact_ids = tuple(image.artifact_id for image in request.design.images)
            if not set(image_artifact_ids).issubset(qc_review.review.metric_artifact_ids):
                raise InputValidationError(
                    "statistics_artifact_not_qc_approved",
                    "统计影像必须全部来自 QC review 冻结的指标 Artifact 清单。",
                )
            self.repository.assert_artifacts_belong_to_run(
                (*image_artifact_ids, request.design.mask_artifact_id),
                qc_review.run_id,
            )
            self._validate_statistical_artifact_lineage(
                request.design,
                manifest_hash=request.input_manifest_hash,
                source_run_id=qc_review.run_id,
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
                elif self.repository.get_manifest(manifest_id).content_hash != plan.manifest_hash:
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
                    current_source_hash = content_hash(self.dataset_inspector.inspect(source))
                    if current_source_hash != plan.manifest_hash:
                        reasons.append(
                            {
                                "code": "source_manifest_content_changed",
                                "message": (
                                    "dataset source files no longer match the approved "
                                    "manifest hash"
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
                current = self.skill_registry.resolve(
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
                current_tool = self.tool_registry.resolve_capability(capability)
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
        try:
            design_view = self._statistics_view(plan)
            qc_review = self.repository.get_approved_qc_review(
                design_view.design.qc_review_revision_id
            )
            if (
                qc_review.project_id != plan.project_id
                or qc_review.review.content_hash != design_view.design.qc_review_hash
                or qc_review.review.input_manifest_hash != plan.manifest_hash
            ):
                reasons.append(
                    {
                        "code": "qc_lock_changed",
                        "message": "approved QC review no longer matches the statistical design",
                    }
                )
            assert_statistics_ready(qc_review.review, design_view.design.subject_order)
            source_run = self.repository.get_run(qc_review.run_id)
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

    # -- runs ---------------------------------------------------------------
    def create_run(self, request: RunCreate, idempotency_key: str) -> RunView:
        def prepare() -> None:
            plan = self.repository.get_plan(request.plan_revision_id)
            if plan.plan_hash != request.expected_plan_hash:
                raise ConflictError(
                    "run_plan_hash_mismatch",
                    "运行请求的计划哈希与当前不可变计划不一致。",
                    expected=plan.plan_hash,
                    received=request.expected_plan_hash,
                )
            self._require_plan_current(plan)
            return None

        def finalize(_prepared: None) -> RunView:
            # The full source scan above stays outside SQLite's writer
            # transaction. After BEGIN IMMEDIATE, re-read the database locks
            # and environment fingerprint so a metadata revision in the
            # preparation window cannot enqueue a stale plan. Raw files cannot
            # be locked by SQLite and must be verified again before staging.
            plan = self.repository.get_plan(request.plan_revision_id)
            if plan.plan_hash != request.expected_plan_hash:
                raise ConflictError(
                    "run_plan_hash_mismatch",
                    "运行请求的计划哈希与当前不可变计划不一致。",
                    expected=plan.plan_hash,
                    received=request.expected_plan_hash,
                )
            self._require_plan_current(plan, verify_source_files=False)
            result = self.repository.create_run(
                project_id=request.project_id,
                plan_revision_id=request.plan_revision_id,
                expected_plan_hash=request.expected_plan_hash,
                max_attempts=request.max_attempts,
                payload={
                    "outcome": request.mock_outcome.value,
                    "delay_ms": request.mock_delay_ms,
                },
            )
            self.repository.append_event(
                project_id=result.project_id,
                run_id=result.run_id,
                event_type="RunQueued",
                severity="info",
                payload={
                    "plan_revision_id": result.plan_revision_id,
                    "executor": "mock",
                    "version": result.version,
                },
            )
            return result

        return self._idempotent_prepared(
            scope=f"projects:{request.project_id}:runs:create",
            key=idempotency_key,
            request=request,
            response_type=RunView,
            prepare=prepare,
            finalize=finalize,
        )

    def get_run(self, run_id: str) -> RunView:
        return self.repository.get_run(run_id)

    def diagnose_run(self, run_id: str, request: RunDiagnosisRequest) -> RunDiagnosisView:
        """Classify a bounded log excerpt without invoking a model or executor."""

        run = self.repository.get_run(run_id)
        diagnosis = diagnose_dpabi_log(request.log_text)
        return RunDiagnosisView(run_id=run.run_id, diagnosis=diagnosis)

    def inspect_ml_table(self, request: MlTableInspectRequest) -> MlTableInspectView:
        project = self.repository.get_project(request.project_id)
        source = self.path_policy.validate_read_path(
            request.source_path,
            project_roots=project.source_roots,
            expect_directory=False,
        )
        try:
            inspection = inspect_table(source, max_rows=request.max_rows)
        except (OSError, ValueError) as exc:
            raise InputValidationError(
                "ml_table_inspection_failed",
                "上传的表格无法检查, 请确认格式、编码和文件完整性。",
                reason=str(exc),
            ) from exc
        return MlTableInspectView(
            project_id=request.project_id,
            source_path_name=source.name,
            inspection=inspection,
        )

    def create_ml_template(self, request: MlTemplateCreateRequest) -> MlTemplateView:
        try:
            template = generate_ml_template(request.design, source_filename=request.source_filename)
        except (OSError, ValueError) as exc:
            raise InputValidationError(
                "ml_template_generation_failed",
                "机器学习模板生成失败, 请检查设计字段。",
                reason=str(exc),
            ) from exc
        return MlTemplateView(template=template)

    def validate_roi_table(self, request: RoiTableCreateRequest) -> RoiTableView:
        issues = validate_roi_request(request.design)
        if issues:
            return RoiTableView(valid=False, issues=issues)
        try:
            long_rows, wide_rows = build_roi_tables(request.records)
        except ValueError as exc:
            return RoiTableView(valid=False, issues=(str(exc),))
        return RoiTableView(valid=True, issues=(), long_rows=long_rows, wide_rows=wide_rows)

    def localize_clusters(self, request: ClusterLocalizationRequest) -> ClusterLocalizationView:
        results = localize_clusters(
            request.clusters,
            request.atlas_points,
            max_distance_mm=request.max_distance_mm,
        )
        return ClusterLocalizationView(results=results, atlas_supplied=bool(request.atlas_points))

    def answer_rsfmri_question(self, request: RsFmriQuestionRequest) -> RsFmriAnswerView:
        # Remote retrieval is intentionally disabled until an explicit provider
        # and outbound-context policy are configured. Local evidence is bounded
        # to project documentation and declarative skills.
        roots = (
            Path(__file__).resolve().parents[2] / "docs",
            Path(__file__).resolve().parents[2] / "skills",
            Path(__file__).resolve().parents[2] / "neuroagent",
        )
        evidence = search_evidence(roots, request.question)
        answer = answer_rsfmri_question(request.question, evidence)
        return RsFmriAnswerView(answer=answer, remote_search_used=False)

    def organization_preview(self, request: OrganizationPreviewRequest) -> OrganizationPreviewView:
        project = self.repository.get_project(request.project_id)
        source = self.path_policy.validate_read_path(
            request.source_path,
            project_roots=project.source_roots,
            expect_directory=True,
        )
        subjects = {
            subject_id: {
                "functional": tuple(item.functional),
                "anatomical": tuple(item.anatomical),
                "inventory": tuple(item.inventory),
            }
            for subject_id, item in request.subjects.items()
        }
        try:
            preview = build_dpabi_preview(
                source,
                target_stage=request.target_stage,
                subjects=subjects,
            )
        except (OSError, ValueError) as exc:
            raise InputValidationError(
                "organization_preview_failed",
                "数据整理预览失败, 请检查相对路径和 DPABI 目标目录。",
                reason=str(exc),
            ) from exc
        return OrganizationPreviewView(
            project_id=request.project_id,
            source_path_name=source.name,
            preview=preview,
        )

    def list_runs(
        self,
        *,
        project_id: str | None = None,
        state: WorkflowState | None = None,
    ) -> list[RunView]:
        return self.repository.list_runs(project_id=project_id, state=state)

    def create_statistics_run(self, request: StatisticsRunCreate, idempotency_key: str) -> RunView:
        def prepare() -> None:
            plan = self.repository.get_plan(request.statistical_design_revision_id)
            design_view = self._statistics_view(plan)
            approved_qc = self.repository.get_approved_qc_review(
                design_view.design.qc_review_revision_id
            )
            if approved_qc.review.content_hash != design_view.design.qc_review_hash:
                raise ConflictError(
                    "qc_review_hash_mismatch",
                    "统计运行引用的 QC review 已失配。",
                )
            if plan.plan_hash != request.expected_plan_hash:
                raise ConflictError(
                    "run_plan_hash_mismatch",
                    "运行请求的计划哈希与当前不可变统计设计不一致。",
                    expected=plan.plan_hash,
                    received=request.expected_plan_hash,
                )
            self._require_plan_current(plan)
            return None

        def finalize(_prepared: None) -> RunView:
            # Repeat statistical/QC, database-lock, and environment checks
            # while BEGIN IMMEDIATE prevents a concurrent metadata revision
            # from slipping between validation and run creation. The recursive
            # source-file scan just completed outside this writer transaction.
            plan = self.repository.get_plan(request.statistical_design_revision_id)
            design_view = self._statistics_view(plan)
            approved_qc = self.repository.get_approved_qc_review(
                design_view.design.qc_review_revision_id
            )
            if approved_qc.review.content_hash != design_view.design.qc_review_hash:
                raise ConflictError(
                    "qc_review_hash_mismatch",
                    "统计运行引用的 QC review 已失配。",
                )
            if plan.plan_hash != request.expected_plan_hash:
                raise ConflictError(
                    "run_plan_hash_mismatch",
                    "运行请求的计划哈希与当前不可变统计设计不一致。",
                    expected=plan.plan_hash,
                    received=request.expected_plan_hash,
                )
            self._require_plan_current(plan, verify_source_files=False)
            result = self.repository.create_run(
                project_id=request.project_id,
                plan_revision_id=request.statistical_design_revision_id,
                expected_plan_hash=request.expected_plan_hash,
                max_attempts=request.max_attempts,
                payload={
                    "outcome": "succeed",
                    "delay_ms": 0,
                    "run_kind": "statistics_mock",
                },
            )
            self.repository.append_event(
                project_id=result.project_id,
                run_id=result.run_id,
                event_type="StatisticsRunQueued",
                severity="info",
                payload={
                    "statistical_design_revision_id": result.plan_revision_id,
                    "plan_hash": request.expected_plan_hash,
                    "executor": "mock",
                },
            )
            return result

        return self._idempotent_prepared(
            scope=f"projects:{request.project_id}:statistics-runs:create",
            key=idempotency_key,
            request=request,
            response_type=RunView,
            prepare=prepare,
            finalize=finalize,
        )

    def register_statistical_result(
        self,
        *,
        run_id: str,
        manifest: StatisticalResultManifest,
        design: StatisticalDesignRevision,
        correction: CorrectionSpec | None,
        qc_review_hash: str,
        environment_hash: str,
        plan_hash: str,
        actor: str,
    ) -> StatisticalResultView:
        """Validate frozen evidence and persist the deterministic reproducibility report.

        Registration is a local use case: the report builder fail-closes on hash
        mismatches and incomplete manifests before any row is written.
        """

        run = self.repository.get_run(run_id)
        plan = self.repository.get_plan(run.plan_revision_id)
        if plan.plan_hash != plan_hash:
            raise ConflictError(
                "statistical_result_plan_hash_mismatch",
                "结果登记引用的方案哈希与运行所属已批准统计设计不一致。",
                expected=plan.plan_hash,
                received=plan_hash,
            )
        if manifest.run_id != run_id:
            raise ConflictError(
                "statistical_result_run_mismatch",
                "结果清单中的 run_id 与登记目标运行不一致。",
                expected=manifest.run_id,
                received=run_id,
            )
        report = build_statistical_reproducibility_report(
            manifest=manifest,
            design=design,
            correction=correction,
            qc_review_hash=qc_review_hash,
            environment_hash=environment_hash,
            plan_hash=plan_hash,
        )
        return self.repository.create_statistical_result(
            project_id=run.project_id,
            run_id=run_id,
            design_revision_id=manifest.design_revision_id,
            mode=manifest.mode.value,
            non_scientific=manifest.non_scientific,
            non_scientific_reason=manifest.non_scientific_reason,
            bundle_hash=report.bundle_hash,
            manifest=manifest.model_dump(mode="json"),
            report_markdown=report.markdown,
            report_json=report.json_text,
            actor=actor,
        )

    def list_statistical_results(
        self, *, project_id: str, run_id: str | None = None
    ) -> list[StatisticalResultView]:
        return self.repository.list_statistical_results(project_id=project_id, run_id=run_id)

    def get_statistical_result(self, result_id: str) -> StatisticalResultDetailView:
        return self.repository.get_statistical_result(result_id)

    def cancel_run(self, run_id: str, request: RunAction, idempotency_key: str) -> RunView:
        def action() -> RunView:
            before = self.repository.get_run(run_id)
            result = self.repository.request_cancel(run_id, request.expected_version)
            self.repository.append_event(
                project_id=result.project_id,
                run_id=run_id,
                event_type="RunCancellationRequested",
                severity="warning",
                payload={
                    "from_state": before.state.value,
                    "to_state": result.state.value,
                    "reason": request.reason,
                    "version": result.version,
                },
            )
            return result

        return self._idempotent(
            scope=f"runs:{run_id}:cancel",
            key=idempotency_key,
            request=request,
            response_type=RunView,
            action=action,
        )

    def create_qc_review(self, request: QcReviewCreate, idempotency_key: str) -> QcReviewView:
        def action() -> QcReviewView:
            self._require_skill_parameter_schema(
                "review-rsfmri-qc",
                {
                    "input_manifest_hash": self.repository.get_plan(
                        self.repository.get_run(request.run_id).plan_revision_id
                    ).manifest_hash,
                    "subject_order": [
                        *request.included_subject_ids,
                        *request.excluded_subject_ids,
                    ],
                    "metric_artifact_ids": list(request.metric_artifact_ids),
                    "checks": [check.model_dump(mode="json") for check in request.checks],
                },
            )
            review = self.repository.create_qc_review(request)
            self.repository.append_event(
                project_id=review.project_id,
                run_id=review.run_id,
                event_type="QcReviewRevisionCreated",
                severity="info",
                payload={
                    "review_revision_id": review.review.review_revision_id,
                    "review_hash": review.review.content_hash,
                    "included_count": len(review.review.included_subject_ids),
                    "excluded_count": len(review.review.excluded_subject_ids),
                    "artifact_count": len(review.review.metric_artifact_ids),
                },
            )
            return review

        return self._idempotent(
            scope=f"runs:{request.run_id}:qc-reviews:create",
            key=idempotency_key,
            request=request,
            response_type=QcReviewView,
            action=action,
        )

    def get_qc_review(self, review_revision_id: str) -> QcReviewView:
        return self.repository.get_qc_review(review_revision_id)

    def approve_qc_review(
        self,
        review_revision_id: str,
        request: QcReviewApprove,
        idempotency_key: str,
    ) -> QcReviewView:
        def action() -> QcReviewView:
            review, run = self.repository.approve_qc_review(review_revision_id, request)
            self.repository.append_event(
                project_id=review.project_id,
                run_id=review.run_id,
                event_type="QcReviewApproved" if request.approved else "QcReviewRejected",
                severity="info" if request.approved else "warning",
                payload={
                    "review_revision_id": review_revision_id,
                    "review_hash": review.review.content_hash,
                    "actor": request.actor,
                    "reason": request.reason,
                    "run_state": run.state.value,
                },
            )
            return review

        return self._idempotent(
            scope=f"qc-reviews:{review_revision_id}:approve",
            key=idempotency_key,
            request=request,
            response_type=QcReviewView,
            action=action,
        )

    def retry_run(self, run_id: str, request: RunAction, idempotency_key: str) -> RunView:
        def action() -> RunView:
            before = self.repository.get_run(run_id)
            result = self.repository.retry_run(run_id, expected_version=request.expected_version)
            self.repository.append_event(
                project_id=result.project_id,
                run_id=run_id,
                event_type="RunRetryRequested",
                severity="warning",
                payload={
                    "from_state": before.state.value,
                    "to_state": result.state.value,
                    "reason": request.reason,
                    "version": result.version,
                },
            )
            return result

        return self._idempotent(
            scope=f"runs:{run_id}:retry",
            key=idempotency_key,
            request=request,
            response_type=RunView,
            action=action,
        )

    def list_artifacts(self, run_id: str) -> list[ArtifactView]:
        self.repository.get_run(run_id)
        return self.repository.list_artifacts(run_id)

    def get_artifact(self, artifact_id: str) -> ArtifactView:
        return self.repository.get_artifact(artifact_id)

    def list_run_events(self, run_id: str, after_event_id: int = 0) -> list[RuntimeEventView]:
        self.repository.get_run(run_id)
        return self.repository.list_events(run_id, after_event_id)

    def list_project_events(
        self, project_id: str, after_event_id: int = 0
    ) -> list[RuntimeEventView]:
        self.repository.get_project(project_id)
        return self.repository.list_events(after_event_id=after_event_id, project_id=project_id)

    # -- model routing and Agent tasks -------------------------------------
    def create_model_profile(
        self, profile: ModelProfileInput, idempotency_key: str
    ) -> ModelProfileView:
        def action() -> ModelProfileView:
            result = self.repository.create_model_profile(profile)
            self.repository.append_event(
                project_id=None,
                run_id=None,
                event_type="ModelProfileCreated",
                severity="info",
                payload={
                    "profile_id": profile.id,
                    "provider": profile.provider,
                    "api_key_env": profile.api_key_env,
                },
            )
            return result

        return self._idempotent(
            scope="model-profiles:create",
            key=idempotency_key,
            request=profile,
            response_type=ModelProfileView,
            action=action,
        )

    def list_model_profiles(self) -> list[ModelProfileView]:
        return self.repository.list_model_profiles()

    def get_model_profile(self, profile_id: str) -> ModelProfileView:
        return self.repository.get_model_profile(profile_id)

    def _model_gateway(self) -> ModelGateway:
        if self.settings.redaction_salt is None:
            raise InputValidationError(
                "redaction_policy_not_configured",
                "未配置 RSFMRI_REDACTION_SALT, 外部模型调用已关闭。",
            )
        try:
            policy = OutboundContextPolicy(self.settings.redaction_salt)
        except OutboundPolicyError as exc:
            raise InputValidationError(
                "redaction_policy_invalid",
                "脱敏策略配置无效, 外部模型调用已关闭。",
            ) from exc
        profiles = [view.profile for view in self.repository.list_model_profiles()]
        return ModelGateway(
            ModelRouter(profiles, {}),
            self.providers,
            policy,
            self.secret_resolver,
        )

    async def _generate_recommendation(self, request: AgentTaskRequest) -> GatewayResult:
        try:
            result = await self._model_gateway().generate(request)
        except OutboundPolicyError as exc:
            raise InputValidationError(
                "outbound_context_rejected",
                "外发上下文无法确认已安全脱敏, 模型调用已阻断。",
            ) from exc
        except ModelRoutingError as exc:
            raise InputValidationError(
                "model_route_unavailable",
                "没有满足任务能力要求的模型配置。",
            ) from exc
        except (ModelGatewayError, ProviderError) as exc:
            raise ApplicationError(
                "model_gateway_unavailable",
                "模型服务不可用或返回内容未通过结构校验。",
                status_code=503,
            ) from exc
        proposed = result.recommendation.proposed_skill_request
        if proposed is not None:
            try:
                SkillRequest.model_validate(proposed)
            except ValidationError as exc:
                raise InputValidationError(
                    "agent_skill_request_invalid",
                    "模型提出的 SkillRequest 未通过严格结构和科研参数校验。",
                ) from exc
        return result

    async def test_provider(
        self, request: ProviderTestRequest, idempotency_key: str
    ) -> ProviderTestView:
        async def prepare() -> GatewayResult:
            profile = self.repository.get_model_profile(request.profile_id)
            if profile.version != request.expected_profile_version:
                raise ConflictError(
                    "revision_conflict",
                    "模型配置版本已变化, 请刷新后重试。",
                    expected=request.expected_profile_version,
                    actual=profile.version,
                )
            return await self._generate_recommendation(
                AgentTaskRequest(
                    task_type=TaskType.PLAN_EXPLAINER,
                    project_id="provider-connectivity-test",
                    summary=SafeAgentSummary(
                        purpose=AgentSummaryPurpose.PROVIDER_CONNECTIVITY_TEST
                    ),
                    preferred_profile_id=request.profile_id,
                )
            )

        def finalize(result: GatewayResult) -> ProviderTestView:
            return ProviderTestView(
                profile_id=request.profile_id,
                available=True,
                routing=result.routing,
                context_hash=result.context_hash,
            )

        return await self._idempotent_async(
            scope=f"model-profiles:{request.profile_id}:test",
            key=idempotency_key,
            request=request,
            response_type=ProviderTestView,
            prepare=prepare,
            finalize=finalize,
        )

    async def create_agent_task(
        self, request: AgentTaskCreate, idempotency_key: str
    ) -> AgentTaskView:
        async def prepare() -> GatewayResult:
            self._require_project_version(
                request.request.project_id, request.expected_project_version
            )
            return await self._generate_recommendation(request.request)

        def finalize(result: GatewayResult) -> AgentTaskView:
            task = self.repository.create_agent_task(
                project_id=request.request.project_id,
                expected_project_version=request.expected_project_version,
                task_type=request.request.task_type.value,
                result=result,
            )
            self.repository.append_event(
                project_id=task.project_id,
                run_id=None,
                event_type="AgentTaskCompleted",
                severity="info",
                payload={
                    "task_id": task.task_id,
                    "task_type": request.request.task_type.value,
                    "context_hash": result.context_hash,
                    "selected_profile_id": result.routing.selected_profile_id,
                },
            )
            return task

        return await self._idempotent_async(
            scope=f"projects:{request.request.project_id}:agent-tasks:create",
            key=idempotency_key,
            request=request,
            response_type=AgentTaskView,
            prepare=prepare,
            finalize=finalize,
        )

    def get_agent_task(self, task_id: str) -> AgentTaskView:
        return self.repository.get_agent_task(task_id)
