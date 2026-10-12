from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import nibabel as nib
import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from neuroagent.api.app import create_app
from neuroagent.application.contracts.conversations import (
    ConversationCreate,
    ConversationTurnCreate,
)
from neuroagent.application.contracts.external_imports import (
    ExternalAnalysisImportIncludeRequest,
    ExternalAnalysisImportInspectRequest,
)
from neuroagent.application.contracts.plans import (
    ApprovalCreate,
    ApprovalDecision,
)
from neuroagent.application.contracts.projects import (
    ExternalDemographicsImportRequest,
    ProjectCreate,
)
from neuroagent.application.contracts.statistics import (
    StatisticalDesignCreate,
    StatisticalDesignValidationRequest,
    StatisticsRunCreate,
)
from neuroagent.application.conversation_work import WorkActionResult
from neuroagent.application.errors import ConflictError, InputValidationError
from neuroagent.application.services import ApplicationServices
from neuroagent.application.statistics_draft import StatisticsDraft, merge_statistics_draft
from neuroagent.context.interfaces import ContextPacket
from neuroagent.domain.fmri.statistics import (
    AnalysisImage,
    MissingValuePolicy,
    StatisticalDesignRevision,
    StatisticalQcSource,
    StatisticalTest,
    Tail,
)
from neuroagent.execution.compiler import MatlabJobCompiler
from neuroagent.infrastructure.matlab_executor import MatlabJobExecutor


def _make_project(service: ApplicationServices, source_root: Path, work_root: Path) -> str:
    project = service.projects.create_project(
        ProjectCreate(
            name="External metric test",
            source_roots=[str(source_root)],
            work_root=str(work_root / "project"),
        ),
        "external-project-key",
    )
    return project.project_id


