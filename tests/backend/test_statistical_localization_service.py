from __future__ import annotations

import json
from types import SimpleNamespace

import nibabel as nib
import numpy as np
import pytest
from fastapi.testclient import TestClient

from neuroagent.api import create_app
from neuroagent.application.errors import ConflictError, InputValidationError
from neuroagent.application.service_mixins import runs

from .test_statistical_atlas_selection import _atlases, _detail


@pytest.fixture
def localization_fixture(service, monkeypatch, tmp_path):
    _atlases(service)
    path = tmp_path / "statistic.nii"
    values = np.zeros((3, 4, 5), dtype=np.float32)
    values[0, 0, 0] = 4
    nib.save(nib.Nifti1Image(values, np.eye(4)), path)
    detail = _detail(None)
    detail.run_id, detail.project_id = "atlas-run", "atlas-project"
    monkeypatch.setattr(
        service.runs.artifact_repository, "get_statistical_result", lambda _: detail
    )
    monkeypatch.setattr(service.runs.repository, "get_run", lambda _: SimpleNamespace())
    monkeypatch.setattr(service.runs.project_repository, "get_project", lambda _: SimpleNamespace())
    monkeypatch.setattr(
        service.runs.artifact_repository, "get_artifact", lambda _: SimpleNamespace()
    )
    monkeypatch.setattr(runs, "artifact_content_path", lambda *_: path)
    return detail, path


@pytest.mark.parametrize("atlas_id", ["aal", "aal3", "brodmann"])
def test_frozen_atlas_localization_round_trips_through_public_api(
    service, localization_fixture, atlas_id
):
    detail, _ = localization_fixture
    selection = service.statistics._freeze_statistical_atlas(atlas_id)
    detail.report_json = json.dumps({"localization_plan": selection.model_dump(mode="json")})
    with TestClient(create_app(service=service)) as client:
        response = client.get(
            f"/api/v1/statistics/results/atlas-result/localization?atlas_id={atlas_id}"
        )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "localized" and result["coordinate_space"] == "MNI"
    assert result["atlas_content_hash"] == selection.atlas_content_hash
    assert result["result_bundle_hash"] == detail.bundle_hash
    cluster = result["clusters"][0]
    assert cluster["label"] == "Region one" and cluster["peak_coordinate_mm"] == [0, 0, 0]
    assert cluster["distance_to_voxel_center_mm"] == 0
    assert cluster["coverage"] == [
        {"label_value": 1, "label": "Region one", "voxel_count": 1, "proportion": 1}
    ]
    assert service.runs.list_statistical_atlases()[0].available


@pytest.mark.parametrize("space,expected_text", [("native", "已确认空间"), ("unknown", "尚未确认")])
def test_known_different_space_and_unconfirmed_space_have_distinct_messages(
    service, localization_fixture, space, expected_text
):
    detail, _ = localization_fixture
    detail.manifest["clusters"][0]["coordinate_space"] = space
    with pytest.raises(ConflictError) as failure:
        service.runs.localize_statistical_result("atlas-result", "aal")
    assert failure.value.code == "statistics_coordinate_space_mismatch"
    assert expected_text in str(failure.value)
    assert failure.value.details["result_spaces"] == [space]


@pytest.mark.parametrize("change", ["extent", "peak", "outside", "multiple_maps"])
def test_localization_service_converts_invalid_spatial_evidence_to_blocking_error(
    service, localization_fixture, change
):
    detail, _ = localization_fixture
    cluster = detail.manifest["clusters"][0]
    if change == "extent":
        cluster["extent_voxels"] = 2
    elif change == "peak":
        cluster["peak_statistic"] = 9
    elif change == "outside":
        cluster["peak_coordinate_mm"] = [99, 99, 99]
    else:
        original_map = next(
            item
            for item in detail.manifest["artifacts"]
            if item["artifact_id"] == cluster["source_map_artifact_id"]
        )
        detail.manifest["artifacts"].append({**original_map, "artifact_id": "second-map"})
        detail.manifest["clusters"].append(
            {**cluster, "cluster_id": "other", "source_map_artifact_id": "second-map"}
        )
    with pytest.raises(InputValidationError) as failure:
        service.runs.localize_statistical_result("atlas-result", "aal")
    assert failure.value.code == "statistical_atlas_localization_failed"


def test_changed_atlas_during_localization_withholds_all_regions(
    service, localization_fixture, monkeypatch
):
    original = runs.get_atlas

    def changed(*args):
        spec, _ = original(*args)
        return spec, "f" * 64

    monkeypatch.setattr(runs, "get_atlas", changed)
    with pytest.raises(ConflictError) as failure:
        service.runs.localize_statistical_result("atlas-result", "aal")
    assert failure.value.code == "atlas_changed_during_localization"


@pytest.mark.parametrize(
    "atlas_id,code", [("unknown", "atlas_not_supported"), ("aal", "atlas_unavailable")]
)
def test_unavailable_atlas_request_fails_before_mapping(
    service, localization_fixture, atlas_id, code
):
    if atlas_id == "aal":
        root = service.environment_provider.configuration().dpabi_dir
        (root / "Templates" / "aal.nii").unlink()
    with pytest.raises(InputValidationError) as failure:
        service.runs.localize_statistical_result("atlas-result", atlas_id)
    assert failure.value.code == code
