"""Project, dataset, manifest, demographics, and split revisions."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import func, select

from neuroagent.application.contracts import (
    DatasetSplitView,
    DatasetView,
    DemographicsRevisionView,
    ManifestRevisionView,
    ProjectView,
)
from neuroagent.application.errors import ConflictError, NotFoundError
from neuroagent.application.hashing import canonical_json, content_hash
from neuroagent.infrastructure.persistence.models import (
    DatasetRow,
    DatasetSplitRevisionRow,
    DemographicsRevisionRow,
    ManifestRevisionRow,
    ProjectRow,
)
from neuroagent.infrastructure.persistence.repository_mixins._base import (
    RepositoryBaseMixin,
    _id,
    _load,
)


class ProjectDatasetMixin(RepositoryBaseMixin):
    # -- projects and datasets ----------------------------------------------

    def create_project(self, name: str, source_roots: list[str], work_root: str) -> ProjectView:
        with self._write_session() as session:
            row = ProjectRow(
                project_id=_id(),
                name=name,
                source_roots_json=canonical_json(source_roots),
                work_root=work_root,
            )
            session.add(row)
            session.flush()
            return self._project(row)

    def get_project(self, project_id: str) -> ProjectView:
        with self.database.session_factory() as session:
            row = session.get(ProjectRow, project_id)
            if row is None:
                raise NotFoundError("project", project_id)
            return self._project(row)

    def list_projects(self) -> list[ProjectView]:
        with self.database.session_factory() as session:
            rows = session.scalars(select(ProjectRow).order_by(ProjectRow.created_at)).all()
            return [self._project(row) for row in rows]

    def create_dataset(
        self,
        project_id: str,
        *,
        name: str,
        source_path: str,
        expected_project_version: int,
    ) -> DatasetView:
        with self._write_session() as session:
            project = session.get(ProjectRow, project_id)
            if project is None:
                raise NotFoundError("project", project_id)
            if project.version != expected_project_version:
                raise ConflictError(
                    "revision_conflict",
                    "项目版本已变化, 请刷新后重试。",
                    expected=expected_project_version,
                    actual=project.version,
                )
            project.version += 1
            row = DatasetRow(
                dataset_id=_id(),
                project_id=project_id,
                name=name,
                source_path=source_path,
            )
            session.add(row)
            session.flush()
            return self._dataset(row)

    def get_dataset(self, dataset_id: str) -> DatasetView:
        with self.database.session_factory() as session:
            row = session.get(DatasetRow, dataset_id)
            if row is None:
                raise NotFoundError("dataset", dataset_id)
            return self._dataset(row)

    def create_manifest(
        self, dataset_id: str, *, expected_version: int, content: dict[str, Any]
    ) -> ManifestRevisionView:
        with self._write_session() as session:
            dataset = session.get(DatasetRow, dataset_id)
            if dataset is None:
                raise NotFoundError("dataset", dataset_id)
            self._check_version("dataset", dataset.version, expected_version)
            revision = (
                session.scalar(
                    select(func.max(ManifestRevisionRow.revision)).where(
                        ManifestRevisionRow.dataset_id == dataset_id
                    )
                )
                or 0
            ) + 1
            row = ManifestRevisionRow(
                manifest_id=_id(),
                dataset_id=dataset_id,
                revision=revision,
                content_hash=content_hash(content),
                content_json=canonical_json(content),
            )
            session.add(row)
            session.flush()
            dataset.current_manifest_id = row.manifest_id
            dataset.version += 1
            return self._manifest(row)

    def get_manifest(self, manifest_id: str) -> ManifestRevisionView:
        with self.database.session_factory() as session:
            row = session.get(ManifestRevisionRow, manifest_id)
            if row is None:
                raise NotFoundError("manifest", manifest_id)
            return self._manifest(row)

    def get_manifest_content(self, manifest_id: str) -> dict[str, Any]:
        with self.database.session_factory() as session:
            row = session.get(ManifestRevisionRow, manifest_id)
            if row is None:
                raise NotFoundError("manifest", manifest_id)
            return cast(dict[str, Any], _load(row.content_json, {}))

    def create_demographics(
        self, dataset_id: str, *, expected_version: int, content: dict[str, Any]
    ) -> DemographicsRevisionView:
        with self._write_session() as session:
            dataset = session.get(DatasetRow, dataset_id)
            if dataset is None:
                raise NotFoundError("dataset", dataset_id)
            self._check_version("dataset", dataset.version, expected_version)
            revision = (
                session.scalar(
                    select(func.max(DemographicsRevisionRow.revision)).where(
                        DemographicsRevisionRow.dataset_id == dataset_id
                    )
                )
                or 0
            ) + 1
            row = DemographicsRevisionRow(
                demographics_id=_id(),
                dataset_id=dataset_id,
                revision=revision,
                content_hash=content_hash(content),
                content_json=canonical_json(content),
            )
            session.add(row)
            dataset.version += 1
            session.flush()
            return self._demographics(row)

    def get_demographics_content(self, demographics_id: str) -> tuple[str, dict[str, Any]]:
        with self.database.session_factory() as session:
            row = session.get(DemographicsRevisionRow, demographics_id)
            if row is None:
                raise NotFoundError("demographics_revision", demographics_id)
            return row.dataset_id, _load(row.content_json, {})

    def get_demographics(self, demographics_id: str) -> DemographicsRevisionView:
        with self.database.session_factory() as session:
            row = session.get(DemographicsRevisionRow, demographics_id)
            if row is None:
                raise NotFoundError("demographics_revision", demographics_id)
            return self._demographics(row)

    def create_split(
        self, dataset_id: str, *, expected_version: int, content: dict[str, Any]
    ) -> DatasetSplitView:
        with self._write_session() as session:
            dataset = session.get(DatasetRow, dataset_id)
            if dataset is None:
                raise NotFoundError("dataset", dataset_id)
            self._check_version("dataset", dataset.version, expected_version)
            revision = (
                session.scalar(
                    select(func.max(DatasetSplitRevisionRow.revision)).where(
                        DatasetSplitRevisionRow.dataset_id == dataset_id
                    )
                )
                or 0
            ) + 1
            row = DatasetSplitRevisionRow(
                split_id=_id(),
                dataset_id=dataset_id,
                revision=revision,
                content_hash=content_hash(content),
                content_json=canonical_json(content),
            )
            session.add(row)
            dataset.version += 1
            session.flush()
            return self._split(row)

    def get_split(self, split_id: str) -> DatasetSplitView:
        with self.database.session_factory() as session:
            row = session.get(DatasetSplitRevisionRow, split_id)
            if row is None:
                raise NotFoundError("dataset_split_revision", split_id)
            return self._split(row)