def _write_map(path: Path, offset: float = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.arange(4 * 5 * 6, dtype=np.float32).reshape((4, 5, 6)) + offset
    nib.save(nib.Nifti1Image(data, np.eye(4)), str(path))


def _valid_import_tree(root: Path) -> tuple[str, ...]:
    _write_map(root / "ASD" / "sub-01-zALFFMap.nii")
    _write_map(root / "ASD" / "sub-02-zALFFMap.nii", offset=1)
    _write_map(root / "HC" / "sub-03-zALFFMap.nii", offset=2)
    _write_map(root / "HC" / "sub-04-zALFFMap.nii", offset=3)
    return (
        "sub-01",
        "sub-02",
        "sub-03",
        "sub-04",
    )


def test_external_paired_source_binds_both_images_and_explicit_condition_direction(
    service: ApplicationServices, source_root: Path, work_root: Path
) -> None:
    project_id = _make_project(service, source_root, work_root)
    source = source_root / "paired"
    for condition in ("before", "after"):
        for index in range(1, 4):
            _write_map(source / condition / f"zALFFMap_sub-{index:02d}.nii", offset=index)
    _write_map(source / "Mask.nii")
    inspected = service.external_imports.inspect_external_analysis_import(
        ExternalAnalysisImportInspectRequest(
            project_id=project_id, source_path=str(source), layout="paired_conditions"
        ),
        "paired-inspect",
    )
    imported = service.external_imports.include_external_analysis_import(
        inspected.import_id,
        ExternalAnalysisImportIncludeRequest(
            expected_version=1,
            included_subject_ids=("sub-03", "sub-01", "sub-02"),
            group_order=("before", "after"),
            coordinate_space="unknown",
            acknowledge_unknown_processing=True,
            actor="researcher",
            reason="Synthetic paired fixture",
            mask_relative_path="Mask.nii",
        ),
        "paired-include",
    )
    assert imported.selection is not None
    assert len({image.artifact_id for image in imported.selection.images}) == 6
    summaries = service.external_imports.list_external_analysis_imports(project_id=project_id)
    assert summaries[0].subject_count == 3
    draft = StatisticsDraft(
        test=StatisticalTest.PAIRED_T,
        condition_column="condition",
        condition_order=["after", "before"],
        metric_artifact_type="external.alff.z_score",
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        correction_method="fdr",
        correction_parameters={"q_threshold": 0.05},
    )
    proposal, issues = service.statistics.prepare_work_statistical_design_proposal(
        external_import_id=imported.import_id, project_id=project_id, draft=draft
    )
    assert proposal is not None and not issues
    assert proposal.design.subject_order == ("sub-03", "sub-01", "sub-02")
    assert [(image.subject_id, image.condition) for image in proposal.design.images] == [
        (subject, condition)
        for condition in ("after", "before")
        for subject in proposal.design.subject_order
    ]
    assert all(image.group is None for image in proposal.design.images)
    assert proposal.correction is not None and proposal.correction.df1 == 2
    request = StatisticalDesignCreate(
        project_id=project_id,
        expected_project_version=proposal.project_version,
        input_manifest_hash=proposal.input_manifest_hash,
        design=proposal.design,
        correction=proposal.correction,
    )
    service.statistics.create_statistical_design(request, "paired-design")
    bindings = MatlabJobCompiler(service.repository)._external_bindings(
        proposal.design, proposal.input_manifest_hash
    )
    assert len(bindings) == 7
    assert len({binding.external_relative_path for binding in bindings}) == 7
    reversed_proposal, issues = service.statistics.prepare_work_statistical_design_proposal(
        external_import_id=imported.import_id,
        project_id=project_id,
        draft=draft.model_copy(update={"condition_order": ["before", "after"]}),
    )
    assert reversed_proposal is not None and not issues
    assert reversed_proposal.proposal_hash != proposal.proposal_hash
    (source / "after" / "zALFFMap_sub-02.nii").unlink()
    with pytest.raises(ConflictError, match="发生变化"):
        service.statistics.create_statistical_design(request, "paired-missing-source")


def test_external_demographics_aligns_covariates_and_invalidates_old_design(
    service: ApplicationServices, source_root: Path, work_root: Path
) -> None:
    project_id = _make_project(service, source_root, work_root)
    source = source_root / "covariates"
    subjects = _valid_import_tree(source)
    _write_map(source / "Mask.nii")
    inspected = service.external_imports.inspect_external_analysis_import(
        ExternalAnalysisImportInspectRequest(project_id=project_id, source_path=str(source)),
        "cov-inspect",
    )
    imported = service.external_imports.include_external_analysis_import(
        inspected.import_id,
        ExternalAnalysisImportIncludeRequest(
            expected_version=1,
            included_subject_ids=subjects,
            group_order=("ASD", "HC"),
            coordinate_space="unknown",
            acknowledge_unknown_processing=True,
            actor="researcher",
            reason="Synthetic covariate fixture",
            mask_relative_path="Mask.nii",
        ),
        "cov-include",
    )
    table = source_root / "demographics.csv"
    table.write_text("ID,Age\nsub-04,29\nsub-01,20\nsub-03,25\nsub-02,23\n", encoding="utf-8")
    body = ExternalDemographicsImportRequest(
        source_path=str(table),
        subject_id_column="ID",
        column_mapping={"age": "Age"},
        expected_import_version=imported.version,
    )
    with TestClient(create_app(service=service)) as client:
        response = client.post(
            f"/api/v1/statistics/external-imports/{imported.import_id}/demographics",
            json=body.model_dump(mode="json"),
            headers={"Idempotency-Key": "cov-import"},
        )
        assert response.status_code == 201, response.text
        demographics = response.json()
        replay = client.post(
            f"/api/v1/statistics/external-imports/{imported.import_id}/demographics",
            json=body.model_dump(mode="json"),
            headers={"Idempotency-Key": "cov-import"},
        )
        assert replay.json() == demographics
    draft = StatisticsDraft(
        test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
        grouping_column="group",
        group_order=["ASD", "HC"],
        metric_artifact_type="external.alff.z_score",
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        covariates=[{"name": "age", "centering": "grand_mean"}],
    )
    proposal, issues = service.statistics.prepare_work_statistical_design_proposal(
        external_import_id=imported.import_id, project_id=project_id, draft=draft
    )
    assert proposal is not None and not issues
    assert proposal.design.external_demographics_revision_id == demographics["demographics_id"]
    assert [value.value for value in proposal.design.covariates[0].values] == [20, 23, 25, 29]
    frozen_request = StatisticalDesignCreate(
        project_id=project_id,
        expected_project_version=proposal.project_version,
        input_manifest_hash=proposal.input_manifest_hash,
        design=proposal.design,
    )
    frozen = service.statistics.create_statistical_design(frozen_request, "cov-design")
    assert frozen.design.external_demographics_hash == demographics["content_hash"]
    for test in (StatisticalTest.CORRELATION, StatisticalTest.REGRESSION):
        prediction, issues = service.statistics.prepare_work_statistical_design_proposal(
            external_import_id=imported.import_id,
            project_id=project_id,
            draft=draft.model_copy(
                update={
                    "test": test,
                    "target_predictor": "age",
                    "grouping_column": None,
                    "group_order": [],
                }
            ),
        )
        assert prediction is not None and not issues
        assert (
            prediction.design.external_demographics_revision_id == demographics["demographics_id"]
        )
        assert all(image.group is None for image in prediction.design.images)
    # A revised table is a new immutable record, never a replacement of the old rows.
    table.write_text("ID,Age\nsub-02,24\nsub-04,29\nsub-03,25\nsub-01,20\n", encoding="utf-8")
    latest_import = service.external_imports.get_external_analysis_import(imported.import_id)
    revised = service.external_imports.import_external_demographics(
        imported.import_id,
        body.model_copy(update={"expected_import_version": latest_import.version}),
        "cov-revision",
    )
    assert revised.revision == 2
    with pytest.raises(ValueError, match="demographics revision changed"):
        MatlabJobCompiler(service.repository)._external_bindings(
            proposal.design, proposal.input_manifest_hash
        )
    with pytest.raises(ConflictError, match="人口学修订"):
        service.statistics.create_statistical_design(frozen_request, "cov-stale-design")
    table.write_text("ID,Age\nsub-01,20\nsub-01,24\n", encoding="utf-8")
    current_version = service.external_imports.get_external_analysis_import(
        imported.import_id
    ).version
    with pytest.raises(InputValidationError, match="重复"):
        service.external_imports.import_external_demographics(
            imported.import_id,
            body.model_copy(update={"expected_import_version": current_version}),
            "cov-duplicate",
        )
    table.write_text("ID,Age\nsub-01,20\nsub-02,24\nsub-03,25\n", encoding="utf-8")
    current_version = service.external_imports.get_external_analysis_import(
        imported.import_id
    ).version
    with pytest.raises(InputValidationError, match="恰好覆盖"):
        service.external_imports.import_external_demographics(
            imported.import_id,
            body.model_copy(update={"expected_import_version": current_version}),
            "cov-missing-subject",
        )
    for ages, expected_text in (
        (["NaN", "23", "25", "29"], "缺少取值"),
        (["25", "25", "25", "25"], "rank"),
    ):
        table.write_text(
            "ID,Age\n"
            + "".join(f"{subject},{age}\n" for subject, age in zip(subjects, ages, strict=True)),
            encoding="utf-8",
        )
        current_version = service.external_imports.get_external_analysis_import(
            imported.import_id
        ).version
        service.external_imports.import_external_demographics(
            imported.import_id,
            body.model_copy(update={"expected_import_version": current_version}),
            f"cov-invalid-{ages[0]}",
        )
        invalid, issues = service.statistics.prepare_work_statistical_design_proposal(
            external_import_id=imported.import_id, project_id=project_id, draft=draft
        )
        assert invalid is None and any(expected_text in issue.message for issue in issues)


def test_work_external_proposal_uses_reviewed_identity_and_rejects_source_drift(
    service: ApplicationServices,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = _make_project(service, source_root, work_root)
    source = source_root / "work-external"
    subjects = _valid_import_tree(source)
    _write_map(source / "Mask.nii")
    inspected = service.external_imports.inspect_external_analysis_import(
        ExternalAnalysisImportInspectRequest(project_id=project_id, source_path=str(source)),
        "work-inspect",
    )
    draft = StatisticsDraft(
        test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
        grouping_column="group",
        group_order=["HC", "ASD"],
        metric_artifact_type="external.alff.z_score",
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        correction_method="fdr",
        correction_parameters={"q_threshold": 0.05},
        external_import_id=inspected.import_id,
    )
    proposal, issues = service.statistics.prepare_work_statistical_design_proposal(
        external_import_id=inspected.import_id,
        project_id=project_id,
        draft=draft,
    )
    assert proposal is None and issues[0].code == "external_import_not_selectable"
    imported = service.external_imports.include_external_analysis_import(
        inspected.import_id,
        ExternalAnalysisImportIncludeRequest(
            expected_version=1,
            included_subject_ids=tuple(reversed(subjects)),
            group_order=("ASD", "HC"),
            acknowledge_unknown_processing=True,
            actor="researcher",
            reason="Synthetic review",
            coordinate_space="unknown",
            mask_relative_path="Mask.nii",
        ),
        "work-include",
    )
    proposal, issues = service.statistics.prepare_work_statistical_design_proposal(
        external_import_id=imported.import_id,
        project_id=project_id,
        draft=draft,
    )
    assert not issues and proposal is not None
    assert proposal.source_run_id is None
    assert proposal.design.external_import_hash == imported.inclusion_hash
    assert proposal.design.subject_order == tuple(reversed(subjects))
    assert proposal.design.group_order == ("HC", "ASD")
    assert [(image.subject_id, image.group) for image in proposal.design.images] == [
        ("sub-04", "HC"),
        ("sub-03", "HC"),
        ("sub-02", "ASD"),
        ("sub-01", "ASD"),
    ]
    assert proposal.correction is not None
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=project_id), "external-work-chat"
    )
    patch = draft.model_dump(mode="json", exclude={"external_import_id", "external_import_hash"})
    patch["external_import_id"] = "model-forged-source"
    interpretation = AsyncMock(
        return_value=(
            json.dumps(
                {
                    "answer": "draft",
                    "action": "prepare_statistical_analysis",
                    "analysis_patch": patch,
                }
            ),
            {},
            None,
        )
    )
    monkeypatch.setattr(service.work_dialogue, "_interpret", interpretation)
    response = asyncio.run(
        service.work_dialogue._respond_analysis(
            conversation,
            ConversationTurnCreate(
                content="Compare HC and ASD",
                project_id=project_id,
                external_analysis_import_id=imported.import_id,
            ),
            ContextPacket(question="Compare", metadata={"token_budget": 12000}),
            workspace_path=None,
            project_id=project_id,
            preferred_profile_id=None,
            workspace_content_consent=False,
            idempotency_key="external-work-turn",
            has_profiles=True,
            reply=lambda text, payload: WorkActionResult(
                assistant_content=text, payload=payload or {}
            ),
        )
    )
    assert (
        response.payload["statistical_design_proposal"]["design"]["external_import_id"]
        == imported.import_id
    )
    assert response.payload["analysis_draft"]["draft"]["external_import_id"] == imported.import_id
    context = interpretation.call_args.args[0]["work_context"]
    assert context["selected_external_source"]["grouping_column"] == "group"
    assert "source_path" not in json.dumps(context["selected_external_source"])
    with pytest.raises(ValidationError, match="cannot select"):
        ConversationTurnCreate(
            content="Compare",
            external_analysis_import_id=imported.import_id,
            target_run_id="another-run",
        )
    with pytest.raises(ValidationError, match="cannot select"):
        ConversationTurnCreate(
            content="Compare",
            external_analysis_import_id=imported.import_id,
            agent_role="preprocessing",
        )
    assert (
        merge_statistics_draft(draft, {"external_import_id": "model-invented"}).external_import_id
        == imported.import_id
    )
    invalid, issues = service.statistics.prepare_work_statistical_design_proposal(
        external_import_id=imported.import_id, project_id="another-project", draft=draft
    )
    assert invalid is None and issues[0].code == "cross_project_external_import"
    _write_map(source / "ASD" / "sub-01-zALFFMap.nii", offset=42)
    invalid, issues = service.statistics.prepare_work_statistical_design_proposal(
        external_import_id=imported.import_id, project_id=project_id, draft=draft
    )
    assert invalid is None and issues[0].code == "external_import_source_changed"


