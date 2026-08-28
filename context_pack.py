"""Immutable, portable semantic checkpoints for cross-harness handoff."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any, Mapping, Sequence
from uuid import uuid4
import zipfile


SCHEMA_VERSION = 1
LAYERS = ("objective", "policy", "progress", "memory", "conversation", "workspace", "provenance")
_REQUIRED_LAYERS = frozenset({"objective", "policy", "progress", "memory", "provenance"})
_CAPTURE_STATUSES = frozenset({"complete", "partial", "dirty", "invalid"})
_CAPTURE_METHODS = frozenset({"native", "cooperative", "reconstructed", "assisted"})
_MAX_ENTRIES = 1024
_MAX_FILE_BYTES = 50 * 1024 * 1024
_MAX_TOTAL_BYTES = 200 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 100
_DENIED_NAMES = frozenset({
    ".env", ".npmrc", ".pypirc", "credentials", "credentials.json", "cookies.txt",
    "id_rsa", "id_ed25519", "known_hosts", "netrc", ".netrc",
})
_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(rb"(?i)\bauthorization\s*[:=]\s*bearer\s+[A-Za-z0-9._~+/-]{12,}"),
    re.compile(rb"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9._~+/-]{12,}"),
)


def telos_home(path: str | Path | None = None) -> Path:
    return (Path(path).expanduser() if path else Path.home() / ".telos").resolve()


def owned_directory(home: str | Path | None, name: str) -> Path:
    root = telos_home(home)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / name
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or root not in path.resolve().parents:
        raise ValueError(f"TELOS {name} directory escapes {root}")
    os.chmod(path, 0o700)
    return path


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _safe_relative(path: str) -> PurePosixPath:
    candidate = PurePosixPath(path)
    if (
        not path or path.startswith(("/", "\\")) or "\\" in path
        or any(part in ("", ".", "..") for part in candidate.parts)
        or (candidate.parts and ":" in candidate.parts[0])
        or len(path) > 512
    ):
        raise ValueError(f"unsafe bundle path: {path!r}")
    if candidate.name.lower() in _DENIED_NAMES:
        raise ValueError(f"credential file is not allowed in a Context Pack: {path}")
    return candidate


def _scan_secret(path: str, data: bytes) -> None:
    _safe_relative(path)
    for pattern in _SECRET_PATTERNS:
        if pattern.search(data):
            raise ValueError(f"possible secret found in {path}; exclude or redact that entry")


def _git(workspace: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(workspace), *args], capture_output=True, text=True,
        check=False, timeout=30,
    )
    if check and result.returncode:
        raise ValueError(result.stderr.strip() or "git workspace inspection failed")
    return result.stdout


def snapshot_workspace(
    workspace: str | Path, *, exclude_paths: Sequence[str] = (),
) -> tuple[dict[str, Any], bytes | None, bool]:
    path = Path(workspace).expanduser().resolve()
    root = Path(_git(path, "rev-parse", "--show-toplevel").strip()).resolve()
    head = _git(root, "rev-parse", "HEAD").strip()
    remote = _git(root, "config", "--get", "remote.origin.url", check=False).strip() or None
    if remote and ("@" in remote and "://" in remote):
        # Strip URL userinfo; SCP-style git@host:path is an identity, not a credential.
        remote = re.sub(r"(?<=://)[^/@]+@", "", remote)
    before = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    clean_excludes = []
    for item in exclude_paths:
        relative = _safe_relative(item).as_posix()
        clean_excludes.append(relative)
    diff_args = ["diff", "--binary", "--no-ext-diff", "HEAD"]
    if clean_excludes:
        diff_args.extend(("--", ".", *(f":(exclude){item}" for item in clean_excludes)))
    patch = _git(root, *diff_args).encode()
    after = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    changed_during_capture = before != after
    untracked = sorted(
        line[3:] for line in after.splitlines() if line.startswith("?? ")
    )
    state = {
        "root": str(root),
        "remote": remote,
        "head": head,
        "dirty": bool(after),
        "status": after.splitlines(),
        "untracked": untracked,
        "untracked_included": False,
        "explicitly_excluded": clean_excludes,
        "changed_during_capture": changed_during_capture,
    }
    incomplete = bool(untracked) or changed_during_capture or bool(clean_excludes)
    return state, patch or None, incomplete


def _semantic_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    # pack identity and wall-clock time do not change semantic content identity.
    return {
        key: value for key, value in manifest.items()
        if key not in {"pack_id", "digest", "created_at_us"}
    }


def _pack_digest(manifest: Mapping[str, Any]) -> str:
    return _sha256(canonical_json(_semantic_manifest(manifest)))


def create_context_pack(
    *, task_run_id: str, objective: Mapping[str, Any], policy: Mapping[str, Any],
    progress: Mapping[str, Any], memory: Mapping[str, Any], provenance: Mapping[str, Any],
    conversation: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    workspace: str | Path | None = None, requirements: Mapping[str, Any] | None = None,
    workspace_exclude: Sequence[str] = (),
    task_type: str | None = None, source_attempt_id: str | None = None,
    profile_revision_id: str | None = None, parent_pack_id: str | None = None,
    capture_status: str | None = None, capture_method: str = "reconstructed",
    home: str | Path | None = None, pack_id: str | None = None,
) -> tuple[dict[str, Any], Path]:
    if not task_run_id:
        raise ValueError("task_run_id is required")
    if capture_method not in _CAPTURE_METHODS:
        raise ValueError(f"unsupported capture_method: {capture_method}")
    entries: dict[str, bytes] = {
        "objective.json": canonical_json(objective),
        "policy.json": canonical_json(policy),
        "progress.json": canonical_json(progress),
        "memory.json": canonical_json(memory),
        "provenance.json": canonical_json(provenance),
    }
    if conversation is not None:
        entries["conversation.json"] = canonical_json(conversation)
    workspace_incomplete = False
    if workspace is not None:
        state, patch, workspace_incomplete = snapshot_workspace(
            workspace, exclude_paths=workspace_exclude,
        )
        entries["workspace/state.json"] = canonical_json(state)
        if patch:
            entries["workspace/changes.patch"] = patch
    if capture_status is None:
        capture_status = "dirty" if workspace_incomplete else "complete"
    if capture_status not in _CAPTURE_STATUSES:
        raise ValueError(f"unsupported capture_status: {capture_status}")
    for path, data in entries.items():
        _scan_secret(path, data)
    entry_manifest = [
        {
            "path": path,
            "kind": path.split("/", 1)[0].split(".", 1)[0],
            "sha256": _sha256(data),
            "bytes": len(data),
            "sensitivity": "private",
        }
        for path, data in sorted(entries.items())
    ]
    present = {entry["kind"] for entry in entry_manifest}
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "pack_id": pack_id or str(uuid4()),
        "digest": "",
        "parent_pack_id": parent_pack_id,
        "task_run_id": task_run_id,
        "source_attempt_id": source_attempt_id,
        "capture_status": capture_status,
        "capture_method": capture_method,
        "task_type": task_type,
        "profile_revision_id": profile_revision_id,
        "requirements": dict(requirements or {}),
        "layers": {
            layer: ("included" if layer in present else "omitted") for layer in LAYERS
        },
        "entries": entry_manifest,
        "created_at_us": time.time_ns() // 1_000,
    }
    if any(manifest["layers"][layer] != "included" for layer in _REQUIRED_LAYERS):
        raise ValueError("required Context Pack layer is missing")
    manifest["digest"] = _pack_digest(manifest)
    base = owned_directory(home, "packs")
    final = base / manifest["pack_id"]
    if final.exists():
        raise ValueError(f"Context Pack already exists: {manifest['pack_id']}")
    temporary = Path(tempfile.mkdtemp(prefix=".pack-", dir=base))
    try:
        for relative, data in entries.items():
            destination = temporary / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.write_bytes(data)
            os.chmod(destination, 0o600)
        manifest_path = temporary / "manifest.json"
        manifest_path.write_bytes(canonical_json(manifest))
        os.chmod(manifest_path, 0o600)
        validate_context_pack(temporary)
        os.replace(temporary, final)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return manifest, final


def validate_context_pack(path: str | Path, *, scan_secrets: bool = True) -> dict[str, Any]:
    root = Path(path).expanduser().resolve()
    try:
        raw = (root / "manifest.json").read_bytes()
        manifest = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid or missing Context Pack manifest") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported Context Pack schema: {manifest.get('schema_version')!r}")
    if manifest.get("capture_status") not in _CAPTURE_STATUSES:
        raise ValueError("invalid capture_status")
    if manifest.get("capture_method") not in _CAPTURE_METHODS:
        raise ValueError("invalid capture_method")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not 1 <= len(entries) <= _MAX_ENTRIES:
        raise ValueError("invalid Context Pack entries")
    seen: set[str] = set()
    total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid Context Pack entry")
        relative = str(entry.get("path") or "")
        safe = _safe_relative(relative)
        if relative in seen:
            raise ValueError(f"duplicate Context Pack entry: {relative}")
        seen.add(relative)
        target = (root / Path(*safe.parts)).resolve()
        if root not in target.parents or not target.is_file() or target.is_symlink():
            raise ValueError(f"missing or unsafe Context Pack entry: {relative}")
        data = target.read_bytes()
        total += len(data)
        if len(data) > _MAX_FILE_BYTES or total > _MAX_TOTAL_BYTES:
            raise ValueError("Context Pack exceeds size limits")
        if entry.get("bytes") != len(data) or entry.get("sha256") != _sha256(data):
            raise ValueError(f"Context Pack checksum mismatch: {relative}")
        if scan_secrets:
            _scan_secret(relative, data)
    actual_files = {
        item.relative_to(root).as_posix() for item in root.rglob("*")
        if item.is_file() and item.name != "manifest.json"
    }
    if actual_files != seen:
        raise ValueError("Context Pack contains unlisted files")
    if manifest.get("digest") != _pack_digest(manifest):
        raise ValueError("Context Pack digest mismatch")
    return manifest


def export_context_pack(path: str | Path, output: str | Path) -> Path:
    root = Path(path).expanduser().resolve()
    manifest = validate_context_pack(root)
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    names = ["manifest.json", *(entry["path"] for entry in manifest["entries"])]
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in names:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                archive.writestr(info, (root / name).read_bytes())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def import_context_pack(bundle: str | Path, *, home: str | Path | None = None) -> tuple[dict[str, Any], Path]:
    bundle = Path(bundle).expanduser().resolve()
    base = owned_directory(home, "packs")
    temporary = Path(tempfile.mkdtemp(prefix=".import-", dir=base))
    try:
        with zipfile.ZipFile(bundle) as archive:
            members = archive.infolist()
            if not 1 <= len(members) <= _MAX_ENTRIES + 1:
                raise ValueError("bundle entry count exceeds limit")
            total = 0
            seen: set[str] = set()
            for member in members:
                safe = _safe_relative(member.filename)
                if member.filename in seen or member.is_dir():
                    raise ValueError(f"duplicate or directory bundle entry: {member.filename}")
                seen.add(member.filename)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ValueError(f"bundle symlink is not allowed: {member.filename}")
                total += member.file_size
                if member.file_size > _MAX_FILE_BYTES or total > _MAX_TOTAL_BYTES:
                    raise ValueError("bundle exceeds size limits")
                if member.file_size and not member.compress_size:
                    raise ValueError(f"bundle compression metadata is unsafe: {member.filename}")
                if member.compress_size and member.file_size / member.compress_size > _MAX_COMPRESSION_RATIO:
                    raise ValueError(f"bundle compression ratio is unsafe: {member.filename}")
                destination = temporary / Path(*safe.parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                data = archive.read(member)
                if len(data) != member.file_size:
                    raise ValueError(f"bundle entry size mismatch: {member.filename}")
                destination.write_bytes(data)
                os.chmod(destination, 0o600)
        manifest = validate_context_pack(temporary)
        final = base / str(manifest["pack_id"])
        if final.exists():
            existing = validate_context_pack(final)
            if existing["digest"] != manifest["digest"]:
                raise ValueError(f"Context Pack id collision: {manifest['pack_id']}")
            return existing, final
        os.replace(temporary, final)
        return manifest, final
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("invalid Context Pack bundle") from exc
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="telos pack", description="Create and move immutable Context Packs.")
    sub = parser.add_subparsers(dest="command")
    inspect_parser = sub.add_parser("inspect")
    inspect_parser.add_argument("pack_id")
    export_parser = sub.add_parser("export")
    export_parser.add_argument("pack_id")
    export_parser.add_argument("-o", "--output", required=True)
    import_parser = sub.add_parser("import")
    import_parser.add_argument("bundle")
    parser.add_argument("--attempt")
    parser.add_argument("--objective")
    parser.add_argument("--done", action="append", default=[])
    parser.add_argument("--next", dest="next_steps", action="append", default=[])
    parser.add_argument("--decision", action="append", default=[])
    parser.add_argument("--workspace", default=str(Path.cwd()))
    parser.add_argument("--no-workspace", action="store_true")
    parser.add_argument("--exclude-workspace-path", action="append", default=[])
    parser.add_argument("--capture-method", choices=sorted(_CAPTURE_METHODS), default="reconstructed")
    parser.add_argument("--status", choices=sorted(_CAPTURE_STATUSES))
    args = parser.parse_args(argv)

    from telos.tracing import SQLiteTraceStore

    home = telos_home(os.environ.get("TELOS_HOME"))
    with SQLiteTraceStore(home / "telos.db") as store:
        if args.command == "inspect":
            pack = store.get_context_pack(args.pack_id)
            if pack is None:
                parser.error(f"Context Pack does not exist: {args.pack_id}")
            manifest = validate_context_pack(pack["path"])
            print(f"Context Pack {manifest['pack_id']}")
            print(f"  digest: {manifest['digest']}")
            print(f"  status: {manifest['capture_status']} ({manifest['capture_method']})")
            print(f"  TaskRun: {manifest['task_run_id']}")
            for layer, state in manifest["layers"].items():
                print(f"  {layer:<12} {state}")
            return 0
        if args.command == "export":
            pack = store.get_context_pack(args.pack_id)
            if pack is None:
                parser.error(f"Context Pack does not exist: {args.pack_id}")
            output = export_context_pack(pack["path"], args.output)
            print(f"exported {pack['digest']} → {output}")
            return 0
        if args.command == "import":
            manifest, path = import_context_pack(args.bundle, home=home)
            existing = store.get_context_pack(manifest["pack_id"])
            if existing is None:
                task_type_id = None
                if manifest.get("task_type"):
                    task_type_id = store.ensure_task_type(str(manifest["task_type"]))["id"]
                if store.get_task_run(manifest["task_run_id"]) is None:
                    objective = json.loads((path / "objective.json").read_text())
                    workspace_state = (
                        json.loads((path / "workspace/state.json").read_text())
                        if (path / "workspace/state.json").exists() else {}
                    )
                    store.create_task_run(
                        row_id=manifest["task_run_id"],
                        goal=str(objective.get("goal") or "Imported Context Pack"),
                        task_type_id=task_type_id, workspace=workspace_state,
                    )
                store.register_context_pack(manifest, path, detached=True)
            print(f"imported {manifest['digest']} → {path}")
            return 0

        attempt_id = args.attempt or os.environ.get("TELOS_ATTEMPT_ID")
        if not attempt_id:
            candidates = []
            for run in store.list_task_runs():
                candidates.extend(
                    attempt for attempt in store.get_task_run(run["id"])["attempts"]
                    if attempt["status"] in {"planned", "running"}
                )
            detail = "\n".join(
                f"  {item['id']}  {item['harness']}  {item['status']}" for item in candidates
            ) or "  (none)"
            parser.error("--attempt or TELOS_ATTEMPT_ID is required; candidates:\n" + detail)
        attempt = store.get_attempt(attempt_id)
        if attempt is None:
            parser.error(f"attempt does not exist: {attempt_id}")
        run_detail = store.get_task_run(attempt["task_run_id"])
        run = run_detail["task_run"]
        evidence = store.get_attempt_evidence(attempt_id)
        evolution = store.get_evolution(run["task_type_id"]) if run["task_type_id"] else None
        profile = store.get_profile_revision(attempt["profile_revision_id"]) if attempt["profile_revision_id"] else None
        explicit_semantics = bool((args.done or args.next_steps) and args.decision)
        manifest, path = create_context_pack(
            home=home, task_run_id=run["id"], source_attempt_id=attempt_id,
            profile_revision_id=attempt.get("profile_revision_id"),
            task_type=evolution["task_type"]["name"] if evolution else None,
            objective={"goal": args.objective or run["goal"], "constraints": []},
            policy={"profile_revision_id": attempt.get("profile_revision_id"),
                    "profile_digest": profile.get("digest") if profile else None},
            progress={
                "done": args.done or [f"Observed {evidence['completed_turns']} completed Trace turn(s)"],
                "in_progress": [], "next": args.next_steps, "blocked": [],
                "last_observed_result": evidence["last_output"],
            },
            memory={"facts": [], "decisions": args.decision, "assumptions": []},
            provenance={"harness": attempt["harness"], "attempt_id": attempt_id,
                        "trace_ids": evidence["trace_ids"], "capture_source": "telos pack CLI"},
            conversation=evidence["conversation"] or None,
            workspace=None if args.no_workspace else args.workspace,
            workspace_exclude=args.exclude_workspace_path,
            requirements={"workspace": "read-write", "tools": ["shell", "file-edit"]}
            if not args.no_workspace else {},
            capture_status=args.status or (None if explicit_semantics else "partial"),
            capture_method=args.capture_method,
        )
        store.register_context_pack(manifest, path)
        print(f"created Context Pack {manifest['pack_id']}")
        print(f"  digest: {manifest['digest']}")
        print(f"  status: {manifest['capture_status']} ({manifest['capture_method']})")
        print(f"  path:   {path}")
    return 0
