"""Persist exact links for explicit composite Work goals.

Revision ID: 0028_composite_goal_links
Revises: 0027_statistical_result_interpretation_outbox
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0028_composite_goal_links"
down_revision: str | None = "0027_statistical_result_interpretation_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    op.create_table(
        "composite_goal_links",
        sa.Column("intent_task_id", sa.String(100), primary_key=True),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("analysis_request_hash", sa.String(64), nullable=False),
        sa.Column("preprocessing_task_id", sa.String(100), nullable=False),
        sa.Column("analysis_proposal_outbox_intent_task_id", sa.String(100), nullable=True),
        sa.Column("analysis_work_task_id", sa.String(100), nullable=True),
        sa.Column("result_interpretation_outbox_task_id", sa.String(100), nullable=True),
        sa.CheckConstraint(
            "length(analysis_request_hash) = 64",
            name="ck_composite_goal_request_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_project",
        ),
        sa.ForeignKeyConstraint(
            ["preprocessing_task_id"],
            ["work_tasks.task_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_preprocessing_task",
        ),
        sa.UniqueConstraint("preprocessing_task_id", name="uq_composite_goal_preprocessing_task"),
        sa.ForeignKeyConstraint(
            ["analysis_proposal_outbox_intent_task_id"],
            ["analysis_proposal_outbox.intent_task_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_proposal_outbox",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_work_task_id"],
            ["work_tasks.task_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_analysis_task",
        ),
        sa.ForeignKeyConstraint(
            ["result_interpretation_outbox_task_id"],
            ["statistical_result_interpretation_outbox.task_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_result_outbox",
        ),
        sa.UniqueConstraint(
            "analysis_proposal_outbox_intent_task_id",
            name="uq_composite_goal_proposal_outbox",
        ),
        sa.UniqueConstraint(
            "result_interpretation_outbox_task_id",
            name="uq_composite_goal_result_outbox",
        ),
    )
    op.create_index(
        "ix_composite_goal_conversation_project",
        "composite_goal_links",
        ["conversation_id", "project_id"],
    )
    op.create_table(
        "composite_goal_analysis_attempts",
        sa.Column("intent_task_id", sa.String(100), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("work_task_id", sa.String(100), nullable=False),
        sa.Column("proposal_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("attempt_number >= 1", name="ck_composite_goal_attempt_positive"),
        sa.CheckConstraint(
            "length(proposal_hash) = 64", name="ck_composite_goal_attempt_hash_length"
        ),
        sa.ForeignKeyConstraint(
            ["intent_task_id"],
            ["composite_goal_links.intent_task_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_attempt_goal",
        ),
        sa.ForeignKeyConstraint(
            ["work_task_id"],
            ["work_tasks.task_id"],
            ondelete="RESTRICT",
            name="fk_composite_goal_attempt_work_task",
        ),
        sa.PrimaryKeyConstraint("intent_task_id", "attempt_number"),
        sa.UniqueConstraint("work_task_id", name="uq_composite_goal_attempt_task"),
        sa.UniqueConstraint(
            "intent_task_id", "proposal_hash", name="uq_composite_goal_attempt_proposal"
        ),
    )
    _backfill_explicit_composite_goals(connection)


def _backfill_explicit_composite_goals(connection: Any) -> None:
    """Backfill only legacy bindings whose independent authority rows agree exactly.

    Ambiguous or partially inconsistent records are deliberately left without a
    composite root. Runtime callers then fail closed and can recover them only
    through an explicit, reviewed relink.
    """

    candidates: list[dict[str, Any]] = []
    task_rows = connection.execute(
        sa.text("SELECT task_id, authority_refs_json FROM work_tasks ORDER BY task_id")
    ).mappings()
    for task_row in task_rows:
        try:
            refs = json.loads(task_row["authority_refs_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(refs, dict):
            continue
        intent_id = refs.get("analysis_intent_task_id")
        request_hash = refs.get("analysis_request_hash")
        conversation_id = refs.get("conversation_id")
        plan_id = refs.get("plan_revision_id")
        manifest_hash = refs.get("input_revision")
        if (
            not all(
                isinstance(value, str) and value
                for value in (intent_id, conversation_id, plan_id, manifest_hash)
            )
            or not isinstance(request_hash, str)
            or len(request_hash) != 64
        ):
            continue
        if any(character not in "0123456789abcdef" for character in request_hash):
            continue

        conversation = (
            connection.execute(
                sa.text(
                    "SELECT mode, project_id FROM conversations "
                    "WHERE conversation_id=:conversation_id"
                ),
                {"conversation_id": conversation_id},
            )
            .mappings()
            .first()
        )
        plan = (
            connection.execute(
                sa.text(
                    "SELECT project_id, manifest_hash FROM plan_revisions "
                    "WHERE plan_revision_id=:plan_id"
                ),
                {"plan_id": plan_id},
            )
            .mappings()
            .first()
        )
        if (
            conversation is None
            or conversation["mode"] != "work"
            or plan is None
            or conversation["project_id"] not in {None, plan["project_id"]}
            or manifest_hash != plan["manifest_hash"]
        ):
            continue
        project_id = plan["project_id"]

        step_rows = connection.execute(
            sa.text(
                "SELECT step_json FROM work_task_steps WHERE task_id=:task_id ORDER BY sequence"
            ),
            {"task_id": task_row["task_id"]},
        ).mappings()
        preprocessing_steps: list[dict[str, Any]] = []
        for step_row in step_rows:
            try:
                step = json.loads(step_row["step_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(step, dict) and step.get("kind") == "preprocessing_run":
                preprocessing_steps.append(step)
        if len(preprocessing_steps) != 1:
            continue
        step = preprocessing_steps[0]
        step_refs = step.get("authority_refs")
        if not isinstance(step_refs, dict) or any(
            step_refs.get(key) != value
            for key, value in (
                ("conversation_id", conversation_id),
                ("analysis_intent_task_id", intent_id),
                ("analysis_request_hash", request_hash),
                ("plan_revision_id", plan_id),
                ("input_revision", manifest_hash),
            )
        ):
            continue

        outbox = (
            connection.execute(
                sa.text("SELECT * FROM analysis_proposal_outbox WHERE intent_task_id=:intent_id"),
                {"intent_id": intent_id},
            )
            .mappings()
            .first()
        )
        if outbox is not None and any(
            outbox[key] != value
            for key, value in (
                ("conversation_id", conversation_id),
                ("project_id", project_id),
                ("preprocessing_task_id", task_row["task_id"]),
                ("analysis_request_hash", request_hash),
                ("plan_revision_id", plan_id),
                ("manifest_hash", manifest_hash),
            )
        ):
            continue
        if outbox is not None:
            run = (
                connection.execute(
                    sa.text(
                        "SELECT project_id, plan_revision_id FROM workflow_runs "
                        "WHERE run_id=:run_id"
                    ),
                    {"run_id": outbox["run_id"]},
                )
                .mappings()
                .first()
            )
            if run is None or (
                run["project_id"] != project_id or run["plan_revision_id"] != plan_id
            ):
                continue

        initial_task_id = outbox["analysis_work_task_id"] if outbox is not None else None
        attempt: dict[str, Any] | None = None
        analysis_refs: dict[str, Any] | None = None
        if outbox is not None and outbox["status"] == "proposal_ready":
            if not isinstance(initial_task_id, str) or not initial_task_id:
                continue
            analysis_row = (
                connection.execute(
                    sa.text("SELECT authority_refs_json FROM work_tasks WHERE task_id=:task_id"),
                    {"task_id": initial_task_id},
                )
                .mappings()
                .first()
            )
            if analysis_row is None:
                continue
            try:
                analysis_refs = json.loads(analysis_row["authority_refs_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(analysis_refs, dict) or (
                analysis_refs.get("conversation_id") != conversation_id
                or analysis_refs.get("analysis_intent_task_id") not in {None, intent_id}
                or analysis_refs.get("analysis_request_hash") not in {None, request_hash}
            ):
                continue
            analysis_steps = connection.execute(
                sa.text("SELECT step_id, step_json FROM work_task_steps WHERE task_id=:task_id"),
                {"task_id": initial_task_id},
            ).mappings()
            proposal_steps: list[tuple[str, dict[str, Any]]] = []
            for analysis_step_row in analysis_steps:
                try:
                    analysis_step = json.loads(analysis_step_row["step_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(analysis_step, dict)
                    and analysis_step.get("kind") == "statistical_run"
                ):
                    proposal_steps.append((analysis_step_row["step_id"], analysis_step))
            if (
                len(proposal_steps) != 1
                or not isinstance(proposal_steps[0][1].get("input_version"), str)
                or len(proposal_steps[0][1]["input_version"]) != 64
                or any(
                    char not in "0123456789abcdef" for char in proposal_steps[0][1]["input_version"]
                )
            ):
                continue
            proposal_hash = proposal_steps[0][1]["input_version"]
            expected_task_id = (
                "work-analysis-"
                + hashlib.sha256(
                    f"{conversation_id}\0analysis\0{proposal_hash}".encode()
                ).hexdigest()[:32]
            )
            if initial_task_id != expected_task_id:
                continue
            analysis_step_id, analysis_step = proposal_steps[0]
            analysis_step_refs = analysis_step.get("authority_refs")
            if not isinstance(analysis_step_refs, dict) or (
                analysis_step_refs.get("conversation_id") != conversation_id
                or analysis_step_refs.get("analysis_intent_task_id") not in {None, intent_id}
                or analysis_step_refs.get("analysis_request_hash") not in {None, request_hash}
            ):
                continue
            continuation_message = (
                connection.execute(
                    sa.text(
                        "SELECT conversation_id, role, payload_json FROM conversation_messages "
                        "WHERE message_id=:message_id"
                    ),
                    {"message_id": outbox["continuation_message_id"]},
                )
                .mappings()
                .first()
            )
            if (
                continuation_message is None
                or continuation_message["conversation_id"] != conversation_id
                or continuation_message["role"] != "assistant"
            ):
                continue
            try:
                continuation_payload = json.loads(continuation_message["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            continuation = (
                continuation_payload.get("analysis_outbox_continuation")
                if isinstance(continuation_payload, dict)
                else None
            )
            proposal = (
                continuation_payload.get("statistical_design_proposal")
                if isinstance(continuation_payload, dict)
                else None
            )
            if (
                not isinstance(continuation, dict)
                or continuation.get("intent_task_id") != intent_id
                or not isinstance(proposal, dict)
                or proposal.get("proposal_hash") != proposal_hash
            ):
                continue
            attempt = {
                "intent_task_id": intent_id,
                "attempt_number": 1,
                "work_task_id": initial_task_id,
                "proposal_hash": proposal_hash,
            }
            analysis_refs_to_update = {
                **analysis_refs,
                "analysis_intent_task_id": intent_id,
                "analysis_request_hash": request_hash,
            }
            analysis_step_to_update = {
                **analysis_step,
                "authority_refs": {
                    **analysis_step_refs,
                    "analysis_intent_task_id": intent_id,
                    "analysis_request_hash": request_hash,
                },
            }
        elif initial_task_id is not None:
            # Existing non-proposal_ready outbox rows must not claim a task.
            continue

        result_outbox = None
        if initial_task_id is not None:
            result_outbox = (
                connection.execute(
                    sa.text(
                        "SELECT task_id FROM statistical_result_interpretation_outbox "
                        "WHERE task_id=:task_id"
                    ),
                    {"task_id": initial_task_id},
                )
                .mappings()
                .first()
            )
            if result_outbox is not None:
                if analysis_refs is None:
                    continue
                result_row = (
                    connection.execute(
                        sa.text(
                            "SELECT conversation_id, project_id, run_id, plan_revision_id "
                            "FROM statistical_result_interpretation_outbox WHERE task_id=:task_id"
                        ),
                        {"task_id": initial_task_id},
                    )
                    .mappings()
                    .one()
                )
                if (
                    result_row["conversation_id"] != conversation_id
                    or result_row["project_id"] != project_id
                    or result_row["plan_revision_id"] != analysis_refs.get("plan_revision_id")
                    or result_row["run_id"] != analysis_refs.get("workflow_run_id")
                ):
                    continue

        candidates.append(
            {
                "intent_task_id": intent_id,
                "conversation_id": conversation_id,
                "project_id": project_id,
                "analysis_request_hash": request_hash,
                "preprocessing_task_id": task_row["task_id"],
                "analysis_proposal_outbox_intent_task_id": (
                    intent_id if outbox is not None else None
                ),
                "analysis_work_task_id": initial_task_id,
                "result_interpretation_outbox_task_id": (
                    initial_task_id if result_outbox is not None else None
                ),
                "attempt": attempt,
                "analysis_authority_refs": (
                    analysis_refs_to_update
                    if outbox is not None and outbox["status"] == "proposal_ready"
                    else None
                ),
                "analysis_step_id": (
                    analysis_step_id
                    if outbox is not None and outbox["status"] == "proposal_ready"
                    else None
                ),
                "analysis_step_json": (
                    analysis_step_to_update
                    if outbox is not None and outbox["status"] == "proposal_ready"
                    else None
                ),
            }
        )

    # Duplicate intents or preprocessing tasks make the legacy authority
    # ambiguous. Drop every candidate in those duplicate groups.
    intent_counts: dict[str, int] = {}
    preprocessing_counts: dict[str, int] = {}
    for candidate in candidates:
        intent_counts[candidate["intent_task_id"]] = (
            intent_counts.get(candidate["intent_task_id"], 0) + 1
        )
        preprocessing_counts[candidate["preprocessing_task_id"]] = (
            preprocessing_counts.get(candidate["preprocessing_task_id"], 0) + 1
        )
    for candidate in candidates:
        if (
            intent_counts[candidate["intent_task_id"]] != 1
            or preprocessing_counts[candidate["preprocessing_task_id"]] != 1
        ):
            continue
        connection.execute(
            sa.text(
                "INSERT INTO composite_goal_links "
                "(intent_task_id, conversation_id, project_id, analysis_request_hash, "
                "preprocessing_task_id, analysis_proposal_outbox_intent_task_id, "
                "analysis_work_task_id, result_interpretation_outbox_task_id) "
                "VALUES (:intent_task_id, :conversation_id, :project_id, :analysis_request_hash, "
                ":preprocessing_task_id, :analysis_proposal_outbox_intent_task_id, "
                ":analysis_work_task_id, :result_interpretation_outbox_task_id)"
            ),
            {
                key: candidate[key]
                for key in (
                    "intent_task_id",
                    "conversation_id",
                    "project_id",
                    "analysis_request_hash",
                    "preprocessing_task_id",
                    "analysis_proposal_outbox_intent_task_id",
                    "analysis_work_task_id",
                    "result_interpretation_outbox_task_id",
                )
            },
        )
        if candidate["analysis_authority_refs"] is not None:
            connection.execute(
                sa.text("UPDATE work_tasks SET authority_refs_json=:refs WHERE task_id=:task_id"),
                {
                    "refs": json.dumps(
                        candidate["analysis_authority_refs"],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "task_id": candidate["analysis_work_task_id"],
                },
            )
            connection.execute(
                sa.text(
                    "UPDATE work_task_steps SET step_json=:step WHERE task_id=:task_id "
                    "AND step_id=:step_id"
                ),
                {
                    "step": json.dumps(
                        candidate["analysis_step_json"],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "task_id": candidate["analysis_work_task_id"],
                    "step_id": candidate["analysis_step_id"],
                },
            )
        if candidate["attempt"] is not None:
            connection.execute(
                sa.text(
                    "INSERT INTO composite_goal_analysis_attempts "
                    "(intent_task_id, attempt_number, work_task_id, proposal_hash) "
                    "VALUES (:intent_task_id, :attempt_number, :work_task_id, :proposal_hash)"
                ),
                candidate["attempt"],
            )


def downgrade() -> None:
    op.drop_table("composite_goal_analysis_attempts")
    op.drop_index("ix_composite_goal_conversation_project", table_name="composite_goal_links")
    op.drop_table("composite_goal_links")