def test_external_import_requires_review_and_binds_inclusion_to_source_hash(
    service: ApplicationServices, source_root: Path, work_root: Path
) -> None:
    project_id = _make_project(service, source_root, work_root)
    source = source_root / "Slow-1"
    subjects = _valid_import_tree(source)
    _write_map(source / "Mask.nii")

    inspected = service.external_imports.inspect_external_analysis_import(
        ExternalAnalysisImportInspectRequest(project_id=project_id, source_path=str(source)),
        "external-inspection-key",
    )

    assert inspected.status == "needs_review"
    assert inspected.version == 1
    assert inspected.inclusion is None
    assert inspected.inspection.metric_kind == "alff"
    assert inspected.inspection.scaling == "z_score"
    assert inspected.inspection.group_order == ("ASD", "HC")
    assert len(inspected.inspection.images) == 4
    assert inspected.inspection.unknown_processing_fields

    included = service.external_imports.include_external_analysis_import(
        inspected.import_id,
        ExternalAnalysisImportIncludeRequest(
            expected_version=1,
            included_subject_ids=subjects,
            group_order=("ASD", "HC"),
            coordinate_space="unknown",
            acknowledge_unknown_processing=True,
            actor="researcher",
            reason="Reviewed external DPABI output names and group folders.",
            mask_relative_path="Mask.nii",
        ),
        "external-inclusion-key",
    )

    assert included.status == "included"
    assert included.version == 2
    assert included.inclusion is not None
    assert included.inclusion.included_subject_ids == subjects
    assert included.inclusion.excluded_subject_ids == ()
    assert included.inclusion.coordinate_space == "unknown"
    assert included.inclusion.coordinate_space_source == "unknown"
    assert included.inclusion.mask_relative_path == "Mask.nii"
    assert included.inclusion.mask_grid_signature == inspected.inspection.grid_signature
    assert included.inclusion.mask_sha256 is not None
    assert included.inclusion_hash is not None
    assert service.external_imports.get_external_analysis_import(
        inspected.import_id
    ).inclusion_hash == (included.inclusion_hash)
    assert service.external_imports.list_external_analysis_imports(project_id=project_id)[
        0
    ].import_id == (inspected.import_id)


