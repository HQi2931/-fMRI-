from __future__ import annotations

import copy
import hashlib

import pytest

from neuroagent.infrastructure.matlab_executor import MatlabJobExecutor

from .test_science_agent_api import _mask_lineage, _metric_lineage


def _outputs(root):
    artifacts = []
    for subject in ("sub-01", "sub-02"):
        path = f"output/{subject}/brain_mask.nii"
        file = root / path
        file.parent.mkdir(parents=True)
        # Byte-integrity fixtures, not scientific NIfTI data.
        file.write_bytes(b"identical verified effective mask")
        mask_lineage = _mask_lineage("a" * 64).model_copy(update={"subject_id": subject})
        artifacts.append(
            {
                "artifact_type": "preprocessing.brain_mask",
                "relative_path": path,
                "checksum": hashlib.sha256(file.read_bytes()).hexdigest(),
                "size_bytes": file.stat().st_size,
                "provenance": {"lineage": mask_lineage.model_dump(mode="json")},
            }
        )
        metric = _metric_lineage("a" * 64, subject, f"generated-output:attempts/{root.name}/{path}")
        artifacts.append(
            {
                "artifact_type": "metric.alff.z_score",
                "relative_path": f"output/{subject}/zALFF.nii",
                "checksum": "b" * 64,
                "size_bytes": 99,
                "provenance": {"lineage": metric.model_dump(mode="json")},
            }
        )
    return tuple(artifacts)


def test_identical_actual_masks_form_one_group_reference_and_preserve_subject_evidence(tmp_path):
    root = tmp_path / "attempt-001"
    outputs = _outputs(root)
    original = copy.deepcopy(outputs)
    result = MatlabJobExecutor._bind_shared_effective_mask(outputs, root)
    group = result[-1]
    assert group["artifact_type"] == "preprocessing.brain_mask"
    assert group["provenance"]["lineage"]["subject_id"] is None
    assert (root / group["relative_path"]).read_bytes() == (
        root / original[0]["relative_path"]
    ).read_bytes()
    sources = group["provenance"]["shared_effective_mask"]["source_masks"]
    assert [item["subject_id"] for item in sources] == ["sub-01", "sub-02"]
    for before, after in zip(original, result[:-1], strict=True):
        assert before["checksum"] == after["checksum"]
        assert before["relative_path"] == after["relative_path"]
        if after["artifact_type"] == "preprocessing.subject_brain_mask":
            assert after["provenance"]["lineage"] == before["provenance"]["lineage"]
        else:
            assert after["provenance"]["lineage"]["mask_artifact_id"] == (
                "generated-output:attempts/attempt-001/output/group/brain_mask.nii"
            )


@pytest.mark.parametrize("change", ["content", "space", "grid", "manifest"])
def test_different_effective_masks_are_never_combined_or_relabelled(tmp_path, change):
    outputs = _outputs(tmp_path)
    mask = outputs[2]
    if change == "content":
        file = tmp_path / mask["relative_path"]
        file.write_bytes(b"different actual subject mask")
        mask.update(
            checksum=hashlib.sha256(file.read_bytes()).hexdigest(), size_bytes=file.stat().st_size
        )
    else:
        field = {"space": "space", "grid": "grid_signature", "manifest": "subject_manifest_hash"}[
            change
        ]
        mask["provenance"]["lineage"][field] = "c" * 64 if change == "manifest" else "different"
    original = copy.deepcopy(outputs)
    assert MatlabJobExecutor._bind_shared_effective_mask(outputs, tmp_path) == original
    assert not (tmp_path / "output/group/brain_mask.nii").exists()


@pytest.mark.parametrize("subject_index", [0, 2])
def test_file_change_after_verified_execution_blocks_shared_mask(tmp_path, subject_index):
    outputs = _outputs(tmp_path)
    path = tmp_path / outputs[subject_index]["relative_path"]
    path.write_bytes(b"x" * path.stat().st_size)
    with pytest.raises(ValueError, match="changed after execution"):
        MatlabJobExecutor._bind_shared_effective_mask(outputs, tmp_path)
    assert not (tmp_path / "output/group/brain_mask.nii").exists()


def test_duplicate_subject_mask_is_rejected(tmp_path):
    outputs = _outputs(tmp_path)
    outputs[2]["provenance"]["lineage"]["subject_id"] = "sub-01"
    with pytest.raises(ValueError, match="duplicate"):
        MatlabJobExecutor._bind_shared_effective_mask(outputs, tmp_path)
