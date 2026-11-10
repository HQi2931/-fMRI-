from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from neuroagent.application.automation.model_budget import (
    CompositeGoalModelCallDecisionStatus,
    CompositeGoalModelCallState,
)
from neuroagent.application.automation.models import (
    AuthorityReferences,
    TaskBudget,
    TaskStep,
    WorkGoal,
    WorkTask,
)
from neuroagent.application.errors import ConflictError
from neuroagent.infrastructure.persistence.automation_repository import (
    SqliteWorkTaskRepository,
)
from neuroagent.infrastructure.persistence.database import Database
from neuroagent.infrastructure.persistence.models import (
    ConversationRow,
    PlanRevisionRow,
    ProjectRow,
)
from neuroagent.infrastructure.persistence.unit_of_work import SqliteUnitOfWork


def _open_repository(database_path) -> tuple[Database, SqliteWorkTaskRepository]:
    database = Database(f"sqlite:///{database_path.as_posix()}")
    database.initialize()
    repository = SqliteWorkTaskRepository(database, unit_of_work=SqliteUnitOfWork(database))
    return database, repository


def _create_goal(
    repository: SqliteWorkTaskRepository,
    database: Database,
    *,
    intent_task_id: str,
    max_steps: int = 10,
    max_model_calls: int = 8,
    max_tokens: int = 5000,
    max_elapsed_seconds: int = 600,
) -> TaskBudget:
    project_id = f"project-{intent_task_id}"
    conversation_id = f"conversation-{intent_task_id}"
    plan_revision_id = f"plan-{intent_task_id}"
    manifest_hash = "d" * 64
    with database.session_factory.begin() as session:
        session.add(
            ProjectRow(
                project_id=project_id,
                name="Synthetic budget project",
                source_roots_json="[]",
                work_root="synthetic-work-root",
                version=1,
            )
        )
        session.flush()
        session.add(
            ConversationRow(
                conversation_id=conversation_id,
                mode="work",
                title="Synthetic budget conversation",
                project_id=project_id,
                version=1,
            )
        )
        session.flush()
        session.add(
            PlanRevisionRow(
                plan_revision_id=plan_revision_id,
                project_id=project_id,
                revision=1,
                version=1,
                state="draft",
                plan_hash="e" * 64,
                manifest_hash=manifest_hash,
                environment_hash="f" * 64,
                plan_json="{}",
                validation_issues_json="[]",
            )
        )

    refs = AuthorityReferences(
        conversation_id=conversation_id,
        analysis_intent_task_id=intent_task_id,
        analysis_request_hash="a" * 64,
        plan_revision_id=plan_revision_id,
        input_revision=manifest_hash,
    )
    budget = TaskBudget(
        max_steps=max_steps,
        max_model_calls=max_model_calls,
        max_tokens=max_tokens,
        max_elapsed_seconds=max_elapsed_seconds,
    )
    task = WorkTask(
        task_id=f"preprocessing-{intent_task_id}",
        goal=WorkGoal(
            description="Complete the requested synthetic fMRI workflow",
            completion_conditions=("the exact preprocessing evidence is verified",),
        ),
        authority_refs=refs,
        steps=(
            TaskStep(
                step_id=f"step-{intent_task_id}",
                sequence=0,
                kind="preprocessing_run",
                authority_refs=refs,
            ),
        ),
        budget=budget,
    )
    repository.create_task(task)
    return budget


def test_new_composite_goal_initializes_budget_from_preprocessing_task(tmp_path) -> None:
    database, repository = _open_repository(tmp_path / "new-goal-budget.sqlite")
    expected = _create_goal(
        repository,
        database,
        intent_task_id="budget-new-goal",
        max_steps=13,
        max_model_calls=7,
        max_tokens=4321,
        max_elapsed_seconds=789,
    )

    budget = repository.get_composite_goal_model_budget("budget-new-goal")
    goal = repository.find_composite_goal("budget-new-goal")

    assert goal is not None
    assert goal.intent_task_id == "budget-new-goal"
    assert budget.max_steps == expected.max_steps
    assert budget.max_model_calls == expected.max_model_calls
    assert budget.max_tokens == expected.max_tokens
    assert budget.max_elapsed_seconds == expected.max_elapsed_seconds
    assert budget.used_steps == budget.used_model_calls == budget.used_tokens == 0
    assert budget.elapsed_seconds == 0
    database.dispose()


