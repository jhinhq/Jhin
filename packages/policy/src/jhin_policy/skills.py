"""Skills capabilities (docs/architecture/skills.md).

``skills.read`` gates the gateway tool that returns a skill's full
instructions. It is deliberately NOT granted by default: the prompt lists
only names and descriptions of the skills enabled for an agent, and the
agent needs this capability to read the bodies. Grants may pin a ``name``
scope (fnmatch pattern) to restrict which skills are readable.
"""

from __future__ import annotations

SKILLS_READ_CAPABILITY = "skills.read"

SKILLS_CAPABILITIES: tuple[str, ...] = (SKILLS_READ_CAPABILITY,)

__all__ = ["SKILLS_CAPABILITIES", "SKILLS_READ_CAPABILITY"]
