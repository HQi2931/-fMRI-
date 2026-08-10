"""In-memory, read-only-after-freeze Skill registry."""

from __future__ import annotations

from collections.abc import Iterable

from neuroagent.skills.models import SkillSpec, SkillStatus


class SkillRegistryError(ValueError):
    pass


class SkillRegistry:
    def __init__(self) -> None:
        self._specs: dict[tuple[str, str], SkillSpec] = {}
        self._frozen = False

    def register(self, spec: SkillSpec) -> None:
        if self._frozen:
            raise SkillRegistryError("Skill registry is frozen")
        key = (spec.skill_id, spec.version)
        if key in self._specs:
            raise SkillRegistryError(f"duplicate Skill version: {spec.skill_id}@{spec.version}")
        self._specs[key] = spec

    def freeze(self) -> None:
        self._frozen = True

    def list(self, *, include_deprecated: bool = False) -> tuple[SkillSpec, ...]:
        specs: Iterable[SkillSpec] = self._specs.values()
        if not include_deprecated:
            specs = (spec for spec in specs if spec.status is not SkillStatus.DEPRECATED)
        return tuple(sorted(specs, key=lambda spec: (spec.skill_id, _semver(spec.version))))

    def resolve(self, skill_id: str, version: str | None = None) -> SkillSpec:
        if version is not None:
            try:
                return self._specs[(skill_id, version)]
            except KeyError as exc:
                raise SkillRegistryError(f"unknown Skill: {skill_id}@{version}") from exc
        candidates = [
            spec
            for (candidate_id, _), spec in self._specs.items()
            if candidate_id == skill_id and spec.status is SkillStatus.REVIEWED
        ]
        if not candidates:
            raise SkillRegistryError(f"no reviewed version for Skill: {skill_id}")
        return max(candidates, key=lambda spec: _semver(spec.version))


def _semver(version: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in version.split("."))  # type: ignore[return-value]