def test_model_budget_reservations_are_isolated_by_intent(tmp_path) -> None:
    database, repository = _open_repository(tmp_path / "goal-budget-isolation.sqlite")
    _create_goal(repository, database, intent_task_id="budget-goal-a")
    _create_goal(repository, database, intent_task_id="budget-goal-b")

    reserved = repository.reserve_composite_goal_model_call(
        "budget-goal-a",
        reservation_id="shared-reservation-id",
        call_id="call-a",
        estimated_tokens=120,
    )
    isolated_reservation = repository.reserve_composite_goal_model_call(
        "budget-goal-b",
        reservation_id="shared-reservation-id",
        call_id="call-b",
        estimated_tokens=80,
    )

    assert reserved.status is CompositeGoalModelCallDecisionStatus.RESERVED
    assert reserved.budget.used_steps == reserved.budget.used_model_calls == 1
    assert reserved.budget.used_tokens == 120
    assert isolated_reservation.status is CompositeGoalModelCallDecisionStatus.RESERVED
    assert isolated_reservation.budget.used_tokens == 80
    isolated = repository.get_composite_goal_model_budget("budget-goal-b")
    assert isolated.used_steps == isolated.used_model_calls == 1
    assert isolated.used_tokens == 80
    assert repository.get_composite_goal_model_budget("budget-goal-a").used_tokens == 120
    database.dispose()


@pytest.mark.parametrize(
    ("limits", "estimated_tokens", "settle_first"),
    [
        ({"max_steps": 1}, 1, False),
        ({"max_model_calls": 0}, 1, False),
        ({"max_tokens": 5}, 6, False),
        ({"max_elapsed_seconds": 1}, 1, True),
    ],
)
def test_reservation_fails_closed_when_any_goal_budget_is_exhausted(
    tmp_path, limits, estimated_tokens: int, settle_first: bool
) -> None:
    database, repository = _open_repository(
        tmp_path / f"goal-budget-exhausted-{len(limits)}-{estimated_tokens}.sqlite"
    )
    _create_goal(repository, database, intent_task_id="budget-exhausted", **limits)
    if settle_first or "max_steps" in limits:
        first = repository.reserve_composite_goal_model_call(
            "budget-exhausted",
            reservation_id="reservation-first",
            call_id="call-first",
            estimated_tokens=1,
        )
        assert first.status is CompositeGoalModelCallDecisionStatus.RESERVED
    if settle_first:
        repository.settle_composite_goal_model_call(
            "budget-exhausted",
            reservation_id="reservation-first",
            actual_tokens=1,
            elapsed_seconds=1,
        )

    result = repository.reserve_composite_goal_model_call(
        "budget-exhausted",
        reservation_id="reservation-blocked",
        call_id="call-blocked",
        estimated_tokens=estimated_tokens,
    )

    assert result.status is CompositeGoalModelCallDecisionStatus.EXHAUSTED
    assert result.reservation.state is CompositeGoalModelCallState.EXHAUSTED
    assert result.reserved_tokens == 0
    database.dispose()


def test_call_id_replay_never_reserves_or_dispatches_a_second_call(tmp_path) -> None:
    database, repository = _open_repository(tmp_path / "goal-budget-replay.sqlite")
    _create_goal(repository, database, intent_task_id="budget-replay")
    first = repository.reserve_composite_goal_model_call(
        "budget-replay",
        reservation_id="reservation-original",
        call_id="provider-call-1",
        estimated_tokens=220,
    )
    replay = repository.reserve_composite_goal_model_call(
        "budget-replay",
        reservation_id="reservation-retry-id",
        call_id="provider-call-1",
        estimated_tokens=220,
    )

    assert first.status is CompositeGoalModelCallDecisionStatus.RESERVED
    assert replay.status is CompositeGoalModelCallDecisionStatus.DUPLICATE
    assert replay.reservation_id == "reservation-original"
    assert replay.reserved_tokens == 220
    assert replay.budget.used_steps == replay.budget.used_model_calls == 1
    assert replay.budget.used_tokens == 220
    database.dispose()


