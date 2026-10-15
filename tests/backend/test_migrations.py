from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
import textwrap
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def test_migrations_do_not_disable_application_loggers(tmp_path: Path) -> None:
    """Running migrations in-process must not switch off neuroagent logging.

    ``fileConfig`` defaults to ``disable_existing_loggers=True``, which silenced
    every already-imported ``neuroagent.*`` logger as soon as Alembic ran.  The
    API's unhandled-error log and the worker's executor traceback both went
    missing in production while every test still passed.
    """

    application_logger = logging.getLogger("neuroagent.workflow.worker")
    assert application_logger.disabled is False

    database_path = tmp_path / "logging.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    command.upgrade(config, "head")

    assert application_logger.disabled is False, "migrations disabled application logging"
    assert application_logger.isEnabledFor(logging.ERROR), (
        "migrations raised the effective level of application logging"
    )


def test_composite_goal_migration_backfills_only_exact_legacy_links(tmp_path: Path) -> None:
    from neuroagent.infrastructure.persistence.models import (
        AnalysisProposalOutboxRow,
        ConversationMessageRow,
        ConversationRow,
        PlanRevisionRow,
        ProjectRow,
        WorkflowRunRow,
    )

    url = f"sqlite:///{(tmp_path / 'composite-goal-backfill.sqlite').as_posix()}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0027_statistical_result_interpretation_outbox")
    engine = create_engine(url)
    request_hash = "d" * 64
    proposal_hash = "e" * 64
    unverified_proposal_hash = "0" * 64
    analysis_task_id = "work-analysis-" + hashlib.sha256(
        f"legacy-composite-conversation\0analysis\0{proposal_hash}".encode()
    ).hexdigest()[:32]
    unverified_analysis_task_id = "work-analysis-" + hashlib.sha256(
        f"legacy-composite-conversation\0analysis\0{unverified_proposal_hash}".encode()
    ).hexdigest()[:32]
    manifest_hash = "b" * 64
    try:
        with engine.begin() as connection:
            connection.execute(
                ProjectRow.__table__.insert().values(
                    project_id="legacy-composite-project",
                    name="Legacy composite",
                    source_roots_json="[]",
                    work_root="fixture",
                )
            )
            connection.execute(
                ConversationRow.__table__.insert().values(
                    conversation_id="legacy-composite-conversation",
                    mode="work",
                    title="Legacy composite",
                    project_id="legacy-composite-project",
                    version=1,
                    foreground_agent_role="preprocessing",
                    work_tasks_json="{}",
                )
            )
            connection.execute(
                ConversationRow.__table__.insert().values(
                    conversation_id="legacy-unbound-work-conversation",
                    mode="work",
                    title="Legacy projectless Work",
                    project_id=None,
                    version=1,
                    foreground_agent_role="preprocessing",
                    work_tasks_json="{}",
                )
            )
            connection.execute(
                PlanRevisionRow.__table__.insert().values(
                    plan_revision_id="legacy-composite-plan",
                    project_id="legacy-composite-project",
                    revision=1,
                    state="approved",
                    plan_hash="a" * 64,
                    manifest_hash=manifest_hash,
                    environment_hash="c" * 64,
                    plan_json="{}",
                )
            )
            connection.execute(
                WorkflowRunRow.__table__.insert().values(
                    run_id="legacy-composite-run",
                    project_id="legacy-composite-project",
                    plan_revision_id="legacy-composite-plan",
                    state="succeeded",
                )
            )

            def add_task(
                task_id: str,
                intent_id: str,
                *,
                analysis: bool = False,
                task_proposal_hash: str | None = None,
                task_conversation_id: str = "legacy-composite-conversation",
            ) -> None:
                authority = {
                    "conversation_id": task_conversation_id,
                    **(
                        {"input_revision": "a" * 64}
                        if analysis
                        else {
                            "analysis_intent_task_id": intent_id,
                            "analysis_request_hash": request_hash,
                            "plan_revision_id": "legacy-composite-plan",
                            "input_revision": manifest_hash,
                            "workflow_run_id": "legacy-composite-run",
                        }
                    ),
                }
                connection.execute(
                    text(
                        "INSERT INTO work_tasks "
                        "(task_id,status,version,goal_json,waiting_condition_json,"
                        "authority_refs_json,recovery_steps_json,budget_json) "
                        "VALUES (:task_id,'waiting_user',1,'{}',:waiting,:authority,'[]','{}')"
                    ),
                    {
                        "task_id": task_id,
                        "waiting": json.dumps(
                            {"kind": "user_decision", "reference": "review", "reason": "legacy"}
                        ),
                        "authority": json.dumps(authority),
                    },
                )
                step = {
                    "step_id": f"step-{task_id}",
                    "sequence": 0,
                    "kind": "statistical_run" if analysis else "preprocessing_run",
                    "status": "ready",
                    "input_version": (task_proposal_hash or proposal_hash)
                    if analysis
                    else "f" * 64,
                    "authority_refs": authority,
                }
                connection.execute(
                    text(
                        "INSERT INTO work_task_steps "
                        "(task_id,step_id,sequence,status,version,step_json) "
                        "VALUES (:task_id,:step_id,0,'ready',1,:step)"
                    ),
                    {
                        "task_id": task_id,
                        "step_id": step["step_id"],
                        "step": json.dumps(step),
                    },
                )

            add_task("legacy-composite-preprocessing", "legacy-composite-intent")
            add_task(
                analysis_task_id,
                "legacy-composite-intent",
                analysis=True,
                task_proposal_hash=proposal_hash,
            )
            add_task("legacy-unverified-preprocessing", "legacy-unverified-intent")
            add_task(
                unverified_analysis_task_id,
                "legacy-unverified-intent",
                analysis=True,
                task_proposal_hash=unverified_proposal_hash,
            )
            add_task("legacy-ambiguous-preprocessing-1", "legacy-ambiguous-intent")
            add_task("legacy-ambiguous-preprocessing-2", "legacy-ambiguous-intent")
            add_task(
                "legacy-unbound-preprocessing",
                "legacy-unbound-intent",
                task_conversation_id="legacy-unbound-work-conversation",
            )
            connection.execute(
                ConversationMessageRow.__table__.insert().values(
                    message_id="legacy-proposal-message",
                    conversation_id="legacy-composite-conversation",
                    sequence=1,
                    role="assistant",
                    content="proposal",
                    payload_json=json.dumps(
                        {
                            "analysis_outbox_continuation": {
                                "intent_task_id": "legacy-composite-intent"
                            },
                            "statistical_design_proposal": {"proposal_hash": proposal_hash},
                        }
                    ),
                )
            )
            connection.execute(
                ConversationMessageRow.__table__.insert().values(
                    message_id="legacy-unverified-proposal-message",
                    conversation_id="legacy-composite-conversation",
                    sequence=2,
                    role="assistant",
                    content="proposal",
                    payload_json=json.dumps(
                        {
                            "analysis_outbox_continuation": {
                                "intent_task_id": "legacy-unverified-intent"
                            },
                            "statistical_design_proposal": {"proposal_hash": "1" * 64},
                        }
                    ),
                )
            )
            connection.execute(
                AnalysisProposalOutboxRow.__table__.insert(),
                [
                    {
                        "intent_task_id": "legacy-composite-intent",
                        "conversation_id": "legacy-composite-conversation",
                        "project_id": "legacy-composite-project",
                        "preprocessing_task_id": "legacy-composite-preprocessing",
                        "plan_revision_id": "legacy-composite-plan",
                        "manifest_hash": manifest_hash,
                        "run_id": "legacy-composite-run",
                        "analysis_request_hash": request_hash,
                        "artifact_ids_json": "[]",
                        "status": "proposal_ready",
                        "attempt_count": 1,
                        "retry_generation": 0,
                        "fencing_token": 1,
                        "continuation_message_id": "legacy-proposal-message",
                        "analysis_work_task_id": analysis_task_id,
                    },
                    {
                        "intent_task_id": "legacy-unverified-intent",
                        "conversation_id": "legacy-composite-conversation",
                        "project_id": "legacy-composite-project",
                        "preprocessing_task_id": "legacy-unverified-preprocessing",
                        "plan_revision_id": "legacy-composite-plan",
                        "manifest_hash": manifest_hash,
                        "run_id": "legacy-composite-run",
                        "analysis_request_hash": request_hash,
                        "artifact_ids_json": "[]",
                        "status": "proposal_ready",
                        "attempt_count": 1,
                        "retry_generation": 0,
                        "fencing_token": 1,
                        "continuation_message_id": "legacy-unverified-proposal-message",
                        "analysis_work_task_id": unverified_analysis_task_id,
                    },
                ],
            )

        command.upgrade(config, "head")
        with engine.connect() as connection:
            linked = connection.execute(
                text(
                    "SELECT analysis_proposal_outbox_intent_task_id, analysis_work_task_id "
                    "FROM composite_goal_links WHERE intent_task_id='legacy-composite-intent'"
                )
            ).one()
            attempt = connection.execute(
                text(
                    "SELECT attempt_number, work_task_id, proposal_hash "
                    "FROM composite_goal_analysis_attempts "
                    "WHERE intent_task_id='legacy-composite-intent'"
                )
            ).one()
            ambiguous_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM composite_goal_links "
                    "WHERE intent_task_id='legacy-ambiguous-intent'"
                )
            ).scalar_one()
            unverified_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM composite_goal_links "
                    "WHERE intent_task_id='legacy-unverified-intent'"
                )
            ).scalar_one()
            unbound = connection.execute(
                text(
                    "SELECT conversation_id, project_id, preprocessing_task_id "
                    "FROM composite_goal_links WHERE intent_task_id='legacy-unbound-intent'"
                )
            ).one()
            migrated_analysis_refs = json.loads(
                connection.execute(
                    text("SELECT authority_refs_json FROM work_tasks WHERE task_id=:task_id"),
                    {"task_id": analysis_task_id},
                ).scalar_one()
            )
            migrated_step = json.loads(
                connection.execute(
                    text("SELECT step_json FROM work_task_steps WHERE task_id=:task_id"),
                    {"task_id": analysis_task_id},
                ).scalar_one()
            )
        assert tuple(linked) == ("legacy-composite-intent", analysis_task_id)
        assert tuple(attempt) == (1, analysis_task_id, proposal_hash)
        assert ambiguous_count == 0
        assert unverified_count == 0
        assert tuple(unbound) == (
            "legacy-unbound-work-conversation",
            "legacy-composite-project",
            "legacy-unbound-preprocessing",
        )
        assert migrated_analysis_refs["analysis_intent_task_id"] == "legacy-composite-intent"
        assert migrated_analysis_refs["analysis_request_hash"] == request_hash
        assert migrated_step["authority_refs"]["analysis_intent_task_id"] == (
            "legacy-composite-intent"
        )
        assert migrated_step["authority_refs"]["analysis_request_hash"] == request_hash
    finally:
        engine.dispose()


