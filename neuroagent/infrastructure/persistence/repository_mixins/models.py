"""Model profiles and Agent task results."""

from __future__ import annotations

from sqlalchemy import select

from neuroagent.agent.models import GatewayResult
from neuroagent.application.contracts import AgentTaskView, ModelProfileInput, ModelProfileView
from neuroagent.application.errors import ConflictError, NotFoundError
from neuroagent.application.hashing import canonical_json
from neuroagent.infrastructure.persistence.models import (
    AgentTaskRow,
    ModelProfileRow,
    ProjectRow,
)
from neuroagent.infrastructure.persistence.repository_mixins._base import (
    RepositoryBaseMixin,
    _as_utc,
    _id,
    _load,
)


class ModelAgentMixin(RepositoryBaseMixin):
    # -- model profiles and Agent results ----------------------------------

    def create_model_profile(self, profile: ModelProfileInput) -> ModelProfileView:
        with self._write_session() as session:
            if session.get(ModelProfileRow, profile.id) is not None:
                raise ConflictError(
                    "model_profile_exists",
                    "同名模型配置已存在; 模型配置是不可变资源。",
                    profile_id=profile.id,
                )
            row = ModelProfileRow(
                profile_id=profile.id,
                profile_json=canonical_json(profile.model_dump(mode="json")),
                version=1,
            )
            session.add(row)
            session.flush()
            return self._model_profile(row)

    def get_model_profile(self, profile_id: str) -> ModelProfileView:
        with self.database.session_factory() as session:
            row = session.get(ModelProfileRow, profile_id)
            if row is None:
                raise NotFoundError("model_profile", profile_id)
            return self._model_profile(row)

    def list_model_profiles(self) -> list[ModelProfileView]:
        with self.database.session_factory() as session:
            rows = session.scalars(
                select(ModelProfileRow).order_by(ModelProfileRow.profile_id)
            ).all()
            return [self._model_profile(row) for row in rows]

    def delete_model_profile(self, profile_id: str) -> None:
        with self._write_session() as session:
            row = session.get(ModelProfileRow, profile_id)
            if row is None:
                raise NotFoundError("model_profile", profile_id)
            session.delete(row)

    def create_agent_task(
        self,
        *,
        project_id: str,
        expected_project_version: int,
        task_type: str,
        result: GatewayResult,
    ) -> AgentTaskView:
        with self._write_session() as session:
            project = session.get(ProjectRow, project_id)
            if project is None:
                raise NotFoundError("project", project_id)
            self._check_version("project", project.version, expected_project_version)
            row = AgentTaskRow(
                task_id=_id(),
                project_id=project_id,
                state="succeeded",
                task_type=task_type,
                context_hash=result.context_hash,
                result_json=canonical_json(result.model_dump(mode="json")),
            )
            session.add(row)
            session.flush()
            return self._agent_task(row)

    def get_agent_task(self, task_id: str) -> AgentTaskView:
        with self.database.session_factory() as session:
            row = session.get(AgentTaskRow, task_id)
            if row is None:
                raise NotFoundError("agent_task", task_id)
            return self._agent_task(row)

    @staticmethod
    def _model_profile(row: ModelProfileRow) -> ModelProfileView:
        return ModelProfileView(
            profile=ModelProfileInput.model_validate(_load(row.profile_json, {})),
            version=row.version,
            created_at=_as_utc(row.created_at),
        )

    @staticmethod
    def _agent_task(row: AgentTaskRow) -> AgentTaskView:
        return AgentTaskView(
            task_id=row.task_id,
            project_id=row.project_id,
            state=row.state,
            result=GatewayResult.model_validate(_load(row.result_json, {})),
            created_at=_as_utc(row.created_at),
        )
