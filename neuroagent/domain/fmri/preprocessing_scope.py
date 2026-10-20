"""Shared input-scope rules for the current preprocessing executor."""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol


class PreprocessingSubject(Protocol):
    subject_id: str
    session_id: str | None


class PreprocessingSubjectScopeIssue(StrEnum):
    SUBJECTS_NOT_UNIQUE = "subjects_not_unique"
    MULTIPLE_SESSIONS = "multiple_sessions"
    CASE_INSENSITIVE_COLLISION = "case_insensitive_collision"
    UNSAFE_SUBJECT_ID = "unsafe_subject_id"


class PreprocessingSubjectScopeError(ValueError):
    """A subject/session identity that the current DPARSFA path cannot execute."""

    def __init__(self, issue: PreprocessingSubjectScopeIssue, message: str) -> None:
        super().__init__(message)
        self.issue = issue


def validate_preprocessing_subject_scope(subjects: Sequence[PreprocessingSubject]) -> None:
    """Validate the exact subject/session boundary required by current preprocessing."""

    ids = [subject.subject_id for subject in subjects]
    if not ids:
        raise PreprocessingSubjectScopeError(
            PreprocessingSubjectScopeIssue.SUBJECTS_NOT_UNIQUE,
            "preprocessing requires unique subjects in a single session",
        )
    if len({subject.session_id for subject in subjects}) != 1:
        raise PreprocessingSubjectScopeError(
            PreprocessingSubjectScopeIssue.MULTIPLE_SESSIONS,
            "multiple sessions are not supported by this preprocessing compiler",
        )
    if len(ids) != len(set(ids)):
        raise PreprocessingSubjectScopeError(
            PreprocessingSubjectScopeIssue.SUBJECTS_NOT_UNIQUE,
            "preprocessing requires unique subjects in a single session",
        )
    if len({subject_id.casefold() for subject_id in ids}) != len(ids):
        raise PreprocessingSubjectScopeError(
            PreprocessingSubjectScopeIssue.CASE_INSENSITIVE_COLLISION,
            "subject IDs collide on a case-insensitive filesystem",
        )
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", item) for item in ids):
        raise PreprocessingSubjectScopeError(
            PreprocessingSubjectScopeIssue.UNSAFE_SUBJECT_ID,
            "subject IDs contain unsafe path characters",
        )
