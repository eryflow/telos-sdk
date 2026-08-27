"""Small Trace/Span lifecycle primitives for harness adapters and the gateway."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
import time
from typing import Any, Protocol
from uuid import uuid4


STATUSES = frozenset({"running", "ok", "error", "cancelled", "abandoned", "unknown"})
SPAN_TYPES = frozenset({"general", "agent", "llm", "tool", "approval", "compaction"})


def now_us() -> int:
    return time.time_ns() // 1_000


class TraceProcessor(Protocol):
    def on_trace_start(self, trace: "Trace") -> None: ...

    def on_trace_end(self, trace: "Trace") -> None: ...

    def on_span_start(self, span: "Span") -> None: ...

    def on_span_end(self, span: "Span") -> None: ...

    def force_flush(self) -> None: ...

    def shutdown(self) -> None: ...


_current_trace: ContextVar[Trace | None] = ContextVar("telos_current_trace", default=None)
_current_span: ContextVar[Span | None] = ContextVar("telos_current_span", default=None)


def get_current_trace() -> "Trace | None":
    return _current_trace.get()


def set_current_trace(trace: "Trace") -> Token["Trace | None"]:
    return _current_trace.set(trace)


def reset_current_trace(token: Token["Trace | None"] | None = None) -> None:
    if token is None:
        _current_trace.set(None)
    else:
        _current_trace.reset(token)


def get_current_span() -> "Span | None":
    return _current_span.get()


def set_current_span(span: "Span") -> Token["Span | None"]:
    return _current_span.set(span)


def reset_current_span(token: Token["Span | None"] | None = None) -> None:
    if token is None:
        _current_span.set(None)
    else:
        _current_span.reset(token)


@dataclass(kw_only=True)
class Trace:
    processor: TraceProcessor
    thread_id: str
    harness: str
    name: str
    source: str = "sdk"
    project_id: str = "default"
    id: str = field(default_factory=lambda: str(uuid4()))
    external_id: str | None = None
    status: str = "running"
    start_time_us: int = field(default_factory=now_us)
    end_time_us: int | None = None
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    error: Any = None
    source_updated_at_us: int = field(default_factory=now_us)
    _context_token: Token[Trace | None] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.external_id = self.external_id or self.id
        if self.status not in STATUSES:
            raise ValueError(f"unsupported trace status: {self.status}")

    def start(self, mark_as_current: bool = False) -> "Trace":
        self.source_updated_at_us = now_us()
        self.processor.on_trace_start(self)
        if mark_as_current:
            self._context_token = set_current_trace(self)
        return self

    def finish(self, status: str = "ok", reset_current: bool = False) -> "Trace":
        if status not in STATUSES or status == "running":
            raise ValueError(f"unsupported finish status: {status}")
        self.status = status
        self.end_time_us = self.end_time_us or now_us()
        self.source_updated_at_us = now_us()
        self.processor.on_trace_end(self)
        if reset_current:
            reset_current_trace(self._context_token)
            self._context_token = None
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "thread_id": self.thread_id,
            "harness": self.harness,
            "source": self.source,
            "external_id": self.external_id,
            "name": self.name,
            "status": self.status,
            "start_time_us": self.start_time_us,
            "end_time_us": self.end_time_us,
            "input": self.input,
            "output": self.output,
            "metadata": self.metadata,
            "tags": self.tags,
            "error": self.error,
            "source_updated_at_us": self.source_updated_at_us,
        }


@dataclass(kw_only=True)
class Span:
    processor: TraceProcessor
    name: str
    source: str = "sdk"
    trace_id: str | None = None
    parent_span_id: str | None = None
    id: str = field(default_factory=lambda: str(uuid4()))
    external_id: str | None = None
    type: str = "general"
    status: str = "running"
    start_time_us: int = field(default_factory=now_us)
    end_time_us: int | None = None
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    model: str | None = None
    provider: str | None = None
    cost_usd_micros: int | None = None
    ttft_us: int | None = None
    error: Any = None
    source_updated_at_us: int = field(default_factory=now_us)
    _context_token: Token[Span | None] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        current_trace = get_current_trace()
        current_span = get_current_span()
        if self.trace_id is None and current_trace is not None:
            self.trace_id = current_trace.id
        if self.trace_id is None:
            raise ValueError("trace_id is required when no trace is current")
        if self.parent_span_id is None and current_span is not None:
            if current_span.trace_id != self.trace_id:
                raise ValueError("current parent span belongs to another trace")
            self.parent_span_id = current_span.id
        self.external_id = self.external_id or self.id
        if self.status not in STATUSES:
            raise ValueError(f"unsupported span status: {self.status}")
        if self.type not in SPAN_TYPES:
            raise ValueError(f"unsupported span type: {self.type}")

    def start(self, mark_as_current: bool = False) -> "Span":
        self.source_updated_at_us = now_us()
        self.processor.on_span_start(self)
        if mark_as_current:
            self._context_token = set_current_span(self)
        return self

    def finish(self, status: str = "ok", reset_current: bool = False) -> "Span":
        if status not in STATUSES or status == "running":
            raise ValueError(f"unsupported finish status: {status}")
        self.status = status
        self.end_time_us = self.end_time_us or now_us()
        self.source_updated_at_us = now_us()
        self.processor.on_span_end(self)
        if reset_current:
            reset_current_span(self._context_token)
            self._context_token = None
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "source": self.source,
            "external_id": self.external_id,
            "name": self.name,
            "type": self.type,
            "status": self.status,
            "start_time_us": self.start_time_us,
            "end_time_us": self.end_time_us,
            "input": self.input,
            "output": self.output,
            "metadata": self.metadata,
            "tags": self.tags,
            "usage": self.usage,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "model": self.model,
            "provider": self.provider,
            "cost_usd_micros": self.cost_usd_micros,
            "ttft_us": self.ttft_us,
            "error": self.error,
            "source_updated_at_us": self.source_updated_at_us,
        }
