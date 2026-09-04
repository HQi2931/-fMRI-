"""Idempotency request reservation, completion, and lease renewal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from pydantic import BaseModel
from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from neuroagent.application.errors import ConflictError
from neuroagent.application.hashing import canonical_json
from neuroagent.infrastructure.persistence.models import IdempotencyRow
from neuroagent.infrastructure.persistence.repository_mixins._base import (
    RepositoryBaseMixin,
    _as_utc,
    _id,
    _load,
)


class IdempotencyMixin(RepositoryBaseMixin):
    # -- idempotency ---------------------------------------------------------

    def begin_idempotent_request(
        self,
        scope: str,
        key: str,
        request_hash: str,
        owner_token: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        with self._write_session() as session:
            record_id = _id()
            inserted = cast(
                CursorResult[Any],
                session.execute(
                    sqlite_insert(IdempotencyRow)
                    .values(
                        record_id=record_id,
                        scope=scope,
                        idempotency_key=key,
                        request_hash=request_hash,
                        status="pending",
                        response_json=None,
                        owner_token=owner_token,
                        lease_expires_at=lease_expires_at,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            IdempotencyRow.scope,
                            IdempotencyRow.idempotency_key,
                        ]
                    )
                ),
            )
            if inserted.rowcount == 1:
                return None
            row = session.scalar(
                select(IdempotencyRow).where(
                    IdempotencyRow.scope == scope,
                    IdempotencyRow.idempotency_key == key,
                )
            )
            if row is None:
                raise ConflictError(
                    "idempotency_race", "幂等请求发生并发冲突, 请重试。", scope=scope
                )
            if row.request_hash != request_hash:
                raise ConflictError(
                    "idempotency_key_reused",
                    "同一 Idempotency-Key 不能用于不同请求。",
                    scope=scope,
                )
            if row.status == "completed" and row.response_json is not None:
                return cast(dict[str, Any], _load(row.response_json, {}))
            if row.status != "pending":
                raise ConflictError(
                    "idempotency_request_in_progress",
                    "相同写请求仍在处理中, 请稍后使用同一 Idempotency-Key 重试。",
                    scope=scope,
                )
            if row.lease_expires_at is not None and _as_utc(row.lease_expires_at) > now:
                raise ConflictError(
                    "idempotency_request_in_progress",
                    "相同写请求仍在处理中, 请稍后使用同一 Idempotency-Key 重试。",
                    scope=scope,
                )
            row.owner_token = owner_token
            row.lease_expires_at = lease_expires_at
            row.updated_at = now
            row.response_json = None
            session.flush()
            return None

    def complete_idempotent_request(
        self,
        scope: str,
        key: str,
        request_hash: str,
        owner_token: str,
        response: BaseModel,
    ) -> None:
        with self._write_session() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(IdempotencyRow)
                    .where(
                        IdempotencyRow.scope == scope,
                        IdempotencyRow.idempotency_key == key,
                        IdempotencyRow.request_hash == request_hash,
                        IdempotencyRow.status == "pending",
                        IdempotencyRow.owner_token == owner_token,
                    )
                    .values(
                        status="completed",
                        response_json=canonical_json(response.model_dump(mode="json")),
                        lease_expires_at=None,
                        updated_at=datetime.now(UTC),
                    )
                ),
            )
            if result.rowcount != 1:
                raise ConflictError(
                    "idempotency_completion_conflict",
                    "无法完成幂等请求记录, 写操作结果需要人工核对。",
                    scope=scope,
                )

    def renew_idempotent_request(
        self,
        scope: str,
        key: str,
        request_hash: str,
        owner_token: str,
        lease_seconds: int,
    ) -> bool:
        now = datetime.now(UTC)
        with self._write_session() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(IdempotencyRow)
                    .where(
                        IdempotencyRow.scope == scope,
                        IdempotencyRow.idempotency_key == key,
                        IdempotencyRow.request_hash == request_hash,
                        IdempotencyRow.status == "pending",
                        IdempotencyRow.owner_token == owner_token,
                    )
                    .values(
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        updated_at=now,
                    )
                ),
            )
            return result.rowcount == 1

    def release_idempotent_request(
        self, scope: str, key: str, request_hash: str, owner_token: str
    ) -> None:
        with self._write_session() as session:
            session.execute(
                delete(IdempotencyRow).where(
                    IdempotencyRow.scope == scope,
                    IdempotencyRow.idempotency_key == key,
                    IdempotencyRow.request_hash == request_hash,
                    IdempotencyRow.status == "pending",
                    IdempotencyRow.owner_token == owner_token,
                )
            )

    def get_idempotent_response(
        self, scope: str, key: str, request_hash: str
    ) -> dict[str, Any] | None:
        """Compatibility read used by diagnostics; it never reserves a key."""
        with self.database.session_factory() as session:
            row = session.scalar(
                select(IdempotencyRow).where(
                    IdempotencyRow.scope == scope,
                    IdempotencyRow.idempotency_key == key,
                    IdempotencyRow.request_hash == request_hash,
                    IdempotencyRow.status == "completed",
                )
            )
            if row is None or row.response_json is None:
                return None
            return cast(dict[str, Any], _load(row.response_json, {}))
