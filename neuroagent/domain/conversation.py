"""Stable values that describe conversations and persistent user memory."""

from __future__ import annotations

from enum import StrEnum


class ConversationMode(StrEnum):
    CHAT = "chat"
    WORK = "work"


class ConversationAction(StrEnum):
    AUTO = "auto"
    SETUP_WORKSPACE = "setup_workspace"
    PREPARE_PREPROCESSING_PLAN = "prepare_preprocessing_plan"
    PREVIEW_PREPROCESSING_RUN = "preview_preprocessing_run"
    START_PREPROCESSING = "start_preprocessing"
    CREATE_HEAD_MOTION_QC = "create_head_motion_qc"
    GET_PROGRESS = "get_progress"
    GET_QC_STATUS = "get_qc_status"
    APPROVE_QC_REVIEW = "approve_qc_review"
    SKIP_QC = "skip_qc"
    GET_STATISTICAL_RESULTS = "get_statistical_results"
    PROPOSE_WORKSPACE_CHANGES = "propose_workspace_changes"


class MemoryScope(StrEnum):
    CONVERSATION = "conversation"
    PROJECT = "project"


class MemoryKind(StrEnum):
    PREFERENCE = "preference"
    INSTRUCTION = "instruction"
    SCIENTIFIC_PARAMETER = "scientific_parameter"
    PROJECT_FACT = "project_fact"
    DECISION = "decision"


class MemoryStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    FORGOTTEN = "forgotten"


class MemoryAction(StrEnum):
    CONFIRM = "confirm"
    UPDATE = "update"
    REJECT = "reject"
    PIN = "pin"
    UNPIN = "unpin"
    FORGET = "forget"
    ACCEPT_PROPOSAL = "accept_proposal"
    MERGE_PROPOSAL = "merge_proposal"
    REJECT_PROPOSAL = "reject_proposal"
