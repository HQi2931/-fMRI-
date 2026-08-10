"""Errors exposed by application use cases.

The API layer maps these errors to the stable error envelope.  Domain and
workflow code can therefore stay independent of FastAPI.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class ApplicationError(Exception):
    """A safe, structured error that may be returned to a local API client."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = dict(details or {})


class NotFoundError(ApplicationError):
    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            "not_found",
            f"{resource} 不存在。",
            status_code=404,
            details={"resource": resource, "resource_id": resource_id},
        )


class ConflictError(ApplicationError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(code, message, status_code=409, details=details)


class InputValidationError(ApplicationError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(code, message, status_code=422, details=details)


class PathPolicyError(ApplicationError):
    def __init__(self, message: str, *, path: str) -> None:
        super().__init__(
            "path_not_allowed",
            message,
            status_code=403,
            details={"path": path},
        )


class ApprovalGateError(ApplicationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("approval_required", message, status_code=409, details=details)
