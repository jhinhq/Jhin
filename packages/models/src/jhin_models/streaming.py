"""Lossless bounded stream assembly. Tool dispatch requires a completed response."""

import json
from typing import Any

from jhin_models.base import (
    ModelProviderError,
    ModelResponse,
    ModelStreamEvent,
    ModelToolCall,
    ModelUsage,
    tool_name_from_wire,
)

MAX_STREAM_CHARS = 2_000_000


class StreamAccumulator:
    def __init__(self, model: str, known_tools: list[str]):
        self.model = model
        self.known_tools = known_tools
        self.request_id: str | None = None
        self.text: list[str] = []
        self.tools: dict[int, dict[str, str]] = {}
        self.usage: dict[str, int] = {}
        self.finish_reason = ""
        self.size = 0
        self.finished = False
        self.citations: list[dict[str, Any]] = []

    def bounded(self, value: str) -> str:
        self.size += len(value)
        if self.size > MAX_STREAM_CHARS:
            raise ModelProviderError("Provider stream exceeded the output limit", retryable=False)
        return value

    def openai(self, chunk: dict[str, Any]) -> list[ModelStreamEvent]:
        if chunk.get("error"):
            raise ModelProviderError("Provider returned a stream error", retryable=True)
        self.model = chunk.get("model") or self.model
        self.request_id = chunk.get("id") or self.request_id
        result = []
        if usage := chunk.get("usage"):
            self.usage.update(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
                cached_tokens=int(
                    (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                ),
            )
            result.append(ModelStreamEvent(type="usage", data=self.usage))
        for choice in chunk.get("choices") or []:
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str) and delta["content"]:
                text = self.bounded(delta["content"])
                self.text.append(text)
                result.append(ModelStreamEvent(type="text_delta", text=text))
            for raw in delta.get("tool_calls") or []:
                index = int(raw.get("index") or 0)
                if not 0 <= index < 256:
                    raise ModelProviderError(
                        "Provider returned too many tool calls", retryable=False
                    )
                tool = self.tools.setdefault(index, {"id": "", "name": "", "arguments": ""})
                function = raw.get("function") or {}
                if raw.get("id"):
                    value = str(raw["id"])
                    if tool["id"] and tool["id"] != value:
                        raise ModelProviderError(
                            "Provider changed a streamed tool identity", retryable=False
                        )
                    tool["id"] = self.bounded(value)
                for key in ("name", "arguments"):
                    if isinstance(function.get(key), str) and not (
                        key == "name" and tool[key] == function[key]
                    ):
                        tool[key] += self.bounded(function[key])
                result.append(
                    ModelStreamEvent(
                        type="tool_delta",
                        index=index,
                        tool_call_id=tool["id"],
                        tool_name=tool["name"],
                        arguments_delta=function.get("arguments") or "",
                    )
                )
            for citation in delta.get("annotations") or []:
                if isinstance(citation, dict) and citation.get("type") == "url_citation":
                    self.bounded(json.dumps(citation))
                    self.citations.append(citation)
                    result.append(ModelStreamEvent(type="citation", data=citation))
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
                self.finished = True
        return result

    def anthropic(self, chunk: dict[str, Any]) -> list[ModelStreamEvent]:
        kind = chunk.get("type")
        if kind == "error":
            raise ModelProviderError("Provider returned a stream error", retryable=True)
        if kind == "message_start":
            message = chunk.get("message") or {}
            self.model = message.get("model") or self.model
            self.request_id = message.get("id")
            usage = message.get("usage") or {}
            self.usage.update(
                input_tokens=int(usage.get("input_tokens") or 0),
                cached_tokens=int(usage.get("cache_read_input_tokens") or 0),
            )
        index = int(chunk.get("index") or 0)
        if not 0 <= index < 256:
            raise ModelProviderError("Provider returned too many content blocks", retryable=False)
        if kind == "content_block_start":
            block = chunk.get("content_block") or {}
            if block.get("type") == "tool_use":
                self.tools[index] = {
                    "id": str(block.get("id") or ""),
                    "name": str(block.get("name") or ""),
                    "arguments": "",
                }
                if block.get("input"):
                    self.tools[index]["arguments"] = self.bounded(json.dumps(block["input"]))
            if block.get("type") == "text" and block.get("text"):
                text = self.bounded(block["text"])
                self.text.append(text)
                return [ModelStreamEvent(type="text_delta", text=text)]
        if kind == "content_block_delta":
            delta = chunk.get("delta") or {}
            if delta.get("type") == "text_delta":
                text = self.bounded(str(delta.get("text") or ""))
                self.text.append(text)
                return [ModelStreamEvent(type="text_delta", text=text)]
            if delta.get("type") == "input_json_delta" and index in self.tools:
                text = self.bounded(str(delta.get("partial_json") or ""))
                tool = self.tools[index]
                tool["arguments"] += text
                return [
                    ModelStreamEvent(
                        type="tool_delta",
                        index=index,
                        tool_call_id=tool["id"],
                        tool_name=tool["name"],
                        arguments_delta=text,
                    )
                ]
            if delta.get("type") == "citations_delta":
                self.bounded(json.dumps(delta.get("citation") or {}))
                self.citations.append(delta.get("citation") or {})
                return [ModelStreamEvent(type="citation", data=delta.get("citation") or {})]
        if kind == "message_delta":
            self.finish_reason = (chunk.get("delta") or {}).get("stop_reason") or self.finish_reason
            self.usage["output_tokens"] = int((chunk.get("usage") or {}).get("output_tokens") or 0)
        if kind == "message_stop":
            self.finished = True
        return []

    def response(self, latency_ms: int) -> ModelResponse:
        if not self.finished:
            raise ModelProviderError(
                "Provider stream ended before completion; no tools were dispatched", retryable=True
            )
        tools = []
        for _, value in sorted(self.tools.items()):
            if not value["id"] or not value["name"]:
                raise ModelProviderError(
                    "Provider returned an incomplete tool call", retryable=False
                )
            tools.append(
                ModelToolCall(
                    id=value["id"],
                    name=tool_name_from_wire(value["name"], self.known_tools),
                    arguments_json=value["arguments"] or "{}",
                )
            )
        from jhin_models.web_search import WebCitation, render_citations

        citations = []
        for value in self.citations:
            citation = value.get("url_citation", value)
            if isinstance(citation, dict) and str(citation.get("url", "")).startswith(
                ("http://", "https://")
            ):
                citations.append(
                    WebCitation(url=citation["url"], title=str(citation.get("title") or ""))
                )
        return ModelResponse(
            text="".join(self.text) + render_citations(citations),
            model=self.model,
            finish_reason=self.finish_reason,
            usage=ModelUsage(**self.usage),
            tool_calls=tuple(tools),
            provider_request_id=self.request_id,
            latency_ms=latency_ms,
        )
