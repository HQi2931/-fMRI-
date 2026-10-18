"""SQLAlchemy persistence models; no scientific behavior belongs here."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ProjectRow(Base):
    __tablename__ = "projects"
    project_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    source_roots_json: Mapped[str] = mapped_column(Text)
    work_root: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DatasetRow(Base):
    __tablename__ = "datasets"
    dataset_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.project_id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    source_path: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    current_manifest_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ManifestRevisionRow(Base):
    __tablename__ = "manifest_revisions"
    manifest_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("datasets.dataset_id", ondelete="RESTRICT"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    content_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (UniqueConstraint("dataset_id", "revision"),)


class DemographicsRevisionRow(Base):
    __tablename__ = "demographics_revisions"
    demographics_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("datasets.dataset_id", ondelete="RESTRICT"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    content_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (UniqueConstraint("dataset_id", "revision"),)


class ExternalDemographicsRevisionRow(Base):
    __tablename__ = "external_demographics_revisions"
    demographics_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    external_import_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("external_analysis_imports.import_id", ondelete="RESTRICT"),
        index=True,
    )
    inclusion_hash: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    content_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (UniqueConstraint("external_import_id", "revision"),)


class DatasetSplitRevisionRow(Base):
    __tablename__ = "dataset_split_revisions"
    split_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("datasets.dataset_id", ondelete="RESTRICT"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    content_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (UniqueConstraint("dataset_id", "revision"),)


class AnalysisProposalOutboxRow(Base):
    __tablename__ = "analysis_proposal_outbox"
    intent_task_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(100), nullable=False)
    project_id: Mapped[str] = mapped_column(String(100), nullable=False)
    preprocessing_task_id: Mapped[str] = mapped_column(String(100), nullable=False)
    plan_revision_id: Mapped[str] = mapped_column(String(100), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(100), nullable=False)
    analysis_request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[float | None] = mapped_column(nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_generation: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[float | None] = mapped_column(nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    continuation_message_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("conversation_messages.message_id", ondelete="RESTRICT"),
        nullable=True,
    )
    analysis_work_task_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'ready', 'proposal_ready', 'waiting_user', 'blocked')",
            name="ck_analysis_outbox_status",
        ),
        CheckConstraint(
            "(status IN ('pending', 'ready') AND continuation_message_id IS NULL) OR "
            "(status IN ('proposal_ready', 'waiting_user', 'blocked') "
            "AND continuation_message_id IS NOT NULL)",
            name="ck_analysis_outbox_terminal_message",
        ),
        CheckConstraint(
            "(status = 'proposal_ready' AND analysis_work_task_id IS NOT NULL) OR "
            "(status != 'proposal_ready' AND analysis_work_task_id IS NULL)",
            name="ck_analysis_outbox_work_task",
        ),
        CheckConstraint("retry_generation >= 0", name="ck_analysis_outbox_retry_generation"),
        CheckConstraint("attempt_count >= 0", name="ck_analysis_outbox_attempt_count"),
        ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            ondelete="RESTRICT",
            name="fk_analysis_outbox_conversation",
        ),
        UniqueConstraint("continuation_message_id", name="uq_analysis_outbox_continuation_message"),
        Index(
            "ix_analysis_outbox_prep_binding",
            "conversation_id",
            "project_id",
            "preprocessing_task_id",
        ),
    )


class StatisticalResultInterpretationOutboxRow(Base):
    __tablename__ = "statistical_result_interpretation_outbox"
    task_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(100), nullable=False)
    project_id: Mapped[str] = mapped_column(String(100), nullable=False)
    run_id: Mapped[str] = mapped_column(String(100), nullable=False)
    plan_revision_id: Mapped[str] = mapped_column(String(100), nullable=False)
    result_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    result_bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_version: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[float | None] = mapped_column(nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_generation: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[float | None] = mapped_column(nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    continuation_message_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("conversation_messages.message_id", ondelete="RESTRICT"),
        nullable=True,
    )
    __table_args__ = (
        CheckConstraint(
            "status IN ('ready', 'completed', 'waiting_user', 'blocked')",
            name="ck_result_interpretation_status",
        ),
        CheckConstraint(
            "(status = 'ready' AND continuation_message_id IS NULL) OR "
            "(status != 'ready' AND continuation_message_id IS NOT NULL)",
            name="ck_result_interpretation_terminal_message",
        ),
        CheckConstraint("fencing_token >= 0", name="ck_result_interpretation_fencing_nonnegative"),
        CheckConstraint("attempt_count >= 0", name="ck_result_interpretation_attempt_nonnegative"),
        CheckConstraint(
            "retry_generation >= 0",
            name="ck_result_interpretation_generation_nonnegative",
        ),
        ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            ondelete="RESTRICT",
            name="fk_result_interpretation_conversation",
        ),
    )


class CompositeGoalLinkRow(Base):
    """Identity-only links for one explicit Work analysis goal."""

    __tablename__ = "composite_goal_links"
    intent_task_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.conversation_id", ondelete="RESTRICT")
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.project_id", ondelete="RESTRICT")
    )
    analysis_request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # ``work_tasks`` is created by Alembic SQL and is intentionally absent
    # from Base.metadata. The durable FK lives in migration 0028; keeping an
    # ORM FK here breaks the in-memory metadata.create_all() used by OpenAPI.
    goal_kind: Mapped[str] = mapped_column(
        String(40), default="preprocess_then_analyze", server_default="preprocess_then_analyze"
    )
    preprocessing_task_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_external_import_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("external_analysis_imports.import_id", ondelete="RESTRICT"),
        nullable=True,
    )
    source_import_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_tree_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_inclusion_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_revision_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    analysis_proposal_outbox_intent_task_id: Mapped[str | None] = mapped_column(
        String(100),
        ForeignKey("analysis_proposal_outbox.intent_task_id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    analysis_work_task_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    result_interpretation_outbox_task_id: Mapped[str | None] = mapped_column(
        String(100),
        ForeignKey("statistical_result_interpretation_outbox.task_id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    __table_args__ = (
        CheckConstraint(
            "length(analysis_request_hash) = 64",
            name="ck_composite_goal_request_hash_length",
        ),
        Index("ix_composite_goal_preprocessing_task", "preprocessing_task_id"),
        CheckConstraint(
            "goal_kind IN ('preprocess_then_analyze', 'analysis_only_external_import')",
            name="ck_composite_goal_kind",
        ),
        CheckConstraint(
            "(goal_kind = 'preprocess_then_analyze' AND preprocessing_task_id IS NOT NULL "
            "AND source_external_import_id IS NULL AND source_import_version IS NULL "
            "AND source_tree_hash IS NULL AND source_inclusion_hash IS NULL "
            "AND source_manifest_hash IS NULL AND source_revision_hash IS NULL) OR "
            "(goal_kind = 'analysis_only_external_import' AND preprocessing_task_id IS NULL "
            "AND source_external_import_id IS NOT NULL AND source_import_version IS NOT NULL "
            "AND source_tree_hash IS NOT NULL AND source_inclusion_hash IS NOT NULL "
            "AND source_manifest_hash IS NOT NULL AND source_revision_hash IS NOT NULL "
            "AND analysis_proposal_outbox_intent_task_id IS NULL "
            "AND (result_interpretation_outbox_task_id IS NULL "
            "OR (analysis_work_task_id IS NOT NULL "
            "AND result_interpretation_outbox_task_id = analysis_work_task_id)))",
            name="ck_composite_goal_source_kind",
        ),
    )


class CompositeGoalModelBudgetRow(Base):
    """Mutable cumulative provider allowance for one composite goal."""

    __tablename__ = "composite_goal_model_budgets"

    intent_task_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("composite_goal_links.intent_task_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    max_steps: Mapped[int] = mapped_column(Integer)
    max_model_calls: Mapped[int] = mapped_column(Integer)
    max_tokens: Mapped[int] = mapped_column(Integer)
    max_elapsed_seconds: Mapped[int] = mapped_column(Integer)
    used_steps: Mapped[int] = mapped_column(Integer, default=0)
    used_model_calls: Mapped[int] = mapped_column(Integer, default=0)
    used_tokens: Mapped[int] = mapped_column(Integer, default=0)
    elapsed_seconds: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        CheckConstraint("max_steps >= 1", name="ck_composite_goal_budget_max_steps"),
        CheckConstraint("max_model_calls >= 0", name="ck_composite_goal_budget_max_calls"),
        CheckConstraint("max_tokens >= 0", name="ck_composite_goal_budget_max_tokens"),
        CheckConstraint("max_elapsed_seconds >= 1", name="ck_composite_goal_budget_max_elapsed"),
        CheckConstraint("used_steps >= 0", name="ck_composite_goal_budget_used_steps"),
        CheckConstraint("used_model_calls >= 0", name="ck_composite_goal_budget_used_calls"),
        CheckConstraint("used_tokens >= 0", name="ck_composite_goal_budget_used_tokens"),
        CheckConstraint("elapsed_seconds >= 0", name="ck_composite_goal_budget_elapsed"),
    )


class CompositeGoalModelCallReservationRow(Base):
    """Idempotency ledger for provider calls charged to a composite goal."""

    __tablename__ = "composite_goal_model_call_reservations"

    intent_task_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("composite_goal_model_budgets.intent_task_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    reservation_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(16))
    estimated_tokens: Mapped[int] = mapped_column(Integer)
    reserved_tokens: Mapped[int] = mapped_column(Integer)
    actual_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elapsed_seconds: Mapped[int] = mapped_column(Integer, default=0)
    reserved_elapsed_seconds: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        UniqueConstraint(
            "intent_task_id", "call_id", name="uq_composite_goal_model_reservation_call"
        ),
        CheckConstraint(
            "state IN ('reserved', 'settled', 'exhausted')",
            name="ck_composite_goal_model_reservation_state",
        ),
        CheckConstraint(
            "estimated_tokens >= 0", name="ck_composite_goal_model_reservation_estimate"
        ),
        CheckConstraint(
            "reserved_tokens >= 0", name="ck_composite_goal_model_reservation_reserved"
        ),
        CheckConstraint(
            "actual_tokens IS NULL OR actual_tokens >= 0",
            name="ck_composite_goal_model_reservation_actual",
        ),
        CheckConstraint("elapsed_seconds >= 0", name="ck_composite_goal_model_reservation_elapsed"),
        CheckConstraint(
            "reserved_elapsed_seconds >= 0",
            name="ck_composite_goal_model_reservation_reserved_elapsed",
        ),
        Index("ix_composite_goal_model_reservation_goal", "intent_task_id", "state"),
    )


class CompositeGoalAnalysisAttemptRow(Base):
    """Identity-only attempt history for explicit composite goals."""

    __tablename__ = "composite_goal_analysis_attempts"
    intent_task_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("composite_goal_links.intent_task_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_task_id: Mapped[str] = mapped_column(String(100), unique=True)
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        CheckConstraint("attempt_number >= 1", name="ck_composite_goal_attempt_positive"),
        CheckConstraint("length(proposal_hash) = 64", name="ck_composite_goal_attempt_hash_length"),
        UniqueConstraint(
            "intent_task_id", "proposal_hash", name="uq_composite_goal_attempt_proposal"
        ),
    )


class PlanRevisionRow(Base):
    __tablename__ = "plan_revisions"
    plan_revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.project_id", ondelete="RESTRICT"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(32))
    plan_hash: Mapped[str] = mapped_column(String(64), index=True)
    manifest_hash: Mapped[str] = mapped_column(String(64))
    environment_hash: Mapped[str] = mapped_column(String(64))
    plan_json: Mapped[str] = mapped_column(Text)
    validation_issues_json: Mapped[str] = mapped_column(Text, default="[]")
    supersedes_plan_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    __table_args__ = (UniqueConstraint("project_id", "revision"),)


class ApprovalRow(Base):
    __tablename__ = "approval_records"
    approval_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plan_revisions.plan_revision_id", ondelete="RESTRICT"), index=True
    )
    plan_hash: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(200))
    decision: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkflowRunRow(Base):
    __tablename__ = "workflow_runs"
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.project_id", ondelete="RESTRICT"), index=True
    )
    plan_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plan_revisions.plan_revision_id", ondelete="RESTRICT"), index=True
    )
    state: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    qc_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    qc_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class QcSkipRow(Base):
    __tablename__ = "qc_skip_records"
    qc_skip_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.run_id", ondelete="RESTRICT"), unique=True, index=True
    )
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    run_version: Mapped[int] = mapped_column(Integer)
    decision_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    actor: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text)
    qc_review_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class JobRow(Base):
    __tablename__ = "jobs"
    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.run_id", ondelete="RESTRICT"), unique=True
    )
    executor_type: Mapped[str] = mapped_column(String(50))
    state: Mapped[str] = mapped_column(String(32), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    __table_args__ = (Index("ix_jobs_claim", "state", "lease_expires_at", "created_at"),)


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    artifact_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.run_id", ondelete="RESTRICT"), index=True
    )
    artifact_type: Mapped[str] = mapped_column(String(100))
    relative_path: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    provenance_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class StatisticalResultRow(Base):
    __tablename__ = "statistical_results"
    result_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_runs.run_id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    design_revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    mode: Mapped[str] = mapped_column(String(40), nullable=False)
    non_scientific: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    non_scientific_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    report_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    report_json: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ExternalAnalysisImportRow(Base):
    __tablename__ = "external_analysis_imports"
    import_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.project_id", ondelete="RESTRICT"), index=True
    )
    source_path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    source_tree_hash: Mapped[str] = mapped_column(String(64), index=True)
    inspection_json: Mapped[str] = mapped_column(Text)
    inclusion_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    inclusion_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class RuntimeEventRow(Base):
    __tablename__ = "runtime_events"
    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(36), index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    severity: Mapped[str] = mapped_column(String(20))
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class IdempotencyRow(Base):
    __tablename__ = "idempotency_records"
    record_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope: Mapped[str] = mapped_column(String(200))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    __table_args__ = (UniqueConstraint("scope", "idempotency_key"),)


class ModelProfileRow(Base):
    __tablename__ = "model_profiles"
    profile_id: Mapped[str] = mapped_column(String(63), primary_key=True)
    profile_json: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AgentTaskRow(Base):
    __tablename__ = "agent_tasks"
    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.project_id", ondelete="RESTRICT"), index=True
    )
    state: Mapped[str] = mapped_column(String(32), index=True)
    task_type: Mapped[str] = mapped_column(String(50))
    context_hash: Mapped[str] = mapped_column(String(64))
    result_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ConversationRow(Base):
    __tablename__ = "conversations"
    conversation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mode: Mapped[str] = mapped_column(String(20), index=True)
    title: Mapped[str] = mapped_column(String(200))
    workspace_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    preferred_profile_id: Mapped[str | None] = mapped_column(String(63), nullable=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.project_id", ondelete="RESTRICT"), nullable=True
    )
    active_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workflow_runs.run_id", ondelete="RESTRICT"), nullable=True
    )
    foreground_agent_role: Mapped[str] = mapped_column(String(32), default="preprocessing")
    work_tasks_json: Mapped[str] = mapped_column(Text, default="{}")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class WorkspaceConsentRow(Base):
    __tablename__ = "workspace_content_consents"
    consent_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.conversation_id", ondelete="CASCADE"), index=True
    )
    workspace_hash: Mapped[str] = mapped_column(String(64), index=True)
    profile_id: Mapped[str] = mapped_column(String(63))
    profile_revision: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(200))
    policy_version: Mapped[str] = mapped_column(String(32))
    provider_host: Mapped[str] = mapped_column(String(255))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "workspace_hash",
            "profile_id",
            "model",
            "policy_version",
            name="uq_workspace_consent_scope",
        ),
    )


class WorkspacePatchRow(Base):
    __tablename__ = "workspace_change_proposals"
    proposal_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.conversation_id", ondelete="CASCADE"), index=True
    )
    workspace_hash: Mapped[str] = mapped_column(String(64), index=True)
    proposal_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    edits_json: Mapped[str] = mapped_column(Text)
    diff_text: Mapped[str] = mapped_column(Text)
    citations_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(20), index=True, default="proposed")
    backup_dir: Mapped[str | None] = mapped_column(Text, nullable=True)
    postimage_json: Mapped[str] = mapped_column(Text, default="{}")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ConfirmationChallengeRow(Base):
    __tablename__ = "confirmation_challenges"
    confirmation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.conversation_id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.project_id", ondelete="RESTRICT"), index=True
    )
    plan_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plan_revisions.plan_revision_id", ondelete="RESTRICT"), index=True
    )
    plan_hash: Mapped[str] = mapped_column(String(64), index=True)
    qc_mode: Mapped[str] = mapped_column(String(20))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workflow_runs.run_id", ondelete="RESTRICT"), nullable=True
    )


class ConversationMessageRow(Base):
    __tablename__ = "conversation_messages"
    message_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.conversation_id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    continuation_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence"),
        UniqueConstraint("continuation_key", name="uq_conversation_message_continuation_key"),
    )


class ConversationToolCallRow(Base):
    __tablename__ = "conversation_tool_calls"
    tool_call_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.conversation_id", ondelete="CASCADE"), index=True
    )
    user_message_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("conversation_messages.message_id", ondelete="CASCADE"),
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32), index=True)
    input_json: Mapped[str] = mapped_column(Text, default="{}")
    output_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Agent tool-loop telemetry; null for rows written before the loop existed.
    step_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    risk_tier: Mapped[str | None] = mapped_column(String(16), nullable=True)
    provider_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ContextSummaryRow(Base):
    __tablename__ = "context_summary_revisions"
    summary_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.conversation_id", ondelete="CASCADE"), index=True
    )
    covered_sequence: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(64))
    method: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MemoryRecordRow(Base):
    __tablename__ = "memory_records"
    importance: Mapped[float] = mapped_column(default=0.5)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    proposed_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposal_source_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    memory_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.conversation_id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("projects.project_id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    scope: Mapped[str] = mapped_column(String(20), index=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    key: Mapped[str] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), index=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    source_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    confidence: Mapped[float] = mapped_column(default=1.0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    __table_args__ = (UniqueConstraint("conversation_id", "scope", "kind", "key"),)


class MemoryEmbeddingRow(Base):
    __tablename__ = "memory_embeddings"
    memory_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory_records.memory_id", ondelete="CASCADE"), primary_key=True
    )
    memory_version: Mapped[int] = mapped_column(Integer)
    model_identity: Mapped[str] = mapped_column(Text)
    vector_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ContextSnapshotRow(Base):
    __tablename__ = "context_snapshots"
    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.conversation_id", ondelete="CASCADE"), index=True
    )
    assistant_message_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("conversation_messages.message_id", ondelete="CASCADE"),
        nullable=True,
    )
    context_hash: Mapped[str] = mapped_column(String(64), index=True)
    profile_id: Mapped[str | None] = mapped_column(String(63), nullable=True)
    manifest_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class QcReviewRow(Base):
    __tablename__ = "qc_review_revisions"
    review_revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.run_id", ondelete="RESTRICT"), index=True
    )
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(20), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    content_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    __table_args__ = (UniqueConstraint("run_id", "revision"),)


class QcApprovalRow(Base):
    __tablename__ = "qc_approval_records"
    qc_approval_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("qc_review_revisions.review_revision_id", ondelete="RESTRICT"),
        index=True,
    )
    review_hash: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(200))
    decision: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PaperRow(Base):
    index_status: Mapped[str] = mapped_column(String(20), default="not_indexed")
    index_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    __tablename__ = "papers"
    paper_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    authors_json: Mapped[str] = mapped_column(Text, default="[]")
    year: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    journal: Mapped[str | None] = mapped_column(String(500), nullable=True)
    doi: Mapped[str | None] = mapped_column(String(300), nullable=True, index=True)
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_file: Mapped[str] = mapped_column(Text)
    source_sha256: Mapped[str] = mapped_column(String(64), index=True)
    page_count: Mapped[int] = mapped_column(Integer)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PaperSectionRow(Base):
    __tablename__ = "paper_sections"
    section_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    paper_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("papers.paper_id", ondelete="CASCADE"), index=True
    )
    section_index: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500))
    section: Mapped[str] = mapped_column(String(200), index=True)
    subsection: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    level: Mapped[int] = mapped_column(Integer)
    page_start: Mapped[int] = mapped_column(Integer, index=True)
    page_end: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    paragraphs_json: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    __table_args__ = (UniqueConstraint("paper_id", "section_index"),)


class LiteratureChunkRow(Base):
    __tablename__ = "literature_chunks"
    chunk_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    paper_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("papers.paper_id", ondelete="CASCADE"), index=True
    )
    section: Mapped[str] = mapped_column(String(200), index=True)
    subsection: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    page_start: Mapped[int] = mapped_column(Integer, index=True)
    page_end: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    chunk_index: Mapped[int] = mapped_column(Integer)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    __table_args__ = (UniqueConstraint("paper_id", "chunk_index"),)