def test_work_task_migration_preserves_legacy_preprocessing_run_and_round_trips(
    tmp_path: Path,
) -> None:
    from neuroagent.infrastructure.persistence.models import (
        PlanRevisionRow,
        ProjectRow,
        WorkflowRunRow,
    )

    url = f"sqlite:///{(tmp_path / 'work-tasks.sqlite').as_posix()}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0020_external_demographics")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                ProjectRow.__table__.insert().values(
                    project_id="legacy-project",
                    name="Legacy Work",
                    source_roots_json="[]",
                    work_root="fixture",
                )
            )
            connection.execute(
                PlanRevisionRow.__table__.insert().values(
                    plan_revision_id="legacy-plan",
                    project_id="legacy-project",
                    revision=1,
                    state="approved",
                    plan_hash="a" * 64,
                    manifest_hash="b" * 64,
                    environment_hash="c" * 64,
                    plan_json="{}",
                )
            )
            connection.execute(
                WorkflowRunRow.__table__.insert().values(
                    run_id="legacy-run",
                    project_id="legacy-project",
                    plan_revision_id="legacy-plan",
                    state="succeeded",
                )
            )
            connection.execute(
                text(
                    "INSERT INTO conversations (conversation_id,mode,title,project_id,"
                    "active_run_id,version,created_at,updated_at) "
                    "VALUES ('legacy-work','work','Old Work','legacy-project','legacy-run',"
                    "1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                )
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT active_run_id,foreground_agent_role,work_tasks_json FROM conversations"
                )
            ).one()
        assert row.active_run_id == "legacy-run"
        assert row.foreground_agent_role == "preprocessing"
        assert json.loads(row.work_tasks_json)["preprocessing"]["run_id"] == "legacy-run"
        command.downgrade(config, "0020_external_demographics")
        assert "work_tasks_json" not in {
            item["name"] for item in inspect(engine).get_columns("conversations")
        }
        command.upgrade(config, "head")
        with engine.connect() as connection:
            tasks = connection.execute(
                text("SELECT work_tasks_json FROM conversations")
            ).scalar_one()
        assert json.loads(tasks)["preprocessing"]["run_id"] == "legacy-run"
    finally:
        engine.dispose()


