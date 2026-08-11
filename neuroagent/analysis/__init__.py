"""Deterministic analysis helpers used by approval-gated rs-fMRI workflows."""

from neuroagent.analysis.clusters import localize_clusters, parse_cluster_table
from neuroagent.analysis.diagnostics import diagnose_dpabi_log
from neuroagent.analysis.models import (
    AtlasPoint,
    ClusterRecord,
    FailureDiagnosis,
    MlDesignRecommendation,
    RoiSignalRecord,
    TableInspection,
)
from neuroagent.analysis.rag import answer_rsfmri_question, search_evidence
from neuroagent.analysis.roi import build_roi_tables, validate_roi_request
from neuroagent.analysis.tables import inspect_table
from neuroagent.analysis.templates import generate_ml_template

__all__ = [
    "AtlasPoint",
    "ClusterRecord",
    "FailureDiagnosis",
    "MlDesignRecommendation",
    "RoiSignalRecord",
    "TableInspection",
    "answer_rsfmri_question",
    "build_roi_tables",
    "diagnose_dpabi_log",
    "generate_ml_template",
    "inspect_table",
    "localize_clusters",
    "parse_cluster_table",
    "search_evidence",
    "validate_roi_request",
]
