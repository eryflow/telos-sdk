"""Registry of external harness CLIs managed by telos.

Note the distinction between two "registries":

- ``registry.py``  —— loads the *in-process* prompt plugin objects (``HarnessPlugin``).
- this module      —— describes the *external executable* harness CLIs
  (``claude`` / ``codex`` …), used by ``telos init`` for auto-detection and
  by the bare ``telos`` command when launching a subprocess.

Each harness can be integrated with zero intrusion simply by pointing
``ANTHROPIC_BASE_URL`` / ``OPENAI_BASE_URL`` at the local gateway. claude-code
additionally supports patching ``~/.claude/settings.json``, so it goes
through the gateway even when not started via the ``telos`` launcher.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

# injection values:
#   "claude-settings" —— patch the env in ~/.claude/settings.json (claude-code exclusive)
#   "env"             —— inject solely via subprocess environment variables (set by the telos launcher at launch time)
_INJECTION_CLAUDE_SETTINGS = "claude-settings"
_INJECTION_ENV = "env"


@dataclass(frozen=True)
class HarnessSpec:
    """A static description of an external harness CLI."""

    name: str                 # canonical name (CLI subcommand / config key)
    display_name: str         # the human-readable name
    default_executable: str   # the default executable name (can be overridden by config)
    injection: str            # "claude-settings" | "env"
    env_var: str              # the name of the environment variable pointing at the gateway


# 4 preset harnesses. The executable names are reasonable default guesses —
# users can override them in the harness_executables of ~/.telos/config.json.
HARNESS_SPECS: dict[str, HarnessSpec] = {
    "claude-code": HarnessSpec(
        name="claude-code",
        display_name="Claude Code",
        default_executable="claude",
        injection=_INJECTION_CLAUDE_SETTINGS,
        env_var="ANTHROPIC_BASE_URL",
    ),
    "codex": HarnessSpec(
        name="codex",
        display_name="Codex",
        default_executable="codex",
        injection=_INJECTION_ENV,
        env_var="OPENAI_BASE_URL",
    ),
    "openclaw": HarnessSpec(
        name="openclaw",
        display_name="OpenClaw",
        default_executable="openclaw",
        injection=_INJECTION_ENV,
        env_var="ANTHROPIC_BASE_URL",
    ),
    "hermes": HarnessSpec(
        name="hermes",
        display_name="Hermes",
        default_executable="hermes",
        injection=_INJECTION_ENV,
        env_var="ANTHROPIC_BASE_URL",
    ),
}

# A stable order for the CLI / docs.
HARNESS_NAMES: tuple[str, ...] = tuple(HARNESS_SPECS.keys())


def get_spec(name: str) -> HarnessSpec:
    """Get a ``HarnessSpec`` by name; an unknown name raises a friendly wrapper around ``KeyError``."""
    try:
        return HARNESS_SPECS[name]
    except KeyError:
        raise ValueError(
            f"Unknown harness: {name!r}. Options: {', '.join(HARNESS_NAMES)}"
        ) from None


def resolve_executable(spec: HarnessSpec, executables: dict[str, str] | None = None) -> str:
    """The harness's actual executable name: a config override takes priority, otherwise the default guess."""
    if executables:
        override = executables.get(spec.name)
        if override:
            return override
    return spec.default_executable


def executable_path(spec: HarnessSpec, executables: dict[str, str] | None = None) -> str | None:
    """The absolute path of the harness executable on PATH; returns ``None`` if not installed."""
    resolved = shutil.which(resolve_executable(spec, executables))
    if resolved is not None:
        return resolved
    for candidate in _fallback_executable_candidates(spec):
        if candidate.exists() and candidate.is_file():
            return str(candidate)
    return None


def _fallback_executable_candidates(spec: HarnessSpec) -> tuple[Path, ...]:
    """Known app-bundled CLI locations that may not be exported onto PATH."""
    if spec.name == "codex":
        return (
            Path("/Applications/Codex.app/Contents/Resources/codex"),
            Path.home() / "Applications/Codex.app/Contents/Resources/codex",
        )
    return ()


def detect_installed(executables: dict[str, str] | None = None) -> list[HarnessSpec]:
    """Return the harnesses installed on the current machine (executable present on PATH)."""
    return [
        spec for spec in HARNESS_SPECS.values()
        if executable_path(spec, executables) is not None
    ]


def gateway_env(spec: HarnessSpec, base_url: str) -> dict[str, str]:
    """The environment variables required to point a given harness at the gateway."""
    return {spec.env_var: base_url}
