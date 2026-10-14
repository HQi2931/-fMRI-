"""Canonical expected outputs for DPABI preprocessing plans."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, field_validator, model_validator

from neuroagent.domain.fmri.artifacts import FrozenModel, MetricScaling
from neuroagent.domain.fmri.metrics import AlffFalffParameters, MetricKind, RehoParameters

PreprocessingMetric = Literal["timeseries", "brain_mask", "alff", "falff", "reho"]


class DpabiPreprocessingOutput(FrozenModel):
    """One subject-level DPABI output and its registered Artifact identity."""

    subject_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    metric: PreprocessingMetric
    scaling: MetricScaling | None = None
    relative_path: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1)

    @field_validator("relative_path")
    @classmethod
    def path_is_relative_and_confined(cls, value: str) -> str:
        path = PurePosixPath(value.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("preprocessing output paths must remain within the run directory")
        return path.as_posix()

    @model_validator(mode="after")
    def output_identity_is_consistent(self) -> DpabiPreprocessingOutput:
        if self.metric == "timeseries":
            expected_type = "preprocessing.functional_timeseries"
            expected_path = f"output/{self.subject_id}/timeseries.nii"
            if self.scaling is not None:
                raise ValueError("timeseries output must not declare metric scaling")
        elif self.metric == "brain_mask":
            expected_type = "preprocessing.brain_mask"
            expected_path = f"output/{self.subject_id}/brain_mask.nii"
            if self.scaling is not None:
                raise ValueError("brain mask output must not declare metric scaling")
        else:
            if self.scaling is None:
                raise ValueError("metric map outputs must declare metric scaling")
            expected_type = f"metric.{self.metric}.{self.scaling.value}"
            expected_path = f"output/{self.subject_id}/{self.scaling.value}_{self.metric}.nii"
        if self.artifact_type != expected_type or self.relative_path != expected_path:
            raise ValueError("preprocessing output Artifact type/path does not match its identity")
        return self


def enumerate_dpabi_preprocessing_outputs(
    subject_ids: tuple[str, ...],
    alff: AlffFalffParameters | None,
    reho: RehoParameters | None,
    *,
    auto_mask: bool = False,
) -> tuple[DpabiPreprocessingOutput, ...]:
    """Enumerate the canonical per-subject DPABI outputs in execution order.

    The executor compiler uses this contract, and WorkTask readiness checks can
    use the same enumerator so subject, metric, scaling, path, and registered
    type identities do not drift between planning and verification.
    """

    outputs: list[DpabiPreprocessingOutput] = []
    for subject_id in subject_ids:
        outputs.append(
            DpabiPreprocessingOutput(
                subject_id=subject_id,
                metric="timeseries",
                scaling=None,
                relative_path=f"output/{subject_id}/timeseries.nii",
                artifact_type="preprocessing.functional_timeseries",
            )
        )
        if auto_mask:
            outputs.append(
                DpabiPreprocessingOutput(
                    subject_id=subject_id,
                    metric="brain_mask",
                    scaling=None,
                    relative_path=f"output/{subject_id}/brain_mask.nii",
                    artifact_type="preprocessing.brain_mask",
                )
            )
        for parameters in (alff, reho):
            if parameters is None:
                continue
            metrics = (
                parameters.requested_metrics
                if isinstance(parameters, AlffFalffParameters)
                else (MetricKind.REHO,)
            )
            if len(set(parameters.requested_scalings)) != len(parameters.requested_scalings):
                raise ValueError("requested metric scalings must be unique")
            for metric in metrics:
                for scaling in parameters.requested_scalings:
                    metric_name = metric.value
                    scaling_name = scaling.value
                    outputs.append(
                        DpabiPreprocessingOutput(
                            subject_id=subject_id,
                            metric=metric_name,
                            scaling=scaling,
                            relative_path=(f"output/{subject_id}/{scaling_name}_{metric_name}.nii"),
                            artifact_type=f"metric.{metric_name}.{scaling_name}",
                        )
                    )
    return tuple(outputs)
