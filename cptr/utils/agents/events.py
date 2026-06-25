"""Normalized event types emitted by coding agent adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AgentTextDelta:
    text: str


@dataclass
class AgentReasoningDelta:
    text: str


@dataclass
class AgentDone:
    usage: dict[str, Any] | None = None
    resume_state: dict[str, Any] | None = None


@dataclass
class AgentError:
    message: str


AgentEvent = AgentTextDelta | AgentReasoningDelta | AgentDone | AgentError
