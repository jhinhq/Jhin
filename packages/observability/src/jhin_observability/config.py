"""Closed, validated observability configuration."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from structlog.types import Processor

MAX_SPAN_QUEUE_SIZE = 65_536
MAX_SPAN_EXPORT_BATCH_SIZE = 8_192
MAX_EXPORT_TIMEOUT_MILLIS = 30_000
MAX_METRIC_EXPORT_INTERVAL_MILLIS = 300_000

_HTTP_AUTHORITIES = frozenset(
    {
        "otel-collector:4317",
        "localhost:4317",
        "127.0.0.1:4317",
        "[::1]:4317",
    }
)
_CANONICAL_INTEGER_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_CANONICAL_RATIO_RE = re.compile(r"(?:0|1)(?:\.[0-9]+)?\Z")
_DNS_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


class ObservabilityConfigurationError(RuntimeError):
    """The process received invalid or conflicting observability configuration."""


class ObservabilityNotInitializedError(RuntimeError):
    """A long-lived process attempted instrumentation before bootstrap."""


@dataclass(frozen=True)
class ObservabilityConfig:
    service_name: str
    service_version: str
    environment: str
    otlp_endpoint: str | None = None
    otlp_insecure: bool = False
    otlp_ca_file: Path | None = None
    otlp_client_certificate_file: Path | None = None
    otlp_client_key_file: Path | None = None
    trace_sampler: Literal["always_on", "always_off", "parentbased_traceidratio"] = (
        "parentbased_traceidratio"
    )
    trace_sample_ratio: float = 0.10
    span_queue_size: int = 2_048
    span_export_batch_size: int = 512
    export_timeout_millis: int = 5_000
    metric_export_interval_millis: int = 60_000
    extra_log_processors: tuple[Processor, ...] = ()

    def __post_init__(self) -> None:
        if not self.service_name or not self.service_version or not self.environment:
            raise ValueError("service name, version, and environment are required")
        self._validate_transport()
        self._validate_sampling()
        self._validate_numeric_limits()

    def _validate_transport(self) -> None:
        certificate_pair = (
            self.otlp_client_certificate_file is not None,
            self.otlp_client_key_file is not None,
        )
        if certificate_pair[0] != certificate_pair[1]:
            raise ValueError("OTLP client certificate and key must be configured together")

        tls_paths_present = any(
            candidate is not None
            for candidate in (
                self.otlp_ca_file,
                self.otlp_client_certificate_file,
                self.otlp_client_key_file,
            )
        )
        if self.otlp_endpoint is None:
            if self.otlp_insecure or tls_paths_present:
                raise ValueError("OTLP configuration requires an OTLP endpoint")
            return

        parsed = urlsplit(self.otlp_endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("OTLP endpoint must be an absolute HTTP(S) URL")
        if not _is_valid_host(parsed.hostname):
            raise ValueError("OTLP endpoint must contain a valid host and port")
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("OTLP endpoint must contain a valid host and port") from None
        if port == 0:
            raise ValueError("OTLP endpoint must contain a valid host and port")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("OTLP endpoint must not contain credentials, query, or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("OTLP gRPC endpoint must use the root path")

        if parsed.scheme == "http":
            if not self.otlp_insecure or parsed.netloc not in _HTTP_AUTHORITIES:
                raise ValueError("cleartext OTLP is allowed only for local collectors on port 4317")
            if tls_paths_present:
                raise ValueError("HTTP OTLP must not configure TLS credential files")
            return

        if self.otlp_insecure:
            raise ValueError("HTTPS OTLP cannot be configured as insecure")

    def _validate_sampling(self) -> None:
        if self.trace_sampler not in {
            "always_on",
            "always_off",
            "parentbased_traceidratio",
        }:
            raise ValueError("trace sampler is not registered")
        ratio = self.trace_sample_ratio
        if (
            isinstance(ratio, bool)
            or not isinstance(ratio, (int, float))
            or not math.isfinite(ratio)
            or not 0 <= ratio <= 1
        ):
            raise ValueError("trace sample ratio must be a finite number between zero and one")

    def _validate_numeric_limits(self) -> None:
        limits = (
            ("span_queue_size", self.span_queue_size, MAX_SPAN_QUEUE_SIZE),
            (
                "span_export_batch_size",
                self.span_export_batch_size,
                MAX_SPAN_EXPORT_BATCH_SIZE,
            ),
            ("export_timeout_millis", self.export_timeout_millis, MAX_EXPORT_TIMEOUT_MILLIS),
            (
                "metric_export_interval_millis",
                self.metric_export_interval_millis,
                MAX_METRIC_EXPORT_INTERVAL_MILLIS,
            ),
        )
        for name, value, maximum in limits:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
            if value > maximum:
                raise ValueError(f"{name} exceeds its reviewed maximum")
        if self.span_export_batch_size > self.span_queue_size:
            raise ValueError("span export batch size cannot exceed queue size")


def _is_valid_host(host: str) -> bool:
    try:
        ip_address(host)
    except ValueError:
        candidate = host[:-1] if host.endswith(".") else host
        return (
            bool(candidate)
            and len(candidate) <= 253
            and all(_DNS_LABEL_RE.fullmatch(label) for label in candidate.split("."))
        )
    return True


class ObservabilitySettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", env_prefix="")

    app_env: Literal["dev", "test", "staging", "production"] = "dev"
    log_level: str = "INFO"
    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_otlp_insecure: bool = False
    otel_exporter_otlp_certificate: Path | None = None
    otel_exporter_otlp_client_certificate: Path | None = None
    otel_exporter_otlp_client_key: Path | None = None
    otel_traces_sampler: Literal["always_on", "always_off", "parentbased_traceidratio"] = (
        "parentbased_traceidratio"
    )
    otel_traces_sampler_arg: float = 0.10
    otel_bsp_max_queue_size: int = 2_048
    otel_bsp_max_export_batch_size: int = 512
    otel_exporter_otlp_timeout_millis: int = 5_000
    otel_metric_export_interval_millis: int = 60_000

    @field_validator(
        "otel_bsp_max_queue_size",
        "otel_bsp_max_export_batch_size",
        "otel_exporter_otlp_timeout_millis",
        "otel_metric_export_interval_millis",
        mode="before",
    )
    @classmethod
    def _parse_canonical_integer(cls, value: object, info: ValidationInfo) -> int:
        if isinstance(value, bool):
            raise ValueError("numeric observability settings do not accept booleans")
        if isinstance(value, str):
            if not _CANONICAL_INTEGER_RE.fullmatch(value):
                raise ValueError(
                    "numeric observability setting must be a canonical decimal integer"
                )
            parsed = int(value)
        elif isinstance(value, int):
            parsed = value
        else:
            raise ValueError("numeric observability setting must be an integer")
        maximum_by_field = {
            "otel_bsp_max_queue_size": MAX_SPAN_QUEUE_SIZE,
            "otel_bsp_max_export_batch_size": MAX_SPAN_EXPORT_BATCH_SIZE,
            "otel_exporter_otlp_timeout_millis": MAX_EXPORT_TIMEOUT_MILLIS,
            "otel_metric_export_interval_millis": MAX_METRIC_EXPORT_INTERVAL_MILLIS,
        }
        field_name = info.field_name
        if field_name is None:
            raise ValueError("numeric observability setting has no field authority")
        if parsed <= 0 or parsed > maximum_by_field[field_name]:
            raise ValueError("numeric observability setting is outside its reviewed range")
        return parsed

    @field_validator("otel_traces_sampler_arg", mode="before")
    @classmethod
    def _parse_canonical_ratio(cls, value: object) -> float:
        if isinstance(value, bool):
            raise ValueError("trace sample ratio does not accept booleans")
        if isinstance(value, str):
            if not _CANONICAL_RATIO_RE.fullmatch(value):
                raise ValueError("trace sample ratio must be a canonical decimal")
            parsed = float(value)
        elif isinstance(value, (int, float)):
            parsed = float(value)
        else:
            raise ValueError("trace sample ratio must be numeric")
        if not math.isfinite(parsed) or not 0 <= parsed <= 1:
            raise ValueError("trace sample ratio must be between zero and one")
        return parsed

    def observability_config(
        self,
        *,
        service_name: str,
        service_version: str,
        extra_log_processors: tuple[Processor, ...] = (),
    ) -> ObservabilityConfig:
        endpoint = (self.otel_exporter_otlp_endpoint or "").strip() or None
        return ObservabilityConfig(
            service_name=service_name,
            service_version=service_version,
            environment=self.app_env,
            otlp_endpoint=endpoint,
            otlp_insecure=self.otel_exporter_otlp_insecure,
            otlp_ca_file=self.otel_exporter_otlp_certificate,
            otlp_client_certificate_file=self.otel_exporter_otlp_client_certificate,
            otlp_client_key_file=self.otel_exporter_otlp_client_key,
            trace_sampler=self.otel_traces_sampler,
            trace_sample_ratio=self.otel_traces_sampler_arg,
            span_queue_size=self.otel_bsp_max_queue_size,
            span_export_batch_size=self.otel_bsp_max_export_batch_size,
            export_timeout_millis=self.otel_exporter_otlp_timeout_millis,
            metric_export_interval_millis=self.otel_metric_export_interval_millis,
            extra_log_processors=extra_log_processors,
        )


def service_version(distribution_name: str) -> str:
    try:
        return version(distribution_name)
    except PackageNotFoundError:
        raise ObservabilityConfigurationError(
            "service distribution metadata is unavailable"
        ) from None


__all__ = [
    "MAX_EXPORT_TIMEOUT_MILLIS",
    "MAX_METRIC_EXPORT_INTERVAL_MILLIS",
    "MAX_SPAN_EXPORT_BATCH_SIZE",
    "MAX_SPAN_QUEUE_SIZE",
    "ObservabilityConfig",
    "ObservabilityConfigurationError",
    "ObservabilityNotInitializedError",
    "ObservabilitySettings",
    "service_version",
]
