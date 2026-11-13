"""Append-only runtime event storage."""

from __future__ import annotations

from sqlalchemy import select

from neuroagent.application.contracts.runs import RuntimeEventView
from neuroagent.application.hashing import canonical_json
from neuroagent.infrastructure.persistence.models import RuntimeEventRow
from neuroagent.infrastructure.persistence.repository_mixins._base import (
    RepositoryBaseMixin,
    _as_utc,
    _load,
)
from neuroagent.observability.events import redact_event_payload
from neuroagent.observability.tracing import current_trace_id


class RuntimeEventMixin(RepositoryBaseMixin):
    """Persist redacted event records independently from artifact metadata."""

    def append_event(
        self,
        *,
        project_id: str | None,
        run_id: str | None,
        event_type: str,
        severity: str,
        payload: dict[str, object],
    ) -> None:
        with self._write_session() as session:
            session.add(
                RuntimeEventRow(
                    trace_id=current_trace_id(),
                    project_id=project_id,
                    run_id=run_id,
                    event_type=event_type,
                    severity=severity,
                    payload_json=canonical_json(redact_event_payload(payload)),
                )
            )

    def list_events(
        self,
        run_id: str | None = None,
        after_event_id: int = 0,
        *,
        project_id: str | None = None,
        limit: int | None = None,
    ) -> list[RuntimeEventView]:
        with self._read_session() as session:
            query = select(RuntimeEventRow).where(RuntimeEventRow.event_id > after_event_id)
            if run_id is not None:
                query = query.where(RuntimeEventRow.run_id == run_id)
            if project_id is not None:
                query = query.where(RuntimeEventRow.project_id == project_id)
            ordered = query.order_by(RuntimeEventRow.event_id)
            if limit is not None:
                ordered = ordered.limit(max(limit, 0))
            rows = session.scalars(ordered).all()
            return [
                RuntimeEventView(
                    event_id=row.event_id,
                    trace_id=row.trace_id,
                    project_id=row.project_id,
                    run_id=row.run_id,
                    event_type=row.event_type,
                    severity=row.severity,
                    payload=_load(row.payload_json, {}),
                    created_at=_as_utc(row.created_at),
                )
                for row in rows
            ]
