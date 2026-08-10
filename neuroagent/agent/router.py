"""Deterministic task-to-profile routing."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from neuroagent.agent.models import AgentTaskRequest, ModelProfile, RoutingDecision


class ModelRoutingError(RuntimeError):
    pass


class ModelRouter:
    def __init__(
        self,
        profiles: Sequence[ModelProfile],
        routes: Mapping[str, Sequence[str]],
    ) -> None:
        self._profiles = {profile.id: profile for profile in profiles}
        self._routes = {key: tuple(value) for key, value in routes.items()}

    @property
    def profiles(self) -> Mapping[str, ModelProfile]:
        return self._profiles

    def candidates(self, request: AgentTaskRequest) -> tuple[ModelProfile, ...]:
        ordered_ids: tuple[str, ...]
        if request.preferred_profile_id:
            ordered_ids = (request.preferred_profile_id,)
        else:
            configured = self._routes.get(request.task_type.value)
            ordered_ids = configured or tuple(
                profile.id
                for profile in sorted(self._profiles.values(), key=lambda item: item.priority)
            )
        candidates: list[ModelProfile] = []
        for profile_id in ordered_ids:
            profile = self._profiles.get(profile_id)
            if profile is None:
                continue
            if request.required_capabilities.issubset(profile.capabilities):
                candidates.append(profile)
        if not candidates:
            raise ModelRoutingError(
                f"no profile supports task {request.task_type.value} and required capabilities"
            )
        return tuple(candidates)

    def decision(
        self, request: AgentTaskRequest, candidates: Sequence[ModelProfile], selected: ModelProfile
    ) -> RoutingDecision:
        return RoutingDecision(
            task_type=request.task_type,
            selected_profile_id=selected.id,
            candidate_profile_ids=tuple(profile.id for profile in candidates),
            required_capabilities=request.required_capabilities,
            reason="explicit profile" if request.preferred_profile_id else "task capability route",
        )
