"""Safe bounded display tails shared by live, terminal and recovery writes."""

from jhin_secrets import get_redactor

# Wire notice used by the sandbox runner. Keep it outside the stream body
# while redacting so it cannot hide an unfinished worker-only credential.
RUNNER_TRUNCATION_NOTICE = "\n…[truncated by sandbox runner]"


def sanitized_output_tail(value: object, *, max_chars: int = 8_192) -> str:
    text = str(value or "")
    notice = RUNNER_TRUNCATION_NOTICE if text.endswith(RUNNER_TRUNCATION_NOTICE) else ""
    body = text[: -len(notice)] if notice else text
    safe = get_redactor().redact_partial_text(body, clipped_start=True).replace("\x00", "?")
    room = max(0, max_chars - len(notice))
    return (str(safe)[-room:] if room else "") + notice
