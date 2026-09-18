"""Thinking time, with the person's own waiting taken back out of it."""

from datetime import UTC, datetime, timedelta

from jhin_domain import Wait, working_time

START = datetime(2026, 9, 6, 12, 16, 13, tzinfo=UTC)


def at(seconds: float) -> datetime:
    """A moment this many seconds after the run started."""
    return START + timedelta(seconds=seconds)


def test_a_run_nobody_interrupted_counts_from_its_own_start() -> None:
    answer = working_time(START, [])

    assert answer.working_since == START
    assert answer.working_seconds == 0


def test_a_run_that_never_started_has_nothing_to_count() -> None:
    answer = working_time(None, [Wait(at(1), at(2))])

    assert answer.working_since is None
    assert answer.working_seconds == 0


def test_the_wait_for_a_person_is_not_thinking_time() -> None:
    """The live case, to the second.

    Run ``01a076a5-d23f-7691-bb64-21f265ce2a3a`` on this workspace ran for
    20m01s. It asked a question 16.9s in and the answer came 19m10s later.
    The agent thought for fifty seconds; the pill said twenty minutes.
    """
    asked = at(16.933)
    answered = at(16.933 + 1150.574)

    answer = working_time(START, [Wait(asked, answered)])

    assert answer.working_seconds == 17
    assert answer.working_since == answered
    # What a client counting from `working_since` shows when the run ends
    # 33.5s later: seconds, where the wall clock said twenty minutes.
    finished = at(1200.976)
    shown = answer.working_seconds + int((finished - answer.working_since).total_seconds())
    assert shown == 50
    assert int((finished - START).total_seconds()) == 1200


def test_a_wait_still_open_stops_the_clock_rather_than_running_it() -> None:
    """An approval left overnight must not read as a night of thinking."""
    answer = working_time(START, [Wait(at(10))])

    assert answer.working_since is None
    assert answer.working_seconds == 10


def test_every_wait_in_a_turn_comes_out_not_just_the_last() -> None:
    answer = working_time(
        START,
        [Wait(at(10), at(70)), Wait(at(100), at(400))],
    )

    assert answer.working_seconds == 40  # 10s, then 30s between the two waits
    assert answer.working_since == at(400)


def test_two_waits_at_once_park_the_run_once() -> None:
    """A question and an approval can overlap; the run is stopped, not doubly
    stopped, and subtracting both would invent thinking time."""
    answer = working_time(
        START,
        [Wait(at(10), at(100)), Wait(at(40), at(70))],
    )

    assert answer.working_seconds == 10
    assert answer.working_since == at(100)


def test_a_wait_swallowed_by_a_longer_one_changes_nothing() -> None:
    assert working_time(START, [Wait(at(10), at(100)), Wait(at(20), at(30))]) == working_time(
        START, [Wait(at(10), at(100))]
    )


def test_waits_arrive_in_any_order() -> None:
    assert working_time(START, [Wait(at(100), at(400)), Wait(at(10), at(70))]) == working_time(
        START, [Wait(at(10), at(70)), Wait(at(100), at(400))]
    )


def test_a_wait_inherited_from_before_the_run_is_clamped_to_it() -> None:
    """A row that opened before this run began cannot buy back time the run
    never had — nor can it push the answer negative."""
    answer = working_time(START, [Wait(at(-600), at(60))])

    assert answer.working_seconds == 0
    assert answer.working_since == at(60)


def test_a_wait_that_closed_before_the_run_began_is_not_this_run_s_wait() -> None:
    answer = working_time(START, [Wait(at(-600), at(-300))])

    assert answer.working_since == START
    assert answer.working_seconds == 0


def test_an_open_wait_wins_over_a_closed_one_it_overlaps() -> None:
    """Whatever else finished, a run with something still pending is parked."""
    answer = working_time(START, [Wait(at(10), at(400)), Wait(at(50))])

    assert answer.working_since is None
    assert answer.working_seconds == 10


def test_a_wait_stamped_after_the_run_ended_is_not_this_run_s_wait() -> None:
    """The live row that made this argument.

    Approval ``01a075a5-1c5e-7153-9343-1bba04100e4b`` on this workspace is
    still ``pending`` and was requested 2h19m after run
    ``01a07525-8543-75b2-9285-600edffb8054`` had already completed — the run
    lived 58 seconds. A walk that trusted the row banked every second from the
    run's start to the request and reported 8361 seconds of thinking. A row
    stamped outside the run's window is not a wait the run ever took.
    """
    answer = working_time(at(0), [Wait(at(8361.756))], ended_at=at(57.961))

    assert answer.working_seconds == 58
    assert answer.working_since is None


def test_a_finished_run_banks_its_last_stretch_instead_of_running_forever() -> None:
    """`working_since` is an instant a client counts from *to now*. Handing one
    out for a run that stopped an hour ago is a clock that never stops."""
    answer = working_time(START, [Wait(at(10), at(70))], ended_at=at(100))

    assert answer.working_since is None
    assert answer.working_seconds == 40  # 10s before the wait, 30s after it


def test_a_wait_that_outlived_the_run_is_clipped_to_it() -> None:
    """A row decided long after the run stopped stops being a wait when the run
    does — the run was not parked on it once there was no run to park."""
    answer = working_time(START, [Wait(at(10), at(900))], ended_at=at(60))

    assert answer.working_since is None
    assert answer.working_seconds == 10


def test_a_run_still_parked_when_it_ended_banks_only_what_it_thought() -> None:
    answer = working_time(START, [Wait(at(10))], ended_at=at(600))

    assert answer.working_since is None
    assert answer.working_seconds == 10


def test_a_run_with_no_span_of_its_own_has_nothing_to_divide_up() -> None:
    """Two clocks disagreeing, or a stamp written out of order."""
    answer = working_time(START, [Wait(at(1), at(2))], ended_at=at(-5))

    assert answer.working_since is None
    assert answer.working_seconds == 0


def test_whole_seconds_are_the_nearest_ones_not_the_ones_below() -> None:
    """Truncation charged the reader up to a second per wait in a turn.

    Approval ``01a0751f-5d26-7df0-9070-f002daec2404``: requested 8.847s into an
    11.433s run and decided 0.349s later. Eleven seconds of thinking, and
    ``int()`` filed it as ten.
    """
    answer = working_time(at(0), [Wait(at(8.847), at(9.196))], ended_at=at(11.433))

    assert answer.working_seconds == 11

    # Half-up, and the same at every scale: `round()` would send 50.5 back as
    # 50, being the nearer even second.
    assert working_time(at(0), ended_at=at(50.5)).working_seconds == 51
    assert working_time(at(0), ended_at=at(49.5)).working_seconds == 50
    assert working_time(at(0), ended_at=at(0.4)).working_seconds == 0
