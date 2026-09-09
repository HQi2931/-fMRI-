"""SQLAlchemy persistence models; no scientific behavior belongs here."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
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
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


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
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (UniqueConstraint("conversation_id", "sequence"),)


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
