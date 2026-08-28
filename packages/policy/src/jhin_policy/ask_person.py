"""The capability for asking the person you are talking to a question.

Deliberately its own capability rather than a corner of an existing one: an
admin who wants a particular agent to stop interrupting people should be able
to say exactly that, with one deny grant, without also taking away its
ability to remember or to ask a colleague.

The capability is not the whole lock. ``organization.ask_person`` is
advertised only on a chat turn and denied everywhere else by its validator
(``jhin_tools.ask_person.validate_ask_person``), so a triggered run, a
delegated child, and an accepted work request cannot reach a person at all —
they decide for themselves and say what they assumed.

This module is pure (no I/O).
"""

ASK_PERSON_CAPABILITY = "organization.ask_person"

__all__ = ["ASK_PERSON_CAPABILITY"]
