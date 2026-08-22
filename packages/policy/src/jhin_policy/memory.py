"""Memory capabilities (experience design, Memory).

Both tools are gateway-mediated and scoped to the calling agent by the
executor: ``memory.search`` only ever returns records the agent is authorized
for *right now*, and ``memory.propose`` routes through deterministic policy
(it can never activate workspace memory or broaden visibility).
"""

MEMORY_READ_CAPABILITY = "memory.read"
MEMORY_PROPOSE_CAPABILITY = "memory.propose"

MEMORY_CAPABILITIES: tuple[str, ...] = (MEMORY_READ_CAPABILITY, MEMORY_PROPOSE_CAPABILITY)

__all__ = ["MEMORY_CAPABILITIES", "MEMORY_PROPOSE_CAPABILITY", "MEMORY_READ_CAPABILITY"]
