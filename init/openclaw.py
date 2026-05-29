"""OpenClaw installer.

Routes the **primary** provider through the local telos gateway by patching
its ``baseUrl`` in ``~/.openclaw/openclaw.json``. Other providers are left
alone — telos lists them in the install output so the user can opt in by
re-running with ``providers_to_patch=[...]`` (or editing baseUrl by hand).

Original URLs are mirrored into ``~/.telos/config.json``'s ``upstreams`` table
and recorded in ``~/.telos/installer-state/openclaw.json`` for uninstall.
Re-running ``install()`` is **safe and re-aligning**:

- If the daemon URL drifted (e.g. ``:7171`` → ``:7392``), the patched
  ``baseUrl`` is updated to the new route URL automatically.
- The state's ``previous_base_url`` is **preserved** across re-patches —
  the installer detects when the current ``baseUrl`` looks like a stale
  telos route and keeps the original recorded URL rather than overwriting
  it with the stale intermediate.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from telos.config import (
    TelosConfig,
    UpstreamConfig,
    load_config,
    revert_upstreams_owned_by,
    save_config,
    telos_home,
)
from telos.init.base import AgentInstaller, InstallResult


_DEFAULT_GATEWAY_URL = "http://127.0.0.1:7171"
_STATE_VERSION = 2

# Matches ``http(s)://<host[:port]>/upstreams/<slug>[/]`` — used to detect
# when a current baseUrl is a stale telos route rather than the user's
# original upstream. The slug pattern is intentionally loose (URL-safe
# identifier) so user-named slugs are recognized too.
_TELOS_ROUTE_RE = re.compile(r"^https?://[^/]+/upstreams/[A-Za-z0-9_\-.]+/?$")


def _looks_like_telos_route(url: str) -> bool:
    return bool(_TELOS_ROUTE_RE.match(url))


def _default_config_path() -> Path:
    home = os.environ.get("OPENCLAW_HOME")
    return (Path(home) if home else Path.home() / ".openclaw") / "openclaw.json"


def _default_state_path() -> Path:
    return telos_home() / "installer-state" / "openclaw.json"


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _classify_api(api: str) -> tuple[str, str]:
    if api == "anthropic-messages":
        return "anthropic-messages", "anthropic"
    return "openai-chat", "deepseek"


@dataclass
class _ProviderInfo:
    provider_id: str
    base_url: str
    api: str


def _all_provider_ids(data: dict[str, Any]) -> list[str]:
    """All provider ids under ``models.providers`` (regardless of validity)."""
    models = data.get("models") or {}
    providers = models.get("providers") if isinstance(models, dict) else None
    if not isinstance(providers, dict):
        return []
    return [str(k) for k in providers.keys()]


def _provider_info(data: dict[str, Any], pid: str) -> _ProviderInfo | None:
    providers = (data.get("models") or {}).get("providers") or {}
    entry = providers.get(pid) if isinstance(providers, dict) else None
    if not isinstance(entry, dict):
        return None
    base_url = entry.get("baseUrl")
    if not isinstance(base_url, str) or not base_url:
        return None
    api = str(entry.get("api") or "openai-completions")
    return _ProviderInfo(provider_id=pid, base_url=base_url, api=api)


def _primary_provider_id(data: dict[str, Any]) -> str | None:
    agents = data.get("agents") or {}
    if not isinstance(agents, dict):
        return None
    defaults = agents.get("defaults")
    if not isinstance(defaults, dict):
        return None
    model = defaults.get("model")
    if not isinstance(model, dict):
        return None
    primary = model.get("primary")
    if not isinstance(primary, str) or "/" not in primary:
        return None
    return primary.split("/", 1)[0]


def _read_state_file(path: Path) -> list[dict[str, Any]]:
    """v1 single-record + v2 list. Always returns a list."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if isinstance(raw, dict) and isinstance(raw.get("patched"), list):
        return [r for r in raw["patched"] if isinstance(r, dict)]
    if isinstance(raw, dict) and "provider_id" in raw:
        return [raw]
    return []


