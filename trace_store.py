"""Append-only local event log for Harness Reporter events.

The existing :mod:`telos.corpus` remains the replay source for raw model
requests. This module stores only the Harness-side facts the gateway cannot
observe itself, keyed by the same ``session_id``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from telos.config import telos_home
from telos.corpus import _safe_name

REPORTER_EVENT_KINDS = frozenset({
    "attempt.started",
    "approval.decided",
    "tool.finished",
    "workspace.changed",
    "artifact.created",
    "user.feedback",
    "attempt.finished",
})


def default_trace_dir() -> Path:
    return telos_home() / "traces"


def load_events(path: Path) -> list[dict[str, Any]]:
    """Read every valid event, tolerating blank or crash-torn JSONL lines."""
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


class TraceStore:
    """One-process writer assigning monotonic per-session sequence numbers."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_trace_dir()
        self._lock = threading.Lock()
        self._next_seq: dict[tuple[str, str], int] = {}
        self._event_ids: dict[tuple[str, str], dict[str, int]] = {}

    def path_for(self, harness: str, session_id: str) -> Path:
        return self.root / _safe_name(harness) / f"{_safe_name(session_id)}.jsonl"

    def append(
        self,
        *,
        harness: str,
        session_id: str,
        event_id: str,
        kind: str,
        data: dict[str, Any],
        observed_at: str | None = None,
    ) -> tuple[int, bool]:
        """Append one event and return ``(seq, created)``.

        ``event_id`` is idempotent within one Harness Session. A retry returns
        the original sequence number without adding another line.
        """
        key = (harness, session_id)
        path = self.path_for(harness, session_id)
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            try:
                self.root.chmod(0o700)
            except OSError:
                pass
            self._ensure_loaded(key, path)
            existing = self._event_ids[key].get(event_id)
            if existing is not None:
                return existing, False

            seq = self._next_seq[key]
            event = {
                "seq": seq,
                "recorded_at": time.time(),
                "observed_at": observed_at,
                "session_id": session_id,
                "harness": harness,
                "source": "reporter",
                "kind": kind,
                "event_id": event_id,
                "data": data,
            }
            self._append_line(path, event)
            self._next_seq[key] = seq + 1
            self._event_ids[key][event_id] = seq
            return seq, True

    def _ensure_loaded(self, key: tuple[str, str], path: Path) -> None:
        if key in self._next_seq:
            return
        events = load_events(path)
        max_seq = 0
        ids: dict[str, int] = {}
        for event in events:
            seq = int(event.get("seq") or 0)
            max_seq = max(max_seq, seq)
            event_id = event.get("event_id")
            if isinstance(event_id, str) and event_id:
                ids[event_id] = seq
        self._next_seq[key] = max_seq + 1
        self._event_ids[key] = ids

    @staticmethod
    def _append_line(path: Path, event: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        record = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        if path.exists() and path.stat().st_size:
            with path.open("rb") as current:
                current.seek(-1, os.SEEK_END)
                if current.read(1) != b"\n":
                    record = b"\n" + record
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            written = os.write(fd, record)
            if written != len(record):
                raise OSError(f"short Trace write: {written}/{len(record)} bytes")
        finally:
            os.close(fd)