def test_reconciliation_migration_backfills_only_supported_waiting_targets(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "reconciliation-backfill.sqlite"
    url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0022_persistent_work_tasks")
    engine = create_engine(url)
    legacy_tasks = (
        ("legacy-run-wait", "waiting_run", "workflow_run", "run-1"),
        ("legacy-qc-wait", "waiting_user", "user_decision", "qc:run-2"),
        (
            "legacy-result-wait",
            "waiting_dependency",
            "dependency",
            "statistical-result:run-3",
        ),
        (
            "legacy-preprocessing-dependency",
            "waiting_dependency",
            "dependency",
            "downstream:run-4",
        ),
        ("legacy-generic-dependency", "waiting_dependency", "dependency", "downstream:run-5"),
        ("legacy-choice-wait", "waiting_user", "user_decision", "analysis-proposal:abc"),
    )
    try:
        with engine.begin() as connection:
            for task_id, status, waiting_kind, reference in legacy_tasks:
                run_id = reference.split(":")[-1] if task_id != "legacy-run-wait" else reference
                connection.execute(
                    text(
                        "INSERT INTO work_tasks "
                        "(task_id,status,version,goal_json,waiting_condition_json,"
                        "authority_refs_json,recovery_steps_json,budget_json) "
                        "VALUES (:task_id,:status,1,'{}',:waiting,:authority,'[]','{}')"
                    ),
                    {
                        "task_id": task_id,
                        "status": status,
                        "waiting": json.dumps(
                            {"kind": waiting_kind, "reference": reference, "reason": "legacy"}
                        ),
                        "authority": json.dumps(
                            {
                                "workflow_run_id": run_id,
                                **(
                                    {"plan_revision_id": "plan-4"}
                                    if task_id == "legacy-preprocessing-dependency"
                                    else {}
                                ),
                            }
                        ),
                    },
                )
                if task_id == "legacy-preprocessing-dependency":
                    connection.execute(
                        text(
                            "INSERT INTO work_task_steps "
                            "(task_id,step_id,sequence,status,version,step_json) "
                            "VALUES (:task_id,'preprocessing:plan-4',0,'waiting',1,:step)"
                        ),
                        {
                            "task_id": task_id,
                            "step": json.dumps(
                                {
                                    "step_id": "preprocessing:plan-4",
                                    "sequence": 0,
                                    "kind": "preprocessing_run",
                                    "status": "waiting",
                                    "authority_refs": {
                                        "plan_revision_id": "plan-4",
                                        "workflow_run_id": "run-4",
                                    },
                                }
                            ),
                        },
                    )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            targets = connection.execute(
                text(
                    "SELECT task_id,run_id,target_kind "
                    "FROM work_task_reconciliation_targets ORDER BY task_id"
                )
            ).all()
        assert [tuple(row) for row in targets] == [
            ("legacy-preprocessing-dependency", "run-4", "workflow_run"),
            ("legacy-qc-wait", "run-2", "qc_decision"),
            ("legacy-result-wait", "run-3", "statistical_result"),
            ("legacy-run-wait", "run-1", "workflow_run"),
        ]
    finally:
        engine.dispose()


def test_initial_alembic_migration_creates_metadata_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {
        "alembic_version",
        "projects",
        "datasets",
        "manifest_revisions",
        "plan_revisions",
        "workflow_runs",
        "jobs",
        "runtime_events",
        "model_profiles",
        "agent_tasks",
        "qc_review_revisions",
        "qc_approval_records",
        "statistical_results",
        "conversations",
        "conversation_messages",
        "conversation_tool_calls",
        "confirmation_challenges",
        "papers",
        "paper_sections",
        "literature_chunks",
    }.issubset(tables)


def test_agent_tool_loop_migration_adds_audit_columns_and_round_trips(tmp_path: Path) -> None:
    """0014 must add the loop columns, and its downgrade must be reversible."""

    database_path = tmp_path / "tool-loop.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        columns = {
            column["name"] for column in inspect(engine).get_columns("conversation_tool_calls")
        }
    finally:
        engine.dispose()

    assert {"step_index", "risk_tier", "provider_call_id", "duration_ms"}.issubset(columns)

    # Downgrade must not be lossy for the columns that already existed, and the
    # upgrade must be re-runnable (a broken batch alter leaves SQLite mid-way).
    command.downgrade(config, "0013_optional_qc")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        downgraded = {
            column["name"] for column in inspect(engine).get_columns("conversation_tool_calls")
        }
    finally:
        engine.dispose()
    assert "step_index" not in downgraded
    assert {"tool_call_id", "tool_name", "input_json", "output_json"}.issubset(downgraded)

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        restored = {
            column["name"] for column in inspect(engine).get_columns("conversation_tool_calls")
        }
    finally:
        engine.dispose()
    assert {"step_index", "risk_tier", "provider_call_id", "duration_ms"}.issubset(restored)


