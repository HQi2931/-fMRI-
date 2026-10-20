"""Explicit transport-to-application message mapping helpers."""

from __future__ import annotations

from types import UnionType
from typing import Any, TypeVar, Union, get_args, get_origin

from pydantic import BaseModel

HttpMessage = TypeVar("HttpMessage", bound=BaseModel)
ApplicationMessage = TypeVar("ApplicationMessage", bound=BaseModel)


def to_application_message(
    message: HttpMessage, target: type[ApplicationMessage]
) -> ApplicationMessage:
    """Map a validated HTTP DTO into an independently owned use-case message."""

    # Keep fields that were actually supplied by the HTTP client distinguishable
    # from DTO defaults. Application commands may use that presence information
    # for compatibility and explicit-intent checks.
    return target.model_validate(message.model_dump(mode="python", exclude_unset=True))


def to_http_response(message: object, target: Any) -> Any:
    """Map application results into API-owned response DTOs at the HTTP edge."""

    origin = get_origin(target)
    arguments = get_args(target)
    if origin is list and arguments:
        if isinstance(message, list):
            return [to_http_response(item, arguments[0]) for item in message]
        return message
    if origin is tuple and arguments:
        item_target = arguments[0]
        if isinstance(message, (list, tuple)):
            return tuple(to_http_response(item, item_target) for item in message)
        return message
    if origin in (Union, UnionType):
        if message is None:
            return None
        for option in arguments:
            if isinstance(option, type) and isinstance(message, option):
                return to_http_response(message, option)
            if isinstance(option, type) and issubclass(option, BaseModel):
                try:
                    return to_http_response(message, option)
                except (TypeError, ValueError):
                    continue
        return message
    if isinstance(target, type) and issubclass(target, BaseModel):
        data = message.model_dump(mode="python") if isinstance(message, BaseModel) else message
        return target.model_validate(data)
    return message
