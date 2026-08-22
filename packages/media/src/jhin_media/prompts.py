"""Avatar generation prompts built from *public* identity only.

The inputs are exactly the fields any workspace viewer can already see on the
agent card (name, role title, public purpose, expertise tags) plus the
explicit hint the requesting admin typed. System prompts, memory, and
conversations never reach this function.
"""

from __future__ import annotations

MAX_PROMPT_HINT_LENGTH = 300
_STYLE = (
    "Stylized editorial illustration portrait avatar, flat vector shapes, bold "
    "simplified forms, limited palette, centered subject on a plain background, "
    "square composition, no text, no logos, not photorealistic."
)


def _clean(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def build_avatar_prompt(
    *,
    name: str,
    role_title: str,
    public_purpose: str,
    expertise: list[str],
    prompt_hint: str = "",
) -> str:
    parts = [_STYLE, f"Subject: an AI teammate called {_clean(name, 200)}."]
    if role_title.strip():
        parts.append(f"Role: {_clean(role_title, 200)}.")
    if public_purpose.strip():
        parts.append(f"Purpose: {_clean(public_purpose, 400)}.")
    tags = [_clean(tag, 64) for tag in expertise if tag.strip()][:10]
    if tags:
        parts.append("Themes: " + ", ".join(tags) + ".")
    hint = _clean(prompt_hint, MAX_PROMPT_HINT_LENGTH)
    if hint:
        parts.append(f"Art direction from the requester: {hint}.")
    return " ".join(parts)
