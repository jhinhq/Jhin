"""Typed input/result contracts for the heartbeat workflow."""

from dataclasses import dataclass


@dataclass
class HeartbeatInput:
    note: str = "heartbeat"
    sleep_seconds: float = 5.0


@dataclass
class HeartbeatResult:
    started_note: str
    finished_note: str
    beats: int