def _write_state_file(path: Path, patched: Sequence[dict[str, Any]]) -> None:
    _atomic_write_json(path, {"version": _STATE_VERSION,
                              "patched": list(patched)})


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------

class OpenClawInstaller(AgentInstaller):
    name = "openclaw"

    def __init__(
        self,
        *,
        proxy_url: str = _DEFAULT_GATEWAY_URL,
        config_path: Path | None = None,
        state_path: Path | None = None,
        providers_to_patch: Sequence[str] | None = None,
    ) -> None:
        super().__init__(proxy_url=proxy_url)
        self._config_path = config_path or _default_config_path()
        self._state_path = state_path or _default_state_path()
        # Escape hatch: explicit list of provider ids to patch. None (default)
        # → patch only the primary provider. Other providers are left alone
        # and surfaced in the install notes as informational.
        self._providers_to_patch = (
            list(providers_to_patch) if providers_to_patch is not None else None
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _read_config(self) -> dict[str, Any] | None:
        if not self._config_path.exists():
            return None
        text = self._config_path.read_text(encoding="utf-8")
        if not text.strip():
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"{self._config_path} is not valid JSON ({e}). "
                f"Restore from {self._config_path.with_suffix(self._config_path.suffix + '.telos.bak')} "
                f"or fix the file manually, then re-run `telos init`."
            ) from e
        if not isinstance(data, dict):
            raise RuntimeError(
                f"{self._config_path} top level must be a JSON object, "
                f"but is actually {type(data).__name__}."
            )
        return data

    def _telos_route_url(self, slug: str) -> str:
        return f"{self.proxy_url.rstrip('/')}/upstreams/{slug}"

    def _ensure_upstream_slug(
        self, telos_cfg: TelosConfig, slug: str, url: str, api: str,
    ) -> bool:
        protocol, engine = _classify_api(api)
        existing = telos_cfg.upstreams.get(slug)
        # Strip any /vN version suffix so the gateway can construct
        # {url}/{tail} where tail already includes "v1/" (added by OpenAI SDK
        # when the patched base_url has no /vN suffix).
        normalized_url = re.sub(r"/v\d+$", "", url.rstrip("/"))
        desired = UpstreamConfig(url=normalized_url, engine=engine,
                                  protocol=protocol, via=self.name)
        if existing == desired:
            return False
        telos_cfg.upstreams[slug] = desired
        return True

    # ------------------------------------------------------------------
    # install
    # ------------------------------------------------------------------

    def install(self) -> InstallResult:
        r = InstallResult(agent=self.name, action="install")

        data = self._read_config()
        if data is None:
            r.notes.append(
                f"{self._config_path} does not exist — install openclaw "
                f"and run `openclaw setup` first."
            )
            return r

        all_ids = _all_provider_ids(data)
        if not all_ids:
            r.notes.append("no providers found under models.providers.")
            return r

        # ---- Choose targets: explicit kwarg > primary ----
        primary_id = _primary_provider_id(data)
        if self._providers_to_patch is not None:
            target_ids = list(self._providers_to_patch)
        elif primary_id is not None:
            target_ids = [primary_id]
        else:
            r.notes.append(
                "agents.defaults.model.primary is not set; configure a "
                "primary model in openclaw first (or pass providers_to_patch)."
            )
            return r

        # Resolve to _ProviderInfo, skipping invalid ids with a note.
        targets: list[_ProviderInfo] = []
        for tid in target_ids:
            info = _provider_info(data, tid)
            if info is None:
                r.notes.append(
                    f"models.providers.{tid} not found or has no baseUrl; "
                    f"skipping"
                )
                continue
            targets.append(info)

        if not targets:
            r.notes.append("nothing to patch.")
            return r

        # ---- Load existing state to preserve original URLs on re-patch ----
        existing_state = _read_state_file(self._state_path)
        state_by_id = {s["provider_id"]: s for s in existing_state}

        # ---- Decide what actually needs changing ----
        # For each target: figure out (a) what the user's TRUE upstream URL is
        # (needed for telos config), and (b) whether openclaw.json needs a
        # baseUrl patch. The two are independent — telos config might need a
        # refresh (e.g. to add the new ``via`` field on upgrade) even when
        # openclaw.json is already routed correctly.
        resolved: list[tuple[_ProviderInfo, str, bool]] = []  # (info, original_url, needs_patch)
        skipped_stale_no_state: list[str] = []

        for t in targets:
            route_url = self._telos_route_url(t.provider_id)
            existing_rec = state_by_id.get(t.provider_id)

            if t.base_url == route_url:
                # Already routed. Recover original from state; if state is
                # missing, we cannot refresh the telos upstream slug either,
                # so skip silently (status will surface this).
                if existing_rec is None:
                    continue
                resolved.append((t, existing_rec["previous_base_url"], False))
            elif _looks_like_telos_route(t.base_url):
                # Stale route: refuse to patch when we cannot recover the
                # original, since patching would lock the stale URL in as
                # "original" and uninstall could never restore the real upstream.
                if existing_rec is None:
                    skipped_stale_no_state.append(t.provider_id)
                    continue
                resolved.append((t, existing_rec["previous_base_url"], True))
            else:
                # Plain original URL.
                resolved.append((t, t.base_url, True))

        for pid in skipped_stale_no_state:
            r.notes.append(
                f"models.providers.{pid}.baseUrl is a stale telos route "
                f"but state file has no record of the original. "
                f"Edit it back to your real upstream URL and re-run install."
            )

        if not resolved:
            r.notes.append("nothing to patch.")
            return r

        # ---- Always refresh telos upstream slugs (even when openclaw.json is
        # already routed) so the ``via`` field stays current across upgrades. ----
        telos_cfg = load_config()
        telos_changed = False
        for info, original_url, _ in resolved:
            if self._ensure_upstream_slug(telos_cfg, info.provider_id,
                                           original_url, info.api):
                telos_changed = True
                r.notes.append(
                    f"telos upstream `{info.provider_id}` → {original_url}"
                )
        if telos_changed:
            telos_path = save_config(telos_cfg)
            r.changed_files.append(telos_path)

        to_patch = [(info, orig) for info, orig, needs in resolved if needs]
        if not to_patch:
            # openclaw.json is already correct; only the telos side may have changed.
            already_routed_ids = [info.provider_id for info, _, needs in resolved
                                   if not needs]
            for pid in already_routed_ids:
                r.notes.append(
                    f"models.providers.{pid}.baseUrl already routed; no change."
                )
            r.already_installed = not telos_changed
            return r

        # ---- Backup once before any mutation to openclaw.json ----
        backup = self._config_path.with_suffix(
            self._config_path.suffix + ".telos.bak"
        )
        if not backup.exists():
            shutil.copy2(self._config_path, backup)
            r.backups.append(backup)

        # ---- Patch openclaw.json ----
        for info, original_url in to_patch:
            route_url = self._telos_route_url(info.provider_id)
            data["models"]["providers"][info.provider_id]["baseUrl"] = route_url
            state_by_id[info.provider_id] = {
                "provider_id": info.provider_id,
                "previous_base_url": original_url,
                "gateway_route_url": route_url,
            }
            r.notes.append(
                f"patched models.providers.{info.provider_id}.baseUrl "
                f"→ {route_url}"
            )

        _atomic_write_json(self._config_path, data)
        r.changed_files.append(self._config_path)
        _write_state_file(self._state_path, list(state_by_id.values()))

        # ---- Informational: list other providers not patched ----
        patched_ids = {info.provider_id for info, _ in to_patch}
        patched_ids.update(info.provider_id for info, _, needs in resolved if not needs)
        other_ids = [pid for pid in all_ids if pid not in patched_ids]
        if other_ids:
            r.notes.append(
                f"other providers detected ({', '.join(other_ids)}); not "
                f"patched. Edit their baseUrl manually if you want them too."
            )
        return r

    # ------------------------------------------------------------------
    # uninstall
    # ------------------------------------------------------------------

    def uninstall(self) -> InstallResult:
        r = InstallResult(agent=self.name, action="uninstall")

        # Undo any ~/.telos/config.json upstream entries owned by openclaw
        # regardless of openclaw.json state. This makes uninstall idempotent
        # and tolerant of partial prior runs.
        telos_saved, telos_changes = revert_upstreams_owned_by(self.name)
        if telos_saved is not None:
            r.changed_files.append(telos_saved)
            r.notes.extend(telos_changes)

        records = _read_state_file(self._state_path)
        if not records:
            if telos_saved is None:
                r.notes.append(
                    f"no telos installer state for openclaw "
                    f"({self._state_path}); nothing to undo."
                )
            return r

        data = self._read_config()
        if data is None:
            r.notes.append(
                f"{self._config_path} does not exist; deleting stale state."
            )
            self._state_path.unlink(missing_ok=True)
            return r

        providers = (data.get("models") or {}).get("providers") or {}
        if not isinstance(providers, dict):
            r.notes.append("models.providers is missing; deleting stale state.")
            self._state_path.unlink(missing_ok=True)
            return r

        remaining: list[dict[str, Any]] = []
        changed = False
        for rec in records:
            pid = rec.get("provider_id")
            prev_url = rec.get("previous_base_url")
            route_url = rec.get("gateway_route_url")
            entry = providers.get(pid)
            if not isinstance(entry, dict):
                r.notes.append(
                    f"models.providers.{pid} no longer exists; dropping state"
                )
                continue
            current = entry.get("baseUrl")
            if current != route_url:
                # Could be: user edited manually, OR a stale route from a
                # daemon-port change. Either way, leave it alone.
                remaining.append(rec)
                r.notes.append(
                    f"models.providers.{pid}.baseUrl is {current!r} "
                    f"(not the route we set); leaving it alone"
                )
                continue
            entry["baseUrl"] = prev_url
            changed = True
            r.notes.append(
                f"restored models.providers.{pid}.baseUrl → {prev_url}"
            )

        if changed:
            _atomic_write_json(self._config_path, data)
            r.changed_files.append(self._config_path)

        if remaining:
            _write_state_file(self._state_path, remaining)
        else:
            self._state_path.unlink(missing_ok=True)
        return r

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self) -> InstallResult:
        r = InstallResult(agent=self.name, action="status")

        if not self._config_path.exists():
            r.notes.append(f"{self._config_path} does not exist")
            return r

        data = self._read_config()
        records = _read_state_file(self._state_path)
        state_by_id = {s["provider_id"]: s for s in records}

        if data is None:
            r.notes.append("openclaw.json is not readable")
            return r

        ids = _all_provider_ids(data)
        if not ids:
            r.notes.append("no providers under models.providers")
            return r

        any_routed = False
        for pid in ids:
            info = _provider_info(data, pid)
            if info is None:
                continue
            route_url = self._telos_route_url(pid)
            if info.base_url == route_url:
                any_routed = True
                prev = state_by_id.get(pid, {}).get("previous_base_url")
                suffix = f" (restores to {prev!r} on uninstall)" if prev else ""
                r.notes.append(f"  ✓ {pid}: routed → {route_url}{suffix}")
            elif _looks_like_telos_route(info.base_url):
                r.notes.append(
                    f"  ! {pid}: stale telos route ({info.base_url}); "
                    f"re-run install to re-align"
                )
            else:
                r.notes.append(f"  · {pid}: direct → {info.base_url}")
        r.already_installed = any_routed
        return r
