from __future__ import annotations

from functools import lru_cache

from dispute_agent.skills.base import DisputeSkill, SkillManifest
from dispute_agent.skills.builtin import BUILTIN_SKILLS
from dispute_agent.skills.registry import SkillRegistry


@lru_cache(maxsize=1)
def get_skill_registry() -> SkillRegistry:
    return SkillRegistry(BUILTIN_SKILLS)


__all__ = ["DisputeSkill", "SkillManifest", "SkillRegistry", "get_skill_registry"]

