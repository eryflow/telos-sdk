"""Session corpus —— the proxy records the **raw request** of every
``/v1/messages`` call to disk, so that ``telos replay`` can replay them for
comparison later.

Why only requests are recorded, not responses: Anthropic ``/v1/messages`` is
stateless; the request body of turn N already contains all assistant replies and
tool_results from the prior N-1 turns. So the "request sequence" itself is a
complete, replayable trace; assistant responses need not be stored separately
(which also avoids writing model output to disk).

What is recorded is the **raw** client→proxy request (before RTK filtering,
before TELOS rewriting), i.e. the "canonical input". On replay each mode
re-derives the wire from the same canonical input, so the comparison is fair.

The default directory is ``~/.telos/corpus/``, one ``<session>.jsonl`` per session.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CORPUS_DIR = Path.home() / ".telos" / "corpus"

_MAX_NAME_LEN = 120  # filesystem-friendly cap for a corpus filename stem


def _safe_name(session_id: str) -> str:
    """Turn a session_id into a safe, **collision-free** filename stem.

    Anything outside ``[A-Za-z0-9._-]`` becomes ``_``. A plain truncation is not
    enough: harness session ids can be long JSON blobs (``device_id`` +
    ``account_uuid`` + the real ``session_id``), and the stable device/account
    prefix alone already overruns the length cap — truncating there would map
    *every* conversation of one account onto a single file. So when the cleaned
    name is too long, keep a readable prefix and append a hash of the **full**
    id, which keeps distinct sessions in distinct files.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)
    if not cleaned:
        return "anon"
    if len(cleaned) <= _MAX_NAME_LEN:
        return cleaned
    digest = hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:16]
    return cleaned[: _MAX_NAME_LEN - len(digest) - 1] + "-" + digest


def record_call(
    corpus_dir: Path,
    session_id: str,
    call_index: int,
    request: dict[str, Any],
    *,
    ts: float | None = None,
) -> None:
    """Append the raw request of one call to ``<corpus_dir>/<session>.jsonl``.

    The caller is responsible for ensuring ``request`` is the raw body sent by the
    client (not rewritten by RTK / TELOS). This function leaves it to the caller
    to decide whether to raise —— it raises normally here, and the proxy side
    wraps it in a try.
    """
    corpus_dir.mkdir(parents=True, exist_ok=True)
    path = corpus_dir / f"{_safe_name(session_id)}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": ts if ts is not None else time.time(),
            "session_id": session_id,
            "call_index": call_index,
            "request": request,
        }, ensure_ascii=False) + "\n")


@dataclass
class CorpusSessionInfo:
    session_id: str
    path: Path
    n_calls: int
    first_ts: float
    last_ts: float
    handle: str = ""        #: the filename stem — a short, copy-pasteable id for ``--session``


def display_session(session_id: str) -> str:
    """A short, human-readable label for a session id.

    Harness session ids are often a JSON blob carrying ``device_id`` /
    ``account_uuid`` / the real ``session_id``; show just the inner
    ``session_id`` when present, otherwise the id as-is (clipped).
    """
    sid = (session_id or "").strip()
    if sid.startswith("{"):
        try:
            inner = json.loads(sid).get("session_id")
        except (json.JSONDecodeError, AttributeError):
            inner = None
        if inner:
            return str(inner)
    return sid if len(sid) <= 48 else sid[:45] + "..."


def _read_lines(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _first_record(path: Path) -> dict[str, Any] | None:
    """Read just the first JSON line of a corpus file (cheap id probe)."""
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    return json.loads(line)
    except (OSError, json.JSONDecodeError):
        return None
    return None


def list_sessions(corpus_dir: Path) -> list[CorpusSessionInfo]:
    """Scan the corpus directory, return a summary of each session (descending by last_ts)."""
    if not corpus_dir.exists():
        return []
    infos: list[CorpusSessionInfo] = []
    for path in sorted(corpus_dir.glob("*.jsonl")):
        recs = _read_lines(path)
        if not recs:
            continue
        sid = recs[0].get("session_id") or path.stem
        ts_vals = [float(r.get("ts") or 0.0) for r in recs]
        infos.append(CorpusSessionInfo(
            session_id=sid,
            path=path,
            n_calls=len(recs),
            first_ts=min(ts_vals) if ts_vals else 0.0,
            last_ts=max(ts_vals) if ts_vals else 0.0,
            handle=path.stem,
        ))
    infos.sort(key=lambda i: i.last_ts, reverse=True)
    return infos


def load_session(corpus_dir: Path, session_id: str) -> list[dict[str, Any]]:
    """Read all turns of a session in append order.

    JSONL order is the recording order. ``call_index`` is process-local and
    restarts from one when the gateway restarts, so sorting by it can corrupt a
    long-lived session's chronology.

    ``session_id`` resolves flexibly — any of these work:

    - the full stored id (the harness JSON blob);
    - the short ``handle`` / filename stem shown by ``telos replay --list``;
    - the inner per-conversation id shown in the ``session`` column
      (e.g. ``70939675-...``), see :func:`display_session`.

    Raises ``FileNotFoundError`` if nothing matches.
    """
    # 1. fast path: the file named directly after the (sanitized) id.
    direct = corpus_dir / f"{_safe_name(session_id)}.jsonl"
    if direct.exists():
        return _read_lines(direct)

    # 2. scan: match by filename stem, full stored id, or inner display id.
    if corpus_dir.exists():
        for path in sorted(corpus_dir.glob("*.jsonl")):
            first = _first_record(path)
            stored = (first or {}).get("session_id") or ""
            if (path.stem == session_id
                    or stored == session_id
                    or display_session(stored) == session_id):
                return _read_lines(path)

    raise FileNotFoundError(
        f"session {session_id!r} not found in corpus (directory {corpus_dir})")