def test_settlement_adjusts_actual_usage_and_is_idempotent(tmp_path) -> None:
    database, repository = _open_repository(tmp_path / "goal-budget-settlement.sqlite")
    _create_goal(repository, database, intent_task_id="budget-settlement")
    repository.reserve_composite_goal_model_call(
        "budget-settlement",
        reservation_id="reservation-settle",
        call_id="provider-call-settle",
        estimated_tokens=300,
        estimated_elapsed_seconds=20,
    )
    reserved_budget = repository.get_composite_goal_model_budget("budget-settlement")
    assert reserved_budget.elapsed_seconds == 20

    settled = repository.settle_composite_goal_model_call(
        "budget-settlement",
        reservation_id="reservation-settle",
        actual_tokens=245,
        elapsed_seconds=4,
    )
    replay = repository.settle_composite_goal_model_call(
        "budget-settlement",
        reservation_id="reservation-settle",
        actual_tokens=245,
        elapsed_seconds=4,
    )

    assert settled.reservation.state is CompositeGoalModelCallState.SETTLED
    assert settled.reservation.reserved_elapsed_seconds == 20
    assert settled.budget.used_tokens == 245
    assert settled.budget.elapsed_seconds == 4
    assert replay == settled
    with pytest.raises(ConflictError, match="拒绝重复记账"):
        repository.settle_composite_goal_model_call(
            "budget-settlement",
            reservation_id="reservation-settle",
            actual_tokens=250,
            elapsed_seconds=4,
        )
    database.dispose()


def test_inflight_elapsed_reservation_survives_repository_restart(tmp_path) -> None:
    path = tmp_path / "goal-budget-inflight-time.sqlite"
    database, repository = _open_repository(path)
    _create_goal(
        repository,
        database,
        intent_task_id="budget-inflight-time",
        max_elapsed_seconds=5,
    )
    reserved = repository.reserve_composite_goal_model_call(
        "budget-inflight-time",
        reservation_id="reservation-crash",
        call_id="provider-call-crash",
        estimated_tokens=100,
        estimated_elapsed_seconds=5,
    )
    assert reserved.reservation.reserved_elapsed_seconds == 5
    assert reserved.budget.elapsed_seconds == 5
    database.dispose()

    restarted_database, restarted_repository = _open_repository(path)
    budget_after_restart = restarted_repository.get_composite_goal_model_budget(
        "budget-inflight-time"
    )
    assert budget_after_restart.remaining_elapsed_seconds == 0
    exhausted = restarted_repository.reserve_composite_goal_model_call(
        "budget-inflight-time",
        reservation_id="reservation-after-crash",
        call_id="provider-call-after-crash",
        estimated_tokens=100,
        estimated_elapsed_seconds=1,
    )
    assert exhausted.status is CompositeGoalModelCallDecisionStatus.EXHAUSTED
    assert exhausted.budget.elapsed_seconds == 5
    restarted_database.dispose()


def test_missing_provider_usage_keeps_estimate_and_still_charges_elapsed(tmp_path) -> None:
    database, repository = _open_repository(tmp_path / "goal-budget-missing-usage.sqlite")
    _create_goal(repository, database, intent_task_id="budget-missing-usage")
    repository.reserve_composite_goal_model_call(
        "budget-missing-usage",
        reservation_id="reservation-missing-usage",
        call_id="provider-call-error",
        estimated_tokens=180,
    )

    settled = repository.settle_composite_goal_model_call(
        "budget-missing-usage",
        reservation_id="reservation-missing-usage",
        actual_tokens=None,
        elapsed_seconds=3,
    )

    assert settled.reservation.actual_tokens is None
    assert settled.budget.used_tokens == 180
    assert settled.budget.used_model_calls == settled.budget.used_steps == 1
    assert settled.budget.elapsed_seconds == 3
    database.dispose()


