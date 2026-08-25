"""First-party OpenAI adapter.

Identical wire format to the compatible base, but pinned to the official
endpoint and using ``max_completion_tokens`` (OpenAI deprecated ``max_tokens``
on chat completions).

Spend reporting: OpenAI has no balance API. Month-to-date cost comes from the
organization **Admin API** (``GET /organization/costs``), which needs a
separate admin key; without one :meth:`get_account_status` raises
:class:`AccountStatusUnsupported` so the UI can offer to add it.

The same endpoint, asked to ``group_by=line_item``, itemises that spend per
model and per input/output side — which is what
:meth:`OpenAIClient.fetch_model_costs` feeds to
:mod:`jhin_models.observed_pricing` to measure the workspace's real
per-token rates. OpenAI exposes no pricing endpoint, so measuring the
invoice is the only way to learn a contract price.
"""

from __future__ import annotations

import calendar
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

import httpx

from jhin_models.base import (
    AccountStatus,
    AccountStatusUnsupported,
    ModelProviderError,
    classify_retryable,
    describe_error_body,
)
from jhin_models.observed_pricing import (
    CostLine,
    ModelCostReport,
    billed_tokens,
    parse_cost_line_item,
)
from jhin_models.pricing import usd_to_micros
from jhin_models.providers.openai_compatible import OpenAICompatibleClient
from jhin_models.web_search import WebSearchConfig

OPENAI_BASE_URL = "https://api.openai.com/v1"
_COSTS_PATH = "/organization/costs"
_COSTS_PAGE_LIMIT = 31
_MAX_COST_PAGES = 12
# Enough for the admin to recognise what went unattributed without turning
# the response into a dump of every service line the organization bills.
_MAX_IGNORED_LABELS = 10
_ADMIN_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)


def month_start(today: date | None = None) -> date:
    current = today or datetime.now(UTC).date()
    return current.replace(day=1)


def _epoch(day: date) -> int:
    return calendar.timegm(datetime(day.year, day.month, day.day, tzinfo=UTC).timetuple())


def sum_cost_buckets(payload: dict[str, Any]) -> int:
    """Micro-dollars across every ``results[].amount.value`` in one page."""
    total = 0
    for bucket in payload.get("data") or []:
        if not isinstance(bucket, dict):
            continue
        for result in bucket.get("results") or []:
            if not isinstance(result, dict):
                continue
            amount = result.get("amount")
            if not isinstance(amount, dict):
                continue
            micros = usd_to_micros(amount.get("value"))
            if micros is not None:
                total += micros
    return total


class OpenAIClient(OpenAICompatibleClient):
    provider_name = "openai"
    max_tokens_field = "max_completion_tokens"
    pricing_catalog = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = OPENAI_BASE_URL,
        admin_api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(base_url=base_url, api_key=api_key, transport=transport)
        self._admin: httpx.AsyncClient | None = None
        if admin_api_key:
            self._admin = httpx.AsyncClient(
                base_url=base_url.rstrip("/"),
                headers=self._headers(admin_api_key),
                timeout=_ADMIN_TIMEOUT,
                transport=transport,
            )

    @property
    def has_admin_key(self) -> bool:
        return self._admin is not None

    def _apply_web_search(self, payload: dict[str, Any], config: WebSearchConfig) -> None:
        """Chat-completions built-in search (search-preview models only).

        ``web_search_options`` takes no use cap; ``max_uses`` is ignored here.
        Citations come back as ``url_citation`` annotations on the message.
        """
        payload["web_search_options"] = {}

    async def get_account_status(self) -> AccountStatus | None:
        """Month-to-date organization cost via the Admin API (paginated)."""
        start = month_start()
        params: dict[str, Any] = {
            "start_time": _epoch(start),
            "bucket_width": "1d",
            "limit": _COSTS_PAGE_LIMIT,
        }
        spent = 0
        async for body in self._admin_pages(params):
            spent += sum_cost_buckets(body)
        return AccountStatus(
            spent_month_micros=spent,
            period_start=start,
            source="openai_admin",
            detail="From OpenAI's admin API (month to date)",
        )

    async def _admin_pages(self, params: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Every page of an Admin API listing, bounded by ``_MAX_COST_PAGES``."""
        if self._admin is None:
            raise AccountStatusUnsupported(
                "OpenAI has no balance API; add an admin key to report month-to-date spend"
            )
        for _ in range(_MAX_COST_PAGES):
            try:
                response = await self._admin.get(_COSTS_PATH, params=params)
            except httpx.HTTPError as exc:
                raise ModelProviderError(
                    f"openai: network error: {type(exc).__name__}", retryable=True
                ) from exc
            if response.status_code >= 400:
                raise ModelProviderError(
                    f"openai: admin API HTTP {response.status_code}: "
                    f"{describe_error_body(response.text)}",
                    status_code=response.status_code,
                    retryable=classify_retryable(response.status_code),
                )
            body = response.json()
            if not isinstance(body, dict):
                raise ModelProviderError("openai: admin API returned an unexpected body")
            yield body
            next_page = body.get("next_page")
            if not body.get("has_more") or not isinstance(next_page, str) or not next_page:
                return
            params = {**params, "page": next_page}

    async def fetch_model_costs(self, *, start: date, end: date) -> ModelCostReport:
        """Itemised spend per model over ``[start, end)`` (whole UTC days).

        ``group_by=line_item`` turns each bucket's results into invoice lines
        such as ``"gpt-4o-2024-08-06, input"``, often with ``quantity``
        carrying the billed token count. That label is a human-facing string,
        not a schema — surface-prefixed lines (``"evals | ..."``) and
        non-model services (``"assistants api | file search"``) are counted
        into the report's ignored bucket rather than guessed at, so the
        reconciliation can say out loud how much of the bill it explained.

        Callers must pass a period of *completed* days: cost buckets for the
        current day are still filling, and dividing a partial bill by a full
        day of tokens understates every rate.
        """
        params: dict[str, Any] = {
            "start_time": _epoch(start),
            "end_time": _epoch(end),
            "bucket_width": "1d",
            "group_by": ["line_item"],
            "limit": _COSTS_PAGE_LIMIT,
        }
        lines: list[CostLine] = []
        total = 0
        ignored = 0
        ignored_labels: set[str] = set()
        async for body in self._admin_pages(params):
            for bucket in body.get("data") or []:
                if not isinstance(bucket, dict):
                    continue
                for result in bucket.get("results") or []:
                    if not isinstance(result, dict):
                        continue
                    amount = result.get("amount")
                    if not isinstance(amount, dict):
                        continue
                    micros = usd_to_micros(amount.get("value"))
                    if micros is None:
                        continue
                    total += micros
                    label = result.get("line_item")
                    parsed = parse_cost_line_item(label)
                    if parsed is None:
                        ignored += micros
                        ignored_labels.add(str(label) if label else "(ungrouped)")
                        continue
                    model_key, side = parsed
                    lines.append(
                        CostLine(
                            model_key=model_key,
                            side=side,
                            cost_micros=micros,
                            billed_tokens=billed_tokens(
                                result.get("quantity"), result.get("quantity_unit")
                            ),
                        )
                    )
        return ModelCostReport(
            lines=lines,
            total_micros=total,
            ignored_micros=ignored,
            ignored_labels=sorted(ignored_labels)[:_MAX_IGNORED_LABELS],
        )

    async def close(self) -> None:
        await super().close()
        if self._admin is not None:
            await self._admin.aclose()
