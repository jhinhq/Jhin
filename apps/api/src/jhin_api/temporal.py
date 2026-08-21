"""One interceptor-aware, concurrency-safe Temporal client provider."""

from __future__ import annotations

import asyncio

from temporalio.client import Client

from jhin_api.settings import Settings
from jhin_observability import ObservabilityRuntime, temporal_client_interceptors


class TemporalClientProvider:
    def __init__(
        self,
        settings: Settings,
        observability: ObservabilityRuntime,
    ) -> None:
        self._settings = settings
        self._observability = observability
        self._lock = asyncio.Lock()
        self._client: Client | None = None

    async def get(self) -> Client:
        cached = self._client
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._client
            if cached is None:
                cached = await Client.connect(
                    self._settings.temporal_address,
                    namespace=self._settings.temporal_namespace,
                    interceptors=temporal_client_interceptors(self._observability),
                )
                self._client = cached
            return cached


__all__ = ["TemporalClientProvider"]