def test_concurrent_reservations_cannot_overdraw_goal_call_budget(tmp_path) -> None:
    database, repository = _open_repository(tmp_path / "goal-budget-concurrent.sqlite")
    _create_goal(
        repository,
        database,
        intent_task_id="budget-concurrent",
        max_steps=1,
        max_model_calls=1,
        max_tokens=100,
    )

    def reserve(index: int):
        return repository.reserve_composite_goal_model_call(
            "budget-concurrent",
            reservation_id=f"reservation-{index}",
            call_id=f"provider-call-{index}",
            estimated_tokens=50,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(reserve, (1, 2)))

    assert sorted(result.status.value for result in results) == ["exhausted", "reserved"]
    budget = repository.get_composite_goal_model_budget("budget-concurrent")
    assert budget.used_steps == budget.used_model_calls == 1
    assert budget.used_tokens == 50
    database.dispose()


def test_migration_backfills_budget_caps_from_existing_preprocessing_task(tmp_path) -> None:
    path = tmp_path / "budget-backfill.sqlite"
    database, repository = _open_repository(path)
    expected = _create_goal(
        repository,
        database,
        intent_task_id="budget-backfill",
        max_steps=17,
        max_model_calls=6,
        max_tokens=8765,
        max_elapsed_seconds=901,
    )
    database.dispose()

    # Reconstruct a real historical schema, including later confirmation columns,
    # rather than stamping a current database as though those migrations never ran.
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    command.downgrade(config, "0030_composite_goal_source_fanout")

    migrated_database = Database(f"sqlite:///{path.as_posix()}")
    migrated_database.initialize()
    migrated_repository = SqliteWorkTaskRepository(
        migrated_database, unit_of_work=SqliteUnitOfWork(migrated_database)
    )
    budget = migrated_repository.get_composite_goal_model_budget("budget-backfill")

    assert budget.max_steps == expected.max_steps
    assert budget.max_model_calls == expected.max_model_calls
    assert budget.max_tokens == expected.max_tokens
    assert budget.max_elapsed_seconds == expected.max_elapsed_seconds
    assert budget.used_steps == budget.used_model_calls == budget.used_tokens == 0
    assert budget.elapsed_seconds == 0
    migrated_database.dispose()


def test_elapsed_budget_migration_preserves_legacy_inflight_reservations(tmp_path) -> None:
    path = tmp_path / "budget-elapsed-migration.sqlite"
    database, repository = _open_repository(path)
    _create_goal(repository, database, intent_task_id="budget-elapsed-migration")
    database.dispose()

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    command.downgrade(config, "0031_composite_goal_model_budgets")
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE composite_goal_model_budgets "
                    "SET used_steps=1, used_model_calls=1, used_tokens=90 "
                    "WHERE intent_task_id='budget-elapsed-migration'"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO composite_goal_model_call_reservations "
                    "(reservation_id, intent_task_id, call_id, state, estimated_tokens, "
                    "reserved_tokens, actual_tokens, elapsed_seconds) "
                    "VALUES ('legacy-reservation', 'budget-elapsed-migration', "
                    "'legacy-call', 'reserved', 90, 90, NULL, 0)"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.connect() as connection:
            reservation = (
                connection.execute(
                    text(
                        "SELECT state, reserved_tokens, reserved_elapsed_seconds "
                        "FROM composite_goal_model_call_reservations "
                        "WHERE intent_task_id='budget-elapsed-migration' "
                        "AND reservation_id='legacy-reservation'"
                    )
                )
                .mappings()
                .one()
            )
    finally:
        engine.dispose()

    assert reservation["state"] == CompositeGoalModelCallState.RESERVED.value
    assert reservation["reserved_tokens"] == 90
    assert reservation["reserved_elapsed_seconds"] == 0
