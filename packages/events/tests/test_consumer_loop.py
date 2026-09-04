"""The pull-consumer loop's treatment of an idle window.

An idle fetch is the normal case: no events arrived inside the timeout. It
used to escape the loop and take the whole event worker down through its
TaskGroup, because ``nats.errors.TimeoutError`` is a *subclass* of the
built-in ``TimeoutError`` while ``nats.js`` raises the built-in one directly,
so catching only the library's class missed it. On the operator's instance
that showed as a worker restarting every few minutes against an empty queue.
"""

from __future__ import annotations

import asyncio
from typing import Any

import nats.errors
import pytest
from opentelemetry.trace import NoOpTracer

from jhin_events import consumer as consumer_module
from jhin_events.consumer import run_pull_consumer


class _Subscription:
    """A pull subscription that raises once, then keeps the loop honest.

    It owns the stop event: the loop is proved to have come round again by
    the second fetch happening at all.
    """

    def __init__(self, first: BaseException, stop: asyncio.Event) -> None:
        self._first = first
        self._stop = stop
        self.fetches = 0

    async def fetch(self, batch: int, **kwargs: Any) -> list[Any]:
        self.fetches += 1
        if self.fetches == 1:
            raise self._first
        self._stop.set()
        return []


class _JetStream:
    def __init__(self, subscription: _Subscription) -> None:
        self._subscription = subscription

    async def pull_subscribe_bind(self, durable: str, stream: str) -> _Subscription:
        return self._subscription


async def _noop_ensure(*args: Any, **kwargs: Any) -> None:
    return None


@pytest.mark.parametrize(
    "idle",
    [
        pytest.param(TimeoutError(), id="builtin-the-one-nats-js-raises"),
        pytest.param(nats.errors.TimeoutError(), id="nats-subclass"),
    ],
)
async def test_an_idle_fetch_keeps_the_loop_running(
    idle: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whichever of the two classes arrives, the consumer goes round again.

    ``asyncio.TimeoutError`` is not a third case: it has been an alias of the
    built-in since 3.11, which is precisely why the narrower catch looked
    right and was not.
    """
    monkeypatch.setattr(consumer_module, "ensure_pull_consumer", _noop_ensure)
    stop = asyncio.Event()
    subscription = _Subscription(idle, stop)

    await asyncio.wait_for(
        run_pull_consumer(
            _JetStream(subscription),  # type: ignore[arg-type]
            tracer=NoOpTracer(),
            stream="EVENTS",
            durable="event-worker",
            handler=_noop_ensure,  # type: ignore[arg-type]
            stop=stop,
            fetch_timeout_seconds=0.01,
        ),
        timeout=5,
    )

    assert subscription.fetches == 2
