from __future__ import annotations

import pytest
from pydantic import ValidationError

from neuroagent.domain.fmri import (
    NormalizationMode,
    NormalizationParameters,
    PreprocessingParameters,
    RealignmentParameters,
    SliceTimingParameters,
    SmoothingMethod,
    SmoothingParameters,
    SmoothingTiming,
    TissueMaskSource,
)
from neuroagent.tools import DpabiPreprocessingRequest, DpabiV82Adapter
from tests.science.conftest import preprocessing_parameters


def preprocessing_request() -> DpabiPreprocessingRequest:
    return DpabiPreprocessingRequest(
        subject_ids=("sub-01", "sub-02"),
        functional_session_number=1,
        starting_dir_name="FunImg",
        input_manifest_hash="a" * 64,
        parameters=preprocessing_parameters(),
    )


def test_preprocessing_parameters_reject_implicit_or_inconsistent_choices() -> None:
    values = preprocessing_parameters().model_dump()
    values["dummy_scans"] = 240
    with pytest.raises(ValidationError, match="smaller than expected"):
        type(preprocessing_parameters()).model_validate(values)

    values = preprocessing_parameters().model_dump()
    values["slice_timing"] = SliceTimingParameters(
        enabled=False,
        slice_count=None,
        slice_order=None,
        reference_slice=None,
    ).model_dump()
    values["provenance"] = values["provenance"][:-1]
    with pytest.raises(ValidationError, match="provenance missing"):
        type(preprocessing_parameters()).model_validate(values)

    values = preprocessing_parameters().model_dump()
    values["realignment"] = RealignmentParameters(
        enabled=False,
        options_source=None,
    ).model_dump()
    with pytest.raises(ValidationError, match="requires realignment"):
        type(preprocessing_parameters()).model_validate(values)

    with pytest.raises(ValidationError, match="permutation"):
        SliceTimingParameters(
            enabled=True,
            slice_count=4,
            slice_order=(1, 2, 2, 4),
            reference_slice=2,
        )


def test_dpabi_v82_preprocessing_cfg_snapshot() -> None:
    projection = DpabiV82Adapter().project_preprocessing_cfg(preprocessing_request())
    assert projection.cfg == {
        "SubjectID": ["sub-01", "sub-02"],
        "SubjectNum": 2,
        "FunctionalSessionNumber": 1,
        "StartingDirName": "FunImg",
        "TR": 2.0,
        "TimePoints": 240,
        "RemoveFirstTimePoints": 10,
        "IsNeedConvertFunDCM2IMG": 0,
        "IsNeedConvertT1DCM2IMG": 0,
        "IsBIDStoDPARSF": 0,
        "IsApplyDownloadedReorientMats": 0,
        "IsNeedReorientFunImgInteractively": 0,
        "IsNeedReorientCropT1Img": 0,
        "IsNeedReorientT1ImgInteractively": 0,
        "IsNeedReorientInteractivelyAfterCoreg": 0,
        "IsBet": 0,
        "IsCalALFF": 0,
        "IsCalReHo": 0,
        "IsCalVoxelSpecificHeadMotion": 0,
        "IsCalDegreeCentrality": 0,
        "IsCalFC": 0,
        "IsExtractROISignals": 0,
        "IsDefineROIInteractively": 0,
        "IsExtractAALTC": 0,
        "IsNormalizeToSymmetricGroupT1Mean": 0,
        "IsSmoothBeforeVMHC": 0,
        "IsCalVMHC": 0,
        "IsCWAS": 0,
        "IsAllowGUI": 0,
        "IsSliceTiming": 1,
        "IsRealign": 1,
        "IsDetrend": 0,
        "IsAutoMask": 1,
        "IsWarpMasksIntoIndividualSpace": 0,
        "SliceTiming": {
            "SliceNumber": 4,
            "TR": 2.0,
            "TA": 1.5,
            "SliceOrder": [1, 3, 2, 4],
            "ReferenceSlice": 2,
        },
        "IsCovremove": 1,
        "Covremove": {
            "Timing": "AfterNormalize",
            "PolynomialTrend": 1,
            "HeadMotion": 4,
            "IsHeadMotionScrubbingRegressors": 1,
            "HeadMotionScrubbingRegressors": {
                "FDType": "FD_Power",
                "FDThreshold": 0.5,
                "PreviousPoints": 1,
                "LaterPoints": 2,
            },
            "WM": {
                "IsRemove": 1,
                "Mask": "SPM",
                "MaskThreshold": 0.99,
                "Method": "Mean",
            },
            "CSF": {
                "IsRemove": 1,
                "Mask": "Segment",
                "MaskThreshold": 0.95,
                "Method": "CompCor",
                "CompCorPCNum": 5,
            },
            "WholeBrain": {
                "IsRemove": 1,
                "IsBothWithWithoutGSR": 0,
                "Mask": "AutoMask",
                "Method": "Mean",
            },
            "OtherCovariatesROI": [],
            "IsAddMeanBack": 1,
        },
        "IsNormalize": 2,
        "IsNeedT1CoregisterToFun": 1,
        "IsSegment": 1,
        "IsDARTEL": 0,
        "Normalize": {
            "Timing": "OnFunctionalData",
            "BoundingBox": [[-90.0, -126.0, -72.0], [90.0, 90.0, 108.0]],
            "VoxSize": [3.0, 3.0, 3.0],
        },
        "Segment": {"AffineRegularisationInSegmentation": "mni"},
        "IsFilter": 1,
        "Filter": {
            "Timing": "AfterNormalize",
            "AHighPass_LowCutoff": 0.01,
            "ALowPass_HighCutoff": 0.08,
            "AAddMeanBack": 1,
        },
        "IsScrubbing": 1,
        "Scrubbing": {
            "FDType": "FD_Jenkinson",
            "FDThreshold": 0.2,
            "PreviousPoints": 1,
            "LaterPoints": 2,
            "Timing": "AfterPreprocessing",
            "ScrubbingMethod": "cut",
        },
        "IsSmooth": 0,
    }
    assert (
        projection.cfg_hash
        == DpabiV82Adapter().project_preprocessing_cfg(preprocessing_request()).cfg_hash
    )


def test_preprocessing_disabled_and_smoothing_branches_are_explicit() -> None:
    values = preprocessing_parameters().model_dump()
    values["normalization"] = NormalizationParameters(
        mode=NormalizationMode.DISABLED,
        timing=None,
        bounding_box_mm=None,
        voxel_size_mm=None,
        structural_artifact_id=None,
        affine_regularization=None,
    ).model_dump()
    values["nuisance"]["timing"] = "after_realign"
    values["nuisance"]["csf"]["mask_source"] = TissueMaskSource.SPM
    values["smoothing"] = SmoothingParameters(
        timing=SmoothingTiming.ON_RESULTS,
        method=SmoothingMethod.SPM,
        fwhm_mm=(6.0, 6.0, 6.0),
    ).model_dump()
    parameters = PreprocessingParameters.model_validate(values)
    request = preprocessing_request().model_copy(update={"parameters": parameters})
    cfg = DpabiV82Adapter().project_preprocessing_cfg(request).cfg
    assert cfg["IsNormalize"] == 0
    assert cfg["IsNeedT1CoregisterToFun"] == 0
    assert cfg["IsSmooth"] == 1
    assert cfg["Smooth"] == {"Timing": "OnResults", "FWHM": [6.0, 6.0, 6.0]}
