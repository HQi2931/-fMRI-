"""Create the initial local metadata schema.

Revision ID: 0001_initial
Revises: None
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("project_id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("source_roots_json", sa.Text(), nullable=False),
        sa.Column("work_root", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "datasets",
        sa.Column("dataset_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("current_manifest_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_datasets_project_id", "datasets", ["project_id"])
    _create_revision_table("manifest_revisions", "manifest_id", "datasets", "dataset_id")
    _create_revision_table("demographics_revisions", "demographics_id", "datasets", "dataset_id")
    _create_revision_table("dataset_split_revisions", "split_id", "datasets", "dataset_id")
    op.create_table(
        "plan_revisions",
        sa.Column("plan_revision_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("environment_hash", sa.String(64), nullable=False),
        sa.Column("plan_json", sa.Text(), nullable=False),
        sa.Column("validation_issues_json", sa.Text(), nullable=False),
        sa.Column("supersedes_plan_revision_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("project_id", "revision"),
    )
    op.create_index("ix_plan_revisions_project_id", "plan_revisions", ["project_id"])
    op.create_index("ix_plan_revisions_plan_hash", "plan_revisions", ["plan_hash"])
    op.create_table(
        "approval_records",
        sa.Column("approval_id", sa.String(36), primary_key=True),
        sa.Column("plan_revision_id", sa.String(36), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_revision_id"], ["plan_revisions.plan_revision_id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_approval_records_plan_revision_id", "approval_records", ["plan_revision_id"]
    )
    op.create_table(
        "workflow_runs",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("plan_revision_id", sa.String(36), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["plan_revision_id"], ["plan_revisions.plan_revision_id"], ondelete="RESTRICT"
        ),
    )
    op.create_index("ix_workflow_runs_project_id", "workflow_runs", ["project_id"])
    op.create_index("ix_workflow_runs_plan_revision_id", "workflow_runs", ["plan_revision_id"])
    op.create_index("ix_workflow_runs_state", "workflow_runs", ["state"])
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False, unique=True),
        sa.Column("executor_type", sa.String(50), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.run_id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_jobs_state", "jobs", ["state"])
    op.create_index("ix_jobs_claim", "jobs", ["state", "lease_expires_at", "created_at"])
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("artifact_type", sa.String(100), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("provenance_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.run_id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_artifacts_project_id", "artifacts", ["project_id"])
    op.create_index("ix_artifacts_run_id", "artifacts", ["run_id"])
    op.create_table(
        "runtime_events",
        sa.Column("event_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("trace_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=True),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_runtime_events_project_id", "runtime_events", ["project_id"])
    op.create_index("ix_runtime_events_run_id", "runtime_events", ["run_id"])
    op.create_index("ix_runtime_events_event_type", "runtime_events", ["event_type"])
    op.create_index("ix_runtime_events_trace_id", "runtime_events", ["trace_id"])
    op.create_table(
        "idempotency_records",
        sa.Column("record_id", sa.String(36), primary_key=True),
        sa.Column("scope", sa.String(200), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scope", "idempotency_key"),
    )


def _create_revision_table(name: str, primary_key: str, parent_table: str, parent_key: str) -> None:
    op.create_table(
        name,
        sa.Column(primary_key, sa.String(36), primary_key=True),
        sa.Column(parent_key, sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            [parent_key], [f"{parent_table}.{parent_key}"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(parent_key, "revision"),
    )
    op.create_index(f"ix_{name}_{parent_key}", name, [parent_key])


def downgrade() -> None:
    for table in (
        "idempotency_records",
        "runtime_events",
        "artifacts",
        "jobs",
        "workflow_runs",
        "approval_records",
        "plan_revisions",
        "dataset_split_revisions",
        "demographics_revisions",
        "manifest_revisions",
        "datasets",
        "projects",
    ):
        op.drop_table(table)