def test_analysis_outbox_consumer_migration_preserves_ready_rows_and_message_sequences(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "analysis-outbox-consumer.sqlite"
    url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0025_analysis_proposal_outbox")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO projects "
                    "(project_id,name,source_roots_json,work_root,version,created_at) "
                    "VALUES ('consumer-project','synthetic','[]','fixture',1,CURRENT_TIMESTAMP)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO conversations "
                    "(conversation_id,mode,title,project_id,foreground_agent_role,"
                    "work_tasks_json,version,created_at,updated_at) "
                    "VALUES ('consumer-conversation','work','fixture','consumer-project',"
                    "'preprocessing','{}',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                )
            )
            for sequence, role, content in (
                (1, "user", "原始分析请求"),
                (2, "assistant", "等待预处理完成"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO conversation_messages "
                        "(message_id,conversation_id,sequence,role,content,payload_json,"
                        "created_at) "
                        "VALUES (:message_id,'consumer-conversation',:sequence,:role,:content,'{}',"
                        "CURRENT_TIMESTAMP)"
                    ),
                    {
                        "message_id": f"legacy-message-{sequence}",
                        "sequence": sequence,
                        "role": role,
                        "content": content,
                    },
                )
            connection.execute(
                text(
                    "INSERT INTO analysis_proposal_outbox "
                    "(intent_task_id,conversation_id,project_id,preprocessing_task_id,"
                    "plan_revision_id,manifest_hash,run_id,analysis_request_hash,artifact_ids_json,"
                    "status,fencing_token) VALUES ('legacy-intent','consumer-conversation',"
                    "'consumer-project','prep-task','plan-rev',:manifest,'run-1',:request,'[]',"
                    "'ready',1)"
                ),
                {"manifest": "a" * 64, "request": "b" * 64},
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            messages = connection.execute(
                text(
                    "SELECT sequence,role,continuation_key FROM conversation_messages "
                    "WHERE conversation_id='consumer-conversation' ORDER BY sequence"
                )
            ).all()
            outbox = connection.execute(
                text(
                    "SELECT status,attempt_count,retry_generation,next_attempt_at,"
                    "continuation_message_id FROM analysis_proposal_outbox "
                    "WHERE intent_task_id='legacy-intent'"
                )
            ).one()
        assert [(row.sequence, row.role, row.continuation_key) for row in messages] == [
            (1, "user", None),
            (2, "assistant", None),
        ]
        assert tuple(outbox) == ("ready", 0, 0, None, None)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE analysis_proposal_outbox SET status='waiting_user' "
                        "WHERE intent_task_id='legacy-intent'"
                    )
                )
        except IntegrityError:
            pass
        else:
            raise AssertionError("terminal outbox status must reference an assistant message")
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE analysis_proposal_outbox SET status='waiting_user', "
                        "continuation_message_id='legacy-message-1' "
                        "WHERE intent_task_id='legacy-intent'"
                    )
                )
        except IntegrityError:
            pass
        else:
            raise AssertionError("a user message cannot be an outbox assistant continuation")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO conversation_messages "
                    "(message_id,conversation_id,sequence,role,content,payload_json,created_at) "
                    "VALUES ('continuation-message','consumer-conversation',3,'assistant',"
                    "'analysis draft','{}',CURRENT_TIMESTAMP)"
                )
            )
            connection.execute(
                text(
                    "UPDATE analysis_proposal_outbox SET status='waiting_user', "
                    "continuation_message_id='continuation-message' "
                    "WHERE intent_task_id='legacy-intent'"
                )
            )
            connection.execute(
                text(
                    "UPDATE analysis_proposal_outbox SET status='ready', "
                    "continuation_message_id=NULL WHERE intent_task_id='legacy-intent'"
                )
            )
        try:
            with engine.connect() as connection:
                connection.execute(text("PRAGMA foreign_keys=ON"))
                connection.commit()
                with connection.begin():
                    connection.execute(
                        text(
                            "DELETE FROM conversations "
                            "WHERE conversation_id='consumer-conversation'"
                        )
                    )
        except IntegrityError:
            pass
        else:
            raise AssertionError("outbox evidence must prevent deleting its conversation")
        command.downgrade(config, "0025_analysis_proposal_outbox")
        with engine.connect() as connection:
            columns = {
                item["name"] for item in inspect(engine).get_columns("analysis_proposal_outbox")
            }
            restored = connection.execute(
                text(
                    "SELECT status,artifact_ids_json FROM analysis_proposal_outbox "
                    "WHERE intent_task_id='legacy-intent'"
                )
            ).one()
        assert "attempt_count" not in columns
        assert tuple(restored) == ("ready", "[]")
        command.upgrade(config, "head")
    finally:
        engine.dispose()


def test_two_processes_can_initialize_the_same_new_sqlite_database(tmp_path: Path) -> None:
    database_path = tmp_path / "shared.sqlite"
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    repository_root = Path(__file__).resolve().parents[2]
    script = textwrap.dedent(
        """
        import sys
        import time
        from pathlib import Path

        from neuroagent.infrastructure.persistence.database import Database

        database_url, marker_text = sys.argv[1:]
        marker = Path(marker_text)
        marker.write_text("ready", encoding="utf-8")
        deadline = time.monotonic() + 10
        while len(list(marker.parent.glob("process-*"))) < 2:
            if time.monotonic() >= deadline:
                raise TimeoutError("peer process did not become ready")
            time.sleep(0.01)
        database = Database(database_url)
        try:
            database.initialize()
        finally:
            database.dispose()
        """
    )
    url = f"sqlite:///{database_path.as_posix()}"
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, url, str(ready_dir / f"process-{index}")],
            cwd=repository_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]
    try:
        results = [process.communicate(timeout=30) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    for process, (stdout, stderr) in zip(processes, results, strict=True):
        assert process.returncode == 0, f"stdout={stdout}\nstderr={stderr}"

    engine = create_engine(url)
    try:
        assert "alembic_version" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
