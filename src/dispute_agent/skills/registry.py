from __future__ import annotations

from collections import defaultdict

from dispute_agent.errors import NotFoundError, ValidationError
from dispute_agent.skills.base import DisputeSkill, SkillManifest


def _semver_key(version: str) -> tuple[int, int, int]:
    try:
        major, minor, patch = version.split(".")
        return int(major), int(minor), int(patch)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"Skill 版本不是合法语义版本: {version}") from exc


class SkillRegistry:
    """In-process registry with immutable name/version addressing."""

    def __init__(self, skills: list[DisputeSkill] | tuple[DisputeSkill, ...] = ()):
        self._skills: dict[tuple[str, str], DisputeSkill] = {}
        self._by_dispute_type: dict[str, list[DisputeSkill]] = defaultdict(list)
        for skill in skills:
            self.register(skill)

    def register(self, skill: DisputeSkill) -> None:
        manifest = skill.manifest
        key = (manifest.name, manifest.version)
        if key in self._skills:
            raise ValidationError(f"Skill 已注册: {manifest.name}@{manifest.version}")
        self._skills[key] = skill
        for dispute_type in manifest.dispute_types:
            self._by_dispute_type[dispute_type.value].append(skill)
            self._by_dispute_type[dispute_type.value].sort(
                key=lambda item: _semver_key(item.manifest.version),
                reverse=True,
            )

    def get(self, name: str, version: str | None = None) -> DisputeSkill:
        if version is not None:
            skill = self._skills.get((name, version))
            if skill is None:
                raise NotFoundError(f"Skill 不存在: {name}@{version}")
            return skill
        candidates = [skill for (skill_name, _), skill in self._skills.items() if skill_name == name]
        if not candidates:
            raise NotFoundError(f"Skill 不存在: {name}")
        return max(candidates, key=lambda item: _semver_key(item.manifest.version))

    def for_dispute_type(self, dispute_type: str) -> tuple[DisputeSkill, ...]:
        return tuple(self._by_dispute_type.get(dispute_type, ()))

    def manifests(self) -> tuple[SkillManifest, ...]:
        skills = sorted(
            self._skills.values(),
            key=lambda item: (item.manifest.name, _semver_key(item.manifest.version)),
        )
        return tuple(item.manifest for item in skills)

    def resolve_bound_skill(self, *, name: str | None, version: str | None) -> DisputeSkill | None:
        if name is None and version is None:
            return None
        if not name or not version:
            raise ValidationError("Claim 的 skill_name 和 skill_version 必须同时存在")
        return self.get(name, version)