def test_external_import_refuses_files_changed_after_inspection(
    service: ApplicationServices, source_root: Path, work_root: Path
) -> None:
    project_id = _make_project(service, source_root, work_root)
    source = source_root / "Slow-1"
    subjects = _valid_import_tree(source)
    inspected = service.external_imports.inspect_external_analysis_import(
        ExternalAnalysisImportInspectRequest(project_id=project_id, source_path=str(source)),
        "external-inspection-key-2",
    )
    _write_map(source / "ASD" / "sub-01-zALFFMap.nii", offset=99)

    with pytest.raises(ConflictError) as caught:
        service.external_imports.include_external_analysis_import(
            inspected.import_id,
            ExternalAnalysisImportIncludeRequest(
                expected_version=1,
                included_subject_ids=subjects,
                group_order=("ASD", "HC"),
                coordinate_space="MNI",
                acknowledge_unknown_processing=True,
                actor="researcher",
                reason="Review should fail because input changed.",
            ),
            "external-inclusion-key-2",
        )
    assert caught.value.code == "external_import_source_changed"


def test_external_import_rejects_mask_on_another_grid(
    service: ApplicationServices, source_root: Path, work_root: Path
) -> None:
    project_id = _make_project(service, source_root, work_root)
    source = source_root / "different-mask-grid"
    subjects = _valid_import_tree(source)
    mask = nib.Nifti1Image(np.ones((4, 5, 6), dtype=np.float32), np.diag([2, 1, 1, 1]))
    nib.save(mask, str(source / "Mask.nii"))
    inspected = service.external_imports.inspect_external_analysis_import(
        ExternalAnalysisImportInspectRequest(project_id=project_id, source_path=str(source)),
        "external-inspection-mask-grid",
    )

    with pytest.raises(InputValidationError) as caught:
        service.external_imports.include_external_analysis_import(
            inspected.import_id,
            ExternalAnalysisImportIncludeRequest(
                expected_version=1,
                included_subject_ids=subjects,
                group_order=("ASD", "HC"),
                coordinate_space="unknown",
                acknowledge_unknown_processing=True,
                actor="researcher",
                reason="Mask grid must be checked before inclusion.",
                mask_relative_path="Mask.nii",
            ),
            "external-inclusion-mask-grid",
        )
    assert caught.value.code == "external_import_mask_grid_mismatch"


