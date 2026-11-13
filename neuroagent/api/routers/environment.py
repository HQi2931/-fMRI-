"""HTTP endpoints for the environment capability."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request

from neuroagent.api.responses.environment import (
    EnvironmentConfigView,
    EnvironmentProbeView,
    HealthView,
)
from neuroagent.api.routers.common import capability_from
from neuroagent.api.schemas.environment import EnvironmentConfigUpdate
from neuroagent.api.schemas.mapping import to_application_message, to_http_response
from neuroagent.application.contracts.environment import (
    EnvironmentConfigUpdate as AppEnvironmentConfigUpdate,
)

router = APIRouter()


@router.get("/health", response_model=HealthView, tags=["system"])
def health(request: Request) -> HealthView:
    return cast(
        HealthView, to_http_response(capability_from(request, "environment").health(), HealthView)
    )


@router.get("/environment/probe", response_model=EnvironmentProbeView, tags=["system"])
def environment_probe(request: Request) -> EnvironmentProbeView:
    return cast(
        EnvironmentProbeView,
        to_http_response(
            capability_from(request, "environment").environment_probe(), EnvironmentProbeView
        ),
    )


@router.get("/environment/config", response_model=EnvironmentConfigView, tags=["system"])
def environment_config(request: Request) -> EnvironmentConfigView:
    return cast(
        EnvironmentConfigView,
        to_http_response(
            capability_from(request, "environment").environment_config(), EnvironmentConfigView
        ),
    )


@router.put("/environment/config", response_model=EnvironmentConfigView, tags=["system"])
def update_environment_config(
    body: EnvironmentConfigUpdate, request: Request
) -> EnvironmentConfigView:
    return cast(
        EnvironmentConfigView,
        to_http_response(
            capability_from(request, "environment").update_environment_config(
                to_application_message(body, AppEnvironmentConfigUpdate)
            ),
            EnvironmentConfigView,
        ),
    )
