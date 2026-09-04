"""Project, dataset, manifest, demographics, and split use cases."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Any

from neuroagent.application.contracts import (
    DatasetCreate,
    DatasetSplitCreate,
    DatasetSplitView,
    DatasetView,
    DemographicsImportRequest,
    DemographicsRevisionView,
    ManifestRevisionView,
    ManifestScanRequest,
    ProjectCreate,
    ProjectView,
)
from neuroagent.application.errors import ConflictError, InputValidationError
from neuroagent.application.service_mixins._base import BaseServiceMixin


class ProjectDatasetMixin(BaseServiceMixin):
    def create_project(self, request: ProjectCreate, idempotency_key: str) -> ProjectView:
        def prepare() -> tuple[list[str], str]:
            roots = [
                str(self.path_policy.validate_project_source_root(path))
                for path in request.source_roots
            ]
            work_root = str(self.path_policy.validate_work_root(request.work_root))
            return roots, work_root

        def finalize(prepared: tuple[list[str], str]) -> ProjectView:
            roots, work_root = prepared
            result = self.repository.create_project(request.name, roots, work_root)
            self.repository.append_event(
                project_id=result.project_id,
                run_id=None,
                event_type="ProjectCreated",
                severity="info",
                payload={"version": result.version, "source_root_count": len(roots)},
            )
            return result

        return self._idempotent_prepared(
            scope="projects:create",
            key=idempotency_key,
            request=request,
            response_type=ProjectView,
            prepare=prepare,
            finalize=finalize,
        )

    def list_projects(self) -> list[ProjectView]:
        return self.repository.list_projects()

    def get_project(self, project_id: str) -> ProjectView:
        return self.repository.get_project(project_id)

    def create_dataset(
        self, project_id: str, request: DatasetCreate, idempotency_key: str
    ) -> DatasetView:
        def prepare() -> str:
            project = self.repository.get_project(project_id)
            source = self.path_policy.validate_read_path(
                request.source_path,
                project_roots=project.source_roots,
                expect_directory=True,
            )
            return str(source)

        def finalize(source_path: str) -> DatasetView:
            result = self.repository.create_dataset(
                project_id,
                name=request.name,
                source_path=source_path,
                expected_project_version=request.expected_project_version,
            )
            self.repository.append_event(
                project_id=project_id,
                run_id=None,
                event_type="DatasetRegistered",
                severity="info",
                payload={"dataset_id": result.dataset_id, "version": result.version},
            )
            return result

        return self._idempotent_prepared(
            scope=f"projects:{project_id}:datasets:create",
            key=idempotency_key,
            request=request,
            response_type=DatasetView,
            prepare=prepare,
            finalize=finalize,
        )

    def get_dataset(self, dataset_id: str) -> DatasetView:
        return self.repository.get_dataset(dataset_id)

    def inspect_dataset(
        self, dataset_id: str, request: ManifestScanRequest, idempotency_key: str
    ) -> ManifestRevisionView:
        def prepare() -> tuple[str, dict[str, Any]]:
            dataset = self.repository.get_dataset(dataset_id)
            project = self.repository.get_project(dataset.project_id)
            source = self.path_policy.validate_read_path(
                dataset.source_path,
                project_roots=project.source_roots,
                expect_directory=True,
            )
            content = self.dataset_inspector.inspect(source)
            return dataset.project_id, content

        def finalize(prepared: tuple[str, dict[str, Any]]) -> ManifestRevisionView:
            project_id, content = prepared
            result = self.repository.create_manifest(
                dataset_id,
                expected_version=request.expected_dataset_version,
                content=content,
            )
            self.repository.append_event(
                project_id=project_id,
                run_id=None,
                event_type="DatasetInspected",
                severity="warning" if result.profile.warnings else "info",
                payload={
                    "dataset_id": dataset_id,
                    "manifest_id": result.manifest_id,
                    "manifest_hash": result.content_hash,
                    "subject_count": result.profile.subject_count,
                    "warnings": result.profile.warnings,
                },
            )
            return result

        return self._idempotent_prepared(
            scope=f"datasets:{dataset_id}:inspect",
            key=idempotency_key,
            request=request,
            response_type=ManifestRevisionView,
            prepare=prepare,
            finalize=finalize,
        )

    def get_manifest(self, manifest_id: str) -> ManifestRevisionView:
        return self.repository.get_manifest(manifest_id)

    def import_demographics(
        self,
        dataset_id: str,
        request: DemographicsImportRequest,
        idempotency_key: str,
    ) -> DemographicsRevisionView:
        def prepare() -> tuple[str, dict[str, Any]]:
            dataset = self.repository.get_dataset(dataset_id)
            if dataset.current_manifest_id is None:
                raise ConflictError(
                    "manifest_required", "导入人口学信息前必须冻结一个受试者清单版本。"
                )
            project = self.repository.get_project(dataset.project_id)
            source = self.path_policy.validate_read_path(
                request.source_path,
                project_roots=project.source_roots,
                expect_directory=False,
            )
            manifest = self.repository.get_manifest(dataset.current_manifest_id)
            manifest_subject_ids = {entry.subject_id for entry in manifest.subjects}
            content = self.demographics_reader(
                source,
                subject_id_column=request.subject_id_column,
                column_mapping=request.column_mapping,
                encoding=request.encoding,
                manifest_subject_ids=manifest_subject_ids,
            )
            return dataset.project_id, content

        def finalize(prepared: tuple[str, dict[str, Any]]) -> DemographicsRevisionView:
            project_id, content = prepared
            result = self.repository.create_demographics(
                dataset_id,
                expected_version=request.expected_dataset_version,
                content=content,
            )
            self.repository.append_event(
                project_id=project_id,
                run_id=None,
                event_type="DemographicsAligned",
                severity=(
                    "warning" if result.missing_subject_ids or result.extra_subject_ids else "info"
                ),
                payload={
                    "dataset_id": dataset_id,
                    "demographics_id": result.demographics_id,
                    "row_count": result.row_count,
                    "missing_count": len(result.missing_subject_ids),
                    "extra_count": len(result.extra_subject_ids),
                },
            )
            return result

        return self._idempotent_prepared(
            scope=f"datasets:{dataset_id}:demographics:import",
            key=idempotency_key,
            request=request,
            response_type=DemographicsRevisionView,
            prepare=prepare,
            finalize=finalize,
        )

    def get_demographics(self, demographics_id: str) -> DemographicsRevisionView:
        return self.repository.get_demographics(demographics_id)

    def create_split(
        self, dataset_id: str, request: DatasetSplitCreate, idempotency_key: str
    ) -> DatasetSplitView:
        def prepare() -> tuple[str, str, dict[str, Any]]:
            dataset = self.repository.get_dataset(dataset_id)
            if dataset.current_manifest_id is None:
                raise ConflictError("manifest_required", "数据集划分前必须冻结受试者清单。")
            manifest = self.repository.get_manifest(dataset.current_manifest_id)
            subject_ids = sorted({entry.subject_id for entry in manifest.subjects})
            if not subject_ids:
                raise InputValidationError("manifest_empty", "受试者清单为空, 无法划分数据集。")
            strata: dict[str, list[str]] = {"__all__": subject_ids}
            if request.stratify_by:
                assert request.demographics_revision_id is not None
                demographics_dataset_id, demographics = self.repository.get_demographics_content(
                    request.demographics_revision_id
                )
                if demographics_dataset_id != dataset_id:
                    raise ConflictError(
                        "cross_dataset_demographics", "人口学版本不属于当前数据集。"
                    )
                by_subject = {row["subject_id"]: row for row in demographics["rows"]}
                missing = [
                    subject_id
                    for subject_id in subject_ids
                    if subject_id not in by_subject
                    or by_subject[subject_id].get(request.stratify_by) in {None, ""}
                ]
                if missing:
                    raise InputValidationError(
                        "stratification_values_missing",
                        "部分受试者缺少分层字段, 无法安全划分。",
                        missing_subject_ids=missing,
                    )
                grouped: dict[str, list[str]] = defaultdict(list)
                for subject_id in subject_ids:
                    grouped[str(by_subject[subject_id][request.stratify_by])].append(subject_id)
                strata = dict(grouped)

            train: list[str] = []
            validation: list[str] = []
            test: list[str] = []
            for stratum, members in sorted(strata.items()):
                rng = random.Random(f"{request.seed}:{stratum}")
                shuffled = sorted(members)
                rng.shuffle(shuffled)
                counts = self._allocation_counts(
                    len(shuffled),
                    [request.train_ratio, request.validation_ratio, request.test_ratio],
                )
                train.extend(shuffled[: counts[0]])
                validation.extend(shuffled[counts[0] : counts[0] + counts[1]])
                test.extend(shuffled[counts[0] + counts[1] :])
            train.sort()
            validation.sort()
            test.sort()
            assigned = train + validation + test
            if len(assigned) != len(set(assigned)) or set(assigned) != set(subject_ids):
                raise ConflictError(
                    "subject_split_leakage", "数据集划分出现受试者重复或遗漏, 已阻止保存。"
                )
            content = {
                "seed": request.seed,
                "stratify_by": request.stratify_by,
                "ratios": {
                    "train": request.train_ratio,
                    "validation": request.validation_ratio,
                    "test": request.test_ratio,
                },
                "manifest_hash": manifest.content_hash,
                "train_subject_ids": train,
                "validation_subject_ids": validation,
                "test_subject_ids": test,
            }
            return dataset.project_id, manifest.content_hash, content

        def finalize(prepared: tuple[str, str, dict[str, Any]]) -> DatasetSplitView:
            project_id, manifest_hash, content = prepared
            result = self.repository.create_split(
                dataset_id,
                expected_version=request.expected_dataset_version,
                content=content,
            )
            self.repository.append_event(
                project_id=project_id,
                run_id=None,
                event_type="DatasetSplitCreated",
                severity="info",
                payload={
                    "dataset_id": dataset_id,
                    "split_id": result.split_id,
                    "manifest_hash": manifest_hash,
                    "counts": [
                        len(result.train_subject_ids),
                        len(result.validation_subject_ids),
                        len(result.test_subject_ids),
                    ],
                },
            )
            return result

        return self._idempotent_prepared(
            scope=f"datasets:{dataset_id}:splits:create",
            key=idempotency_key,
            request=request,
            response_type=DatasetSplitView,
            prepare=prepare,
            finalize=finalize,
        )

    def get_split(self, split_id: str) -> DatasetSplitView:
        return self.repository.get_split(split_id)

    @staticmethod
    def _allocation_counts(size: int, ratios: list[float]) -> list[int]:
        raw = [size * ratio for ratio in ratios]
        counts = [math.floor(value) for value in raw]
        remainder = size - sum(counts)
        order = sorted(range(len(ratios)), key=lambda index: (-(raw[index] - counts[index]), index))
        for index in order[:remainder]:
            counts[index] += 1
        return counts