def test_external_import_with_scanner_blockers_cannot_be_included(
    service: ApplicationServices, source_root: Path, work_root: Path
) -> None:
    project_id = _make_project(service, source_root, work_root)
    source = source_root / "mixed"
    _write_map(source / "ASD" / "sub-01-zALFFMap.nii")
    _write_map(source / "HC" / "sub-02-zReHoMap.nii", offset=1)
    inspected = service.external_imports.inspect_external_analysis_import(
        ExternalAnalysisImportInspectRequest(project_id=project_id, source_path=str(source)),
        "external-inspection-key-3",
    )
    assert inspected.inspection.blocking_issues

    with pytest.raises(InputValidationError, match="阻断项"):
        service.external_imports.include_external_analysis_import(
            inspected.import_id,
            ExternalAnalysisImportIncludeRequest(
                expected_version=1,
                included_subject_ids=("sub-01-zALFFMap", "sub-02-zReHoMap"),
                group_order=("ASD", "HC"),
                coordinate_space="unknown",
                acknowledge_unknown_processing=True,
                actor="researcher",
                reason="Attempt to include an invalid mixed metric source.",
            ),
            "external-inclusion-key-3",
        )


def test_external_import_api_returns_full_scan_once_and_bounded_list_projection(
    service: ApplicationServices, source_root: Path, work_root: Path
) -> None:
    project_id = _make_project(service, source_root, work_root)
    source = source_root / "Slow-1"
    _valid_import_tree(source)

    with TestClient(create_app(service=service)) as client:
        response = client.post(
            "/api/v1/statistics/external-imports/inspect",
            headers={"Idempotency-Key": "external-api-inspect-key"},
            json={"project_id": project_id, "source_path": str(source)},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "needs_review"
        assert len(body["inspection"]["images"]) == 4

        listed = client.get(
            "/api/v1/statistics/external-imports", params={"project_id": project_id}
        )
        assert listed.status_code == 200
        summary = listed.json()[0]
        assert summary["subject_count"] == 4
        assert summary["group_counts"] == {"ASD": 2, "HC": 2}
        assert "source_path" not in summary
        assert "images" not in summary

        decision = client.post(
            f"/api/v1/statistics/external-imports/{body['import_id']}/include",
            headers={"Idempotency-Key": "external-api-include-key"},
            json={
                "expected_version": 1,
                "included_subject_ids": [
                    image["subject_id"] for image in body["inspection"]["images"]
                ],
                "group_order": ["ASD", "HC"],
                "coordinate_space": "unknown",
                "acknowledge_unknown_processing": True,
                "actor": "researcher",
                "reason": "Reviewed the scan and accepted unknown processing metadata.",
            },
        )
        assert decision.status_code == 200
        assert decision.json()["status"] == "included"


def test_external_source_freezes_design_and_stages_hash_verified_copies(
    service: ApplicationServices, source_root: Path, work_root: Path
) -> None:
    project_id = _make_project(service, source_root, work_root)
    source = source_root / "external-execution"
    subjects = _valid_import_tree(source)
    _write_map(source / "Mask.nii")
    inspected = service.external_imports.inspect_external_analysis_import(
        ExternalAnalysisImportInspectRequest(project_id=project_id, source_path=str(source)),
        "execution-inspect",
    )
    imported = service.external_imports.include_external_analysis_import(
        inspected.import_id,
        ExternalAnalysisImportIncludeRequest(
            expected_version=1,
            included_subject_ids=subjects,
            group_order=("ASD", "HC"),
            coordinate_space="MNI",
            acknowledge_unknown_processing=True,
            actor="researcher",
            reason="Synthetic source reviewed for execution staging test.",
            mask_relative_path="Mask.nii",
        ),
        "execution-include",
    )
    assert imported.selection is not None
    selection = imported.selection
    # A shuffled explicit order must preserve subject/group identity.
    images = tuple(reversed(selection.images))
    design = StatisticalDesignRevision(
        revision_id="external-two-sample",
        test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
        subject_order=tuple(item.subject_id for item in images),
        images=tuple(
            AnalysisImage(
                subject_id=item.subject_id,
                artifact_id=item.artifact_id,
                group=item.group,
                condition=None,
            )
            for item in images
        ),
        group_order=("ASD", "HC"),
        condition_order=(),
        covariates=(),
        contrast=(1, 0),
        one_sample_baseline=None,
        mask_artifact_id=selection.mask_artifact_id,
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        qc_source=StatisticalQcSource.EXTERNAL_IMPORT,
        external_import_id=imported.import_id,
        external_import_hash=imported.inclusion_hash,
    )
    frozen = service.statistics.create_statistical_design(
        StatisticalDesignCreate(
            project_id=project_id,
            expected_project_version=service.projects.get_project(project_id).version,
            input_manifest_hash=selection.input_manifest_hash,
            design=design,
        ),
        "execution-design",
    )
    validated = service.statistics.validate_statistical_design(
        frozen.plan_revision.plan_revision_id,
        StatisticalDesignValidationRequest(expected_version=frozen.plan_revision.version),
        "execution-validate",
    )
    service.plans.approve_plan(
        validated.plan_revision.plan_revision_id,
        ApprovalCreate(
            expected_version=validated.plan_revision.version,
            plan_hash=validated.plan_revision.plan_hash,
            actor="researcher",
            decision=ApprovalDecision.APPROVED,
            reason="Synthetic external design reviewed.",
        ),
        "execution-approve",
    )
    run = service.runs.create_statistics_run(
        StatisticsRunCreate(
            project_id=project_id,
            statistical_design_revision_id=validated.plan_revision.plan_revision_id,
            expected_plan_hash=validated.plan_revision.plan_hash,
        ),
        "execution-run",
    )
    job = MatlabJobCompiler(service.repository).compile_statistics(
        job_id="external-job",
        run_id=run.run_id,
        plan_hash=validated.plan_revision.plan_hash,
        approval_record_id="test-approval",
        input_manifest_hash=selection.input_manifest_hash,
        design=design,
        correction=None,
    )
    assert len(job.artifact_bindings) == 5
    assert all(item.external_import_id == imported.import_id for item in job.artifact_bindings)
    destination = Path(service.projects.get_project(project_id).work_root) / run.run_id / "attempt"
    destination.mkdir(parents=True)
    executor = MatlabJobExecutor(service.repository, service.settings)
    executor._stage_inputs(run.run_id, destination, job)
    for binding in job.artifact_bindings:
        assert binding.external_relative_path is not None
        assert (destination / binding.relative_path).read_bytes() == (
            source / binding.external_relative_path
        ).read_bytes()

    _write_map(source / "ASD" / "sub-01-zALFFMap.nii", offset=100)
    with pytest.raises(ConflictError) as caught:
        service.runs.create_statistics_run(
            StatisticsRunCreate(
                project_id=project_id,
                statistical_design_revision_id=validated.plan_revision.plan_revision_id,
                expected_plan_hash=validated.plan_revision.plan_hash,
            ),
            "execution-run-after-drift",
        )
    assert caught.value.code == "plan_stale"
    second_attempt = destination / "second-attempt"
    second_attempt.mkdir()
    with pytest.raises(ValueError, match="source hash changed"):
        executor._stage_inputs(run.run_id, second_attempt, job)
