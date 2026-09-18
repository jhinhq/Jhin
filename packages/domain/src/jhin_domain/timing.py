"""How long an agent has actually been thinking, when a person was in the loop.

A turn is one ``agent_run``. Its ``started_at`` is stamped once, when the run
begins, and is never re-stamped: when an approval is decided or a question is
answered the run goes back to ``running`` carrying the same ``started_at``.
Wall-clock elapsed time from that stamp is therefore not the agent's thinking
time — it is the agent's thinking time *plus every minute the person took to
decide*. On this workspace's own database, run
``01a076a5-d23f-7691-bb64-21f265ce2a3a`` ran from 12:16:13 to 12:36:14 and
spent 19m10s of that waiting for somebody to answer a question: twenty minutes
of "thinking", fifty seconds of thought.

This module is the correction, and it is a *derivation* rather than a new
column, deliberately:

* ``started_at`` keeps its meaning. It is what the run metrics, the duration
  histogram and every audit read as "when this run began", and re-stamping it
  on resume would silently redefine all of them — while still counting the
  wait, since the pause before the re-stamp is exactly the interval nobody
  measured.
* The waits are already recorded, to the millisecond, by the rows that *are*
  the waits: an approval knows when it was requested and when it was decided,
  a question when it was asked and answered, a review when it was raised and
  ruled on. Those rows are the authority on how long a person took, so the
  number is computed from them rather than from a second copy that a crashed
  worker could fail to write.
* It is retroactive. Runs already in the database get the right number, which
  a field only written from now on could never do.

The unit of the answer is a pair, not a duration, because the client is
counting: :attr:`WorkingTime.working_seconds` is the thinking already banked
and :attr:`WorkingTime.working_since` is the instant the current stretch of
thinking began, so a browser shows ``banked + (now - since)`` and ticks it
locally. While the run is parked on somebody, ``working_since`` is ``None`` —
there is no stretch in progress, and a surface with no instant to count from
shows no clock rather than a wrong one.

The run's own lifetime is the outer bound on all of it. A wait row points at a
run but is not owned by it: nothing stops an approval from being requested
after the run it names has already finished, and this workspace's database has
exactly that — approval ``01a075a5-1c5e-7153-9343-1bba04100e4b`` was requested
2h19m after run ``01a07525-8543-75b2-9285-600edffb8054`` completed, and a walk
that trusted the row printed 8361 seconds of thinking for a run that lived 58.
So ``ended_at`` is asked for, waits outside the run's span are dropped, waits
that outlive it are clipped to it, and a finished run banks its last stretch
rather than handing a client an instant to keep counting from forever.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

__all__ = ["Wait", "WorkingTime", "working_time"]


@dataclass(frozen=True, slots=True)
class Wait:
    """One span a run spent parked on somebody else's decision.

    ``ended_at`` is ``None`` while the wait is still open — nobody has decided
    yet — which is a different fact from a wait that ended a moment ago, and
    the two produce different answers below.
    """

    started_at: datetime
    ended_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class WorkingTime:
    """Thinking time, split into what is settled and what is still running."""

    #: When the current uninterrupted stretch of work began, or ``None`` when
    #: there is no stretch in progress to count: the run is parked on somebody
    #: right now, it has finished and everything it did is in
    #: :attr:`working_seconds`, or it never had a span this could be read from
    #: (no start stamp, or an end at or before the start). The four are one
    #: value here on purpose — every one of them means "show no clock" — so a
    #: caller that wants to *say* which is which has to carry the reason
    #: itself rather than infer it from the ``None``.
    working_since: datetime | None
    #: Whole seconds of thinking that finished before ``working_since``.
    working_seconds: int


def _whole_seconds(banked: float) -> int:
    """Whole seconds, to the nearest one rather than down to it.

    ``int()`` truncates, so every wait in a run cost the total up to a second:
    two consecutive waits banking 49.5s and 11.3s of thinking reported 49 and
    11. Half-up by hand rather than :func:`round`, which rounds halves to the
    nearest *even* second and would send 50.5 back as 50.
    """
    return max(0, int(banked + 0.5))


def working_time(
    started_at: datetime | None,
    waits: Iterable[Wait] = (),
    *,
    ended_at: datetime | None = None,
) -> WorkingTime:
    """Split a run's life into thinking and waiting, and return the thinking.

    Pure and total. Overlapping waits are merged rather than counted twice —
    an approval and a question can be open at the same moment and the run is
    parked once, not twice — and a wait that began before the run started
    (a row inherited by a resumed run) is clamped to the run's own start.

    A wait that is still open ends the walk: everything after it has not
    happened yet, and there is no stretch of work in progress to count from.

    ``ended_at`` is when the run finished, and passing it is what keeps the
    answer inside the run's own life. A wait row names a run but is not owned
    by one, and a row stamped outside the run's window is not a wait this run
    ever took: one begun at or after ``ended_at`` is dropped, and one that
    outlives the run is clipped to it. With an ``ended_at`` the walk also
    finishes the run rather than leaving it open — the last stretch is banked
    and ``working_since`` is ``None``, because a client counts from
    ``working_since`` to *now* and a run that stopped an hour ago must not go
    on collecting hours. Leave it ``None`` for a run still in flight.
    """
    if started_at is None:
        return WorkingTime(working_since=None, working_seconds=0)
    if ended_at is not None and ended_at <= started_at:
        # A run with no span of its own — a stamp not yet written, or two
        # clocks disagreeing. There is no thinking to divide up.
        return WorkingTime(working_since=None, working_seconds=0)

    spans: list[tuple[datetime, datetime | None]] = []
    for wait in waits:
        if wait.ended_at is not None and wait.ended_at <= started_at:
            continue  # Over before the run began.
        start = max(wait.started_at, started_at)
        if ended_at is not None and start >= ended_at:
            continue  # Opened after the run was already over.
        end = wait.ended_at
        if end is not None and ended_at is not None and end > ended_at:
            end = ended_at  # Outlived the run; the run stopped waiting when it stopped.
        spans.append((start, end))
    spans.sort(key=lambda span: span[0])

    cursor = started_at
    banked = 0.0
    for start, end in spans:
        if end is not None and end <= cursor:
            # Wholly inside a wait already accounted for.
            continue
        if start > cursor:
            banked += (start - cursor).total_seconds()
        if end is None:
            # Still parked. Nothing after this has been worked yet.
            return WorkingTime(working_since=None, working_seconds=_whole_seconds(banked))
        cursor = max(cursor, end)
    if ended_at is not None:
        # The run is over: its last stretch is banked, not still running.
        if ended_at > cursor:
            banked += (ended_at - cursor).total_seconds()
        return WorkingTime(working_since=None, working_seconds=_whole_seconds(banked))
    return WorkingTime(working_since=cursor, working_seconds=_whole_seconds(banked))
