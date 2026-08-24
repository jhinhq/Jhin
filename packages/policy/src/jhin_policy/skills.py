"""Skills capabilities (docs/architecture/skills.md).

``skills.read`` gates the gateway tool that returns a skill's full
instructions. It is deliberately NOT granted by default: the prompt lists
only names and descriptions of the skills enabled for an agent, and the
agent needs this capability to read the bodies. Grants may pin a ``name``
scope (fnmatch pattern) to restrict which skills are readable.

``skills.manage`` gates ``skills.create`` and ``skills.update`` — an agent
authoring persistent workspace configuration (new instruction packs every
other agent may come to read). Elevated risk, approval-gated by default,
same posture as ``organization.create_agent``.
"""

from __future__ import annotations

SKILLS_READ_CAPABILITY = "skills.read"
SKILLS_MANAGE_CAPABILITY = "skills.manage"

SKILLS_CAPABILITIES: tuple[str, ...] = (SKILLS_READ_CAPABILITY, SKILLS_MANAGE_CAPABILITY)

__all__ = ["SKILLS_CAPABILITIES", "SKILLS_MANAGE_CAPABILITY", "SKILLS_READ_CAPABILITY"]
