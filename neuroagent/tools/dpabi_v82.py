"""Explicit domain-to-DPABI V8.2 mapping.

Only fields confirmed in the local V8.2 source are emitted here.  The adapter
does not choose scientific parameters and does not execute MATLAB.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from neuroagent.domain.fmri.metrics import (
    AlffFalffParameters,
    RehoParameters,
    TemporalFilterTiming,
)
from neuroagent.domain.fmri.preprocessing import (
    FdType,
    GlobalSignalMaskSource,
    MotionCensoringParameters,
    NormalizationMode,
    NormalizationTiming,
    NuisanceTiming,
    PreprocessingParameters,
    ScrubbingTiming,
    TissueMaskSource,
    TissueRegressor,
    TissueRegressorMethod,
)
from neuroagent.domain.fmri.statistics import (
    CorrectionSpec,
    FdrCorrection,
    GrfCorrection,
    StatisticalDesignRevision,
    StatisticalTest,
    Tail,
    centered_covariate_columns,
    design_matrix,
    residual_degrees_of_freedom,
)
from neuroagent.skills.models import stable_hash


class FrozenToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DpabiMetricRequest(FrozenToolModel):
    subject_ids: tuple[str, ...]
    functional_session_number: int = Field(gt=0)
    starting_dir_name: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    input_manifest_hash: str = Field(min_length=64, max_length=64)
    mask_relative_path: str = Field(min_length=1)
    alff_falff: AlffFalffParameters | None
    reho: RehoParameters | None

    @field_validator("mask_relative_path")
    @classmethod
    def safe_mask_path(cls, value: str) -> str:
        normalized = PurePosixPath(value.replace("\\", "/"))
        if normalized.is_absolute() or ".." in normalized.parts:
            raise ValueError("mask path must remain inside the run staging directory")
        return normalized.as_posix()

    @model_validator(mode="after")
    def request_is_complete(self) -> DpabiMetricRequest:
        if not self.subject_ids or len(set(self.subject_ids)) != len(self.subject_ids):
            raise ValueError("subject_ids must be non-empty and unique")
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", item) for item in self.subject_ids):
            raise ValueError("subject IDs contain unsafe characters")
        if self.alff_falff is None and self.reho is None:
            raise ValueError("at least one metric configuration is required")
        if self.alff_falff and self.reho:
            if self.alff_falff.result_smoothing != self.reho.global_result_smoothing:
                raise ValueError(
                    "combined DPARSFA runs require one consistent global result-smoothing choice"
                )
            if (
                self.alff_falff.result_smoothing_fwhm_mm
                != self.reho.global_result_smoothing_fwhm_mm
            ):
                raise ValueError("combined DPARSFA runs require one global result-smoothing FWHM")
            if self.alff_falff.result_smoothing and self.reho.smooth_reho:
                raise ValueError("combined settings would smooth ReHo twice")
        return self


class DpabiPreprocessingRequest(FrozenToolModel):
    subject_ids: tuple[str, ...]
    functional_session_number: int = Field(gt=0)
    starting_dir_name: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    input_manifest_hash: str = Field(min_length=64, max_length=64)
    parameters: PreprocessingParameters

    @model_validator(mode="after")
    def safe_subjects(self) -> DpabiPreprocessingRequest:
        if not self.subject_ids or len(set(self.subject_ids)) != len(self.subject_ids):
            raise ValueError("subject_ids must be non-empty and unique")
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", item) for item in self.subject_ids):
            raise ValueError("subject IDs contain unsafe characters")
        return self


class DpabiCfgProjection(FrozenToolModel):
    adapter_version: str
    source_evidence: tuple[str, ...]
    cfg: dict[str, Any]
    cfg_hash: str = Field(min_length=64, max_length=64)


class StatisticsFunction(StrEnum):
    TTEST1 = "y_TTest1_Image"
    TTEST2 = "y_TTest2_Image"
    TTEST_PAIRED = "y_TTestPaired_Image"
    GROUP_ANALYSIS = "y_GroupAnalysis_Image"


class StatisticsCall(FrozenToolModel):
    function: StatisticsFunction
    ordered_subject_ids: tuple[str, ...]
    dependent_artifact_groups: tuple[tuple[str, ...], ...]
    other_covariates_by_group: tuple[tuple[tuple[float, ...], ...], ...]
    mask_artifact_id: str
    design_matrix: tuple[tuple[float, ...], ...]
    contrast: tuple[float, ...]
    one_sample_baseline: float | None
    tail: Tail
    residual_df: int = Field(gt=0)
    revision_id: str
    design_hash: str = Field(min_length=64, max_length=64)


class CorrectionFunction(StrEnum):
    FDR = "y_FDR_Image"
    GRF = "y_GRF_Threshold"


class CorrectionCall(FrozenToolModel):
    function: CorrectionFunction
    parameters: dict[str, Any]
    correction_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def registered_parameters_are_complete(self) -> CorrectionCall:
        common = {"mask_artifact_id", "statistic_type", "df1", "df2"}
        required = (
            common | {"q_threshold"}
            if self.function is CorrectionFunction.FDR
            else common
            | {
                "voxel_p_threshold",
                "two_tailed",
                "cluster_p_threshold",
                "smoothness_mode",
                "smoothness_dlh",
            }
        )
        missing = sorted(required - self.parameters.keys())
        extra = sorted(self.parameters.keys() - required)
        if missing or extra:
            raise ValueError(
                f"correction parameters do not match {self.function.value}: "
                f"missing={missing}, extra={extra}"
            )
        return self


class DpabiV82Adapter:
    version = "1.0.0"

    def project_preprocessing_cfg(self, request: DpabiPreprocessingRequest) -> DpabiCfgProjection:
        parameters = request.parameters
        cfg: dict[str, Any] = {
            "SubjectID": list(request.subject_ids),
            "SubjectNum": len(request.subject_ids),
            "FunctionalSessionNumber": request.functional_session_number,
            "StartingDirName": request.starting_dir_name,
            "TR": parameters.tr_seconds,
            # DPABI uses zero to mean that the caller explicitly declines a count check.
            "TimePoints": parameters.expected_time_points or 0,
            "RemoveFirstTimePoints": parameters.dummy_scans,
            # Conversion is a separate staged job and must never be inherited from base Cfg.
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
            "IsSliceTiming": int(parameters.slice_timing.enabled),
            "IsRealign": int(parameters.realignment.enabled),
            "IsDetrend": int(parameters.detrend),
            "IsAutoMask": int(
                parameters.nuisance.enabled
                and parameters.nuisance.global_signal is not None
                and parameters.nuisance.global_signal.enabled
                and parameters.nuisance.global_signal.mask_source
                is GlobalSignalMaskSource.AUTO_MASK
            ),
            "IsWarpMasksIntoIndividualSpace": int(
                parameters.nuisance.warp_masks_to_individual_space or False
            ),
        }
        if parameters.slice_timing.enabled:
            slice_count = parameters.slice_timing.slice_count
            assert slice_count is not None
            cfg["SliceTiming"] = {
                "SliceNumber": slice_count,
                "TR": parameters.tr_seconds,
                "TA": parameters.tr_seconds - (parameters.tr_seconds / slice_count),
                "SliceOrder": list(parameters.slice_timing.slice_order or ()),
                "ReferenceSlice": parameters.slice_timing.reference_slice,
            }

        cfg.update(_project_nuisance(parameters))
        cfg.update(_project_normalization(parameters))
        cfg.update(_project_filter(parameters))
        cfg.update(_project_scrubbing(parameters))
        cfg.update(_project_smoothing(parameters))
        return DpabiCfgProjection(
            adapter_version=self.version,
            source_evidence=(
                "DPARSFA_run.m:628-677",
                "DPARSFA_run.m:685-800",
                "DPARSFA_run.m:912-964",
                "DPARSFA_run.m:2759-2961",
                "DPARSFA_run.m:2983-3409",
                "DPARSFA_run.m:3439-4048",
                "DPARSF_run.m:40-68",
            ),
            cfg=cfg,
            cfg_hash=stable_hash(cfg),
        )

    def project_pipeline_cfg(
        self,
        preprocessing: DpabiPreprocessingRequest,
        metrics: DpabiMetricRequest | None,
    ) -> DpabiCfgProjection:
        common = self.project_preprocessing_cfg(preprocessing)
        if metrics is None:
            return common
        identity = (
            preprocessing.subject_ids,
            preprocessing.functional_session_number,
            preprocessing.starting_dir_name,
            preprocessing.input_manifest_hash,
        )
        metric_identity = (
            metrics.subject_ids,
            metrics.functional_session_number,
            metrics.starting_dir_name,
            metrics.input_manifest_hash,
        )
        if identity != metric_identity:
            raise ValueError("preprocessing and metric projections must share frozen inputs")
        metric_projection = self.project_metric_cfg(metrics)
        cfg = dict(common.cfg)
        for field in ("TR", "IsFilter"):
            if field in metric_projection.cfg and metric_projection.cfg[field] != cfg.get(field):
                raise ValueError(f"preprocessing and metric projection disagree on {field}")
        for field, value in metric_projection.cfg.get("Filter", {}).items():
            if cfg.get("Filter", {}).get(field) != value:
                raise ValueError(f"preprocessing and metric projection disagree on Filter.{field}")
        if cfg.get("IsSmooth"):
            raise ValueError("common preprocessing smoothing must be disabled for metric runs")
        for field, value in metric_projection.cfg.items():
            if field in {"SubjectID", "SubjectNum", "FunctionalSessionNumber", "StartingDirName"}:
                continue
            if field in {"IsFilter", "Filter"}:
                continue
            cfg[field] = value
        return DpabiCfgProjection(
            adapter_version=self.version,
            source_evidence=tuple(
                dict.fromkeys((*common.source_evidence, *metric_projection.source_evidence))
            ),
            cfg=cfg,
            cfg_hash=stable_hash(cfg),
        )

    def project_metric_cfg(self, request: DpabiMetricRequest) -> DpabiCfgProjection:
        cfg: dict[str, Any] = {
            "SubjectID": list(request.subject_ids),
            "SubjectNum": len(request.subject_ids),
            "FunctionalSessionNumber": request.functional_session_number,
            "StartingDirName": request.starting_dir_name,
            "IsCalALFF": int(request.alff_falff is not None),
            "IsCalReHo": int(request.reho is not None),
        }
        tr_values = {
            value
            for value in (
                request.alff_falff.tr_seconds if request.alff_falff else None,
                request.reho.tr_seconds if request.reho else None,
            )
            if value is not None
        }
        if len(tr_values) != 1:
            raise ValueError("all requested metrics must use one explicit TR")
        cfg["TR"] = tr_values.pop()
        # Every metric is bound to a staged, typed mask.  Never fall back to
        # DPARSFA_run's hidden ``'Default'`` mask selection.
        cfg["MaskFile"] = request.mask_relative_path

        if request.alff_falff:
            alff_params = request.alff_falff
            if alff_params.filter_timing is TemporalFilterTiming.BEFORE_NORMALIZE:
                raise ValueError("BeforeNormalize filtering is incompatible with standard fALFF")
            cfg["CalALFF"] = {
                # Source names are counter-intuitive: high-pass stores the low cutoff.
                "AHighPass_LowCutoff": alff_params.frequency_band.low_hz,
                "ALowPass_HighCutoff": alff_params.frequency_band.high_hz,
            }
            if alff_params.filter_timing is TemporalFilterTiming.DISABLED:
                cfg["IsFilter"] = 0
            else:
                cfg["Filter"] = {"Timing": "AfterNormalize"}

        global_smoothing = bool(
            (request.alff_falff and request.alff_falff.result_smoothing)
            or (request.reho and request.reho.global_result_smoothing)
        )
        global_fwhm = (
            request.alff_falff.result_smoothing_fwhm_mm
            if request.alff_falff and request.alff_falff.result_smoothing
            else request.reho.global_result_smoothing_fwhm_mm
            if request.reho and request.reho.global_result_smoothing
            else None
        )

        if request.reho:
            reho_params = request.reho
            cfg["CalReHo"] = {
                "ClusterNVoxel": reho_params.cluster_voxels,
                "SmoothReHo": int(reho_params.smooth_reho),
            }
            if reho_params.temporal_filter_band is not None:
                assert reho_params.temporal_filter_add_mean_back is not None
                cfg["IsFilter"] = 1
                cfg["Filter"] = {
                    "Timing": "AfterNormalize",
                    "AHighPass_LowCutoff": reho_params.temporal_filter_band.low_hz,
                    "ALowPass_HighCutoff": reho_params.temporal_filter_band.high_hz,
                    "AAddMeanBack": int(reho_params.temporal_filter_add_mean_back),
                }
            else:
                cfg["IsFilter"] = 0
            if reho_params.smooth_reho:
                cfg["IsSmooth"] = 0
                cfg["Smooth"] = {
                    "Timing": "",
                    "FWHM": list(reho_params.smooth_reho_fwhm_mm or ()),
                }
        if global_smoothing:
            cfg["IsSmooth"] = 1
            cfg["Smooth"] = {"Timing": "OnResults", "FWHM": list(global_fwhm or ())}
        elif not (request.reho and request.reho.smooth_reho):
            cfg["IsSmooth"] = 0

        return DpabiCfgProjection(
            adapter_version=self.version,
            source_evidence=(
                "DPARSFA_run.m:3925-3972",
                "DPARSFA_run.m:3975-4008",
                "DPARSFA_run.m:4056-4095",
                "y_reho.m:69-75",
            ),
            cfg=cfg,
            cfg_hash=stable_hash(cfg),
        )

    def project_statistics(self, design: StatisticalDesignRevision) -> StatisticsCall:
        function = {
            StatisticalTest.ONE_SAMPLE_T: StatisticsFunction.TTEST1,
            StatisticalTest.INDEPENDENT_TWO_SAMPLE_T: StatisticsFunction.TTEST2,
            StatisticalTest.PAIRED_T: StatisticsFunction.TTEST_PAIRED,
            StatisticalTest.CORRELATION: StatisticsFunction.GROUP_ANALYSIS,
            StatisticalTest.REGRESSION: StatisticsFunction.GROUP_ANALYSIS,
        }[design.test]
        centered_columns = centered_covariate_columns(design)
        covariate_by_subject = {
            subject_id: tuple(column[index] for column in centered_columns)
            for index, subject_id in enumerate(design.subject_order)
        }
        if design.test is StatisticalTest.INDEPENDENT_TWO_SAMPLE_T:
            groups = tuple(
                tuple(image.artifact_id for image in design.images if image.group == group)
                for group in design.group_order
            )
            covariate_groups = tuple(
                tuple(
                    covariate_by_subject[image.subject_id]
                    for image in design.images
                    if image.group == group
                )
                for group in design.group_order
            )
        elif design.test is StatisticalTest.PAIRED_T:
            groups = tuple(
                tuple(image.artifact_id for image in design.images if image.condition == condition)
                for condition in design.condition_order
            )
            covariate_groups = tuple(
                tuple(covariate_by_subject[subject_id] for subject_id in design.subject_order)
                for _condition in design.condition_order
            )
        else:
            groups = (tuple(image.artifact_id for image in design.images),)
            covariate_groups = (
                tuple(covariate_by_subject[subject_id] for subject_id in design.subject_order),
            )
        body = {
            "function": function.value,
            "subjects": design.subject_order,
            "groups": groups,
            "covariate_groups": covariate_groups,
            "mask": design.mask_artifact_id,
            "matrix": design_matrix(design),
            "contrast": design.contrast,
            "one_sample_baseline": design.one_sample_baseline,
            "tail": design.tail.value,
            "revision": design.revision_id,
        }
        return StatisticsCall(
            function=function,
            ordered_subject_ids=design.subject_order,
            dependent_artifact_groups=groups,
            other_covariates_by_group=covariate_groups,
            mask_artifact_id=design.mask_artifact_id,
            design_matrix=design_matrix(design),
            contrast=design.contrast,
            one_sample_baseline=design.one_sample_baseline,
            tail=design.tail,
            residual_df=residual_degrees_of_freedom(design),
            revision_id=design.revision_id,
            design_hash=stable_hash(body),
        )

    def project_correction(self, correction: CorrectionSpec) -> CorrectionCall:
        if isinstance(correction, FdrCorrection):
            function = CorrectionFunction.FDR
            parameters = {
                "q_threshold": correction.q_threshold,
                "mask_artifact_id": correction.mask_artifact_id,
                "statistic_type": correction.statistic_type.value,
                "df1": correction.df1,
                "df2": correction.df2,
            }
        elif isinstance(correction, GrfCorrection):
            function = CorrectionFunction.GRF
            parameters = {
                "voxel_p_threshold": correction.voxel_p_threshold,
                "two_tailed": correction.two_tailed,
                "cluster_p_threshold": correction.cluster_p_threshold,
                "mask_artifact_id": correction.mask_artifact_id,
                "statistic_type": correction.statistic_type.value,
                "df1": correction.df1,
                "df2": correction.df2,
                "smoothness_mode": correction.smoothness_mode.value,
                "smoothness_dlh": correction.smoothness_dlh,
            }
        else:  # pragma: no cover - protected by the type contract
            raise TypeError("unsupported correction specification")
        return CorrectionCall(
            function=function,
            parameters=parameters,
            correction_hash=stable_hash({"function": function.value, "parameters": parameters}),
        )


def _project_nuisance(parameters: PreprocessingParameters) -> dict[str, Any]:
    nuisance = parameters.nuisance
    if not nuisance.enabled:
        return {"IsCovremove": 0}
    assert nuisance.timing is not None
    assert nuisance.polynomial_trend is not None
    assert nuisance.head_motion_model is not None
    assert nuisance.white_matter is not None
    assert nuisance.csf is not None
    assert nuisance.global_signal is not None
    assert nuisance.add_mean_back is not None
    covremove: dict[str, Any] = {
        "Timing": {
            NuisanceTiming.AFTER_REALIGN: "AfterRealign",
            NuisanceTiming.AFTER_NORMALIZE: "AfterNormalize",
        }[nuisance.timing],
        "PolynomialTrend": nuisance.polynomial_trend,
        "HeadMotion": int(nuisance.head_motion_model),
        "IsHeadMotionScrubbingRegressors": int(nuisance.head_motion_scrubbing is not None),
        "WM": _project_tissue_regressor(nuisance.white_matter),
        "CSF": _project_tissue_regressor(nuisance.csf),
        "WholeBrain": {
            "IsRemove": int(nuisance.global_signal.enabled),
            # The GUI-only rerun path is not invoked by DPARSFA_run automation.
            "IsBothWithWithoutGSR": 0,
        },
        "OtherCovariatesROI": [],
        "IsAddMeanBack": int(nuisance.add_mean_back),
    }
    compcor_counts = {
        regressor.compcor_components
        for regressor in (nuisance.white_matter, nuisance.csf)
        if regressor.enabled and regressor.method is TissueRegressorMethod.COMPCOR
    }
    if compcor_counts:
        # DPARSFA V8.2 reads only Cfg.Covremove.CSF.CompCorPCNum for the
        # combined WM/CSF CompCor mask, even when only WM is selected.
        covremove["CSF"]["CompCorPCNum"] = compcor_counts.pop()
    if nuisance.head_motion_scrubbing is not None:
        covremove["HeadMotionScrubbingRegressors"] = _project_motion_censoring(
            nuisance.head_motion_scrubbing
        )
    if nuisance.global_signal.enabled:
        assert nuisance.global_signal.mask_source is not None
        assert nuisance.global_signal.method is not None
        covremove["WholeBrain"].update(
            {
                "Mask": {
                    GlobalSignalMaskSource.SPM: "SPM",
                    GlobalSignalMaskSource.AUTO_MASK: "AutoMask",
                }[nuisance.global_signal.mask_source],
                "Method": "Mean",
            }
        )
    return {"IsCovremove": 1, "Covremove": covremove}


def _project_tissue_regressor(regressor: TissueRegressor) -> dict[str, Any]:
    if not regressor.enabled:
        return {"IsRemove": 0}
    assert regressor.mask_source is not None
    assert regressor.mask_threshold is not None
    assert regressor.method is not None
    projected: dict[str, Any] = {
        "IsRemove": 1,
        "Mask": {
            TissueMaskSource.SPM: "SPM",
            TissueMaskSource.SEGMENT: "Segment",
        }[regressor.mask_source],
        "MaskThreshold": regressor.mask_threshold,
        "Method": {
            TissueRegressorMethod.MEAN: "Mean",
            TissueRegressorMethod.COMPCOR: "CompCor",
        }[regressor.method],
    }
    return projected


def _project_motion_censoring(censoring: MotionCensoringParameters) -> dict[str, Any]:
    return {
        "FDType": {
            FdType.POWER: "FD_Power",
            FdType.JENKINSON: "FD_Jenkinson",
        }[censoring.fd_type],
        "FDThreshold": censoring.fd_threshold_mm,
        "PreviousPoints": censoring.previous_points,
        "LaterPoints": censoring.later_points,
    }


def _project_normalization(parameters: PreprocessingParameters) -> dict[str, Any]:
    normalization = parameters.normalization
    result: dict[str, Any] = {"IsNormalize": int(normalization.mode)}
    flags = {
        NormalizationMode.DISABLED: (0, 0, 0),
        NormalizationMode.EPI_TEMPLATE: (0, 0, 0),
        NormalizationMode.T1_SEGMENT: (1, 1, 0),
        NormalizationMode.DARTEL: (1, 2, 1),
    }[normalization.mode]
    result.update(
        {
            "IsNeedT1CoregisterToFun": flags[0],
            "IsSegment": flags[1],
            "IsDARTEL": flags[2],
        }
    )
    if normalization.mode is NormalizationMode.DISABLED:
        return result
    assert normalization.timing is not None
    assert normalization.bounding_box_mm is not None
    assert normalization.voxel_size_mm is not None
    result["Normalize"] = {
        "Timing": {
            NormalizationTiming.ON_FUNCTIONAL_DATA: "OnFunctionalData",
            NormalizationTiming.ON_RESULTS: "OnResults",
        }[normalization.timing],
        "BoundingBox": [list(row) for row in normalization.bounding_box_mm],
        "VoxSize": list(normalization.voxel_size_mm),
    }
    if normalization.affine_regularization is not None:
        result["Segment"] = {
            "AffineRegularisationInSegmentation": normalization.affine_regularization.value
        }
    return result


def _project_filter(parameters: PreprocessingParameters) -> dict[str, Any]:
    temporal_filter = parameters.temporal_filter
    if temporal_filter.timing is TemporalFilterTiming.DISABLED:
        return {"IsFilter": 0}
    assert temporal_filter.frequency_band is not None
    assert temporal_filter.add_mean_back is not None
    return {
        "IsFilter": 1,
        "Filter": {
            "Timing": {
                TemporalFilterTiming.BEFORE_NORMALIZE: "BeforeNormalize",
                TemporalFilterTiming.AFTER_NORMALIZE: "AfterNormalize",
            }[temporal_filter.timing],
            "AHighPass_LowCutoff": temporal_filter.frequency_band.low_hz,
            "ALowPass_HighCutoff": temporal_filter.frequency_band.high_hz,
            "AAddMeanBack": int(temporal_filter.add_mean_back),
        },
    }


def _project_scrubbing(parameters: PreprocessingParameters) -> dict[str, Any]:
    scrubbing = parameters.scrubbing
    if not scrubbing.enabled:
        return {"IsScrubbing": 0}
    assert scrubbing.timing is ScrubbingTiming.AFTER_PREPROCESSING
    assert scrubbing.censoring is not None
    assert scrubbing.method is not None
    projected = _project_motion_censoring(scrubbing.censoring)
    projected.update(
        {
            "Timing": "AfterPreprocessing",
            "ScrubbingMethod": scrubbing.method.value,
        }
    )
    return {"IsScrubbing": 1, "Scrubbing": projected}


def _project_smoothing(parameters: PreprocessingParameters) -> dict[str, Any]:
    smoothing = parameters.smoothing
    if smoothing.timing.value == "disabled":
        return {"IsSmooth": 0}
    assert smoothing.method is not None
    assert smoothing.fwhm_mm is not None
    return {
        "IsSmooth": int(smoothing.method),
        "Smooth": {
            "Timing": {
                "on_functional_data": "OnFunctionalData",
                "on_results": "OnResults",
            }[smoothing.timing.value],
            "FWHM": list(smoothing.fwhm_mm),
        },
    }
