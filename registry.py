"""Plugin / adapter registry (load by name, avoiding hard top-level module dependencies).

Both harnesses and engines are instantiated here, ensuring the bridge does not
directly import any concrete implementation —— this is the code-level realization
of "the three tiers only pass values downward, never reference upward".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telos.harness.base import HarnessPlugin
    from telos.engine.base import EngineAdapter


_HARNESS_ALIASES: dict[str, str] = {
    "claude-code": "hermes",
}


def canonical_harness(name: str) -> str:
    """Resolve a harness alias into its canonical name (``claude-code`` → ``hermes``).

    Non-aliases are returned as-is. This ensures the usage log / dashboard show a
    consistent harness whether the caller passes an alias or the canonical name.
    """
    return _HARNESS_ALIASES.get(name, name)


# canonical harness name → human-facing display name shown on dashboards/reports.
# The internal codename (hermes) is not intuitive to users —— the dashboard always
# shows the name from here.
_HARNESS_DISPLAY_NAMES: dict[str, str] = {
    "openclaw": "OpenClaw",
    "hermes": "Claude Code",
    "telos": "Telos",
    "codex": "Codex",
}


def harness_display_name(name: str) -> str:
    """Map a harness name (canonical or alias) to its dashboard display name.

    ``hermes`` / ``claude-code`` → ``"Claude Code"``. Unknown names (e.g.
    ``passthrough`` / ``rtk-only`` / ``?``) are returned as-is.
    """
    if not name:
        return name
    return _HARNESS_DISPLAY_NAMES.get(canonical_harness(name), name)


def load_harness(name: str) -> "HarnessPlugin":
    """Load a harness plugin by name.

    Supported: ``openclaw``, ``hermes``, ``telos``
    Alias: ``claude-code`` → hermes
    """
    canonical = canonical_harness(name)
    if canonical == "openclaw":
        from telos.harness.openclaw import OpenClawPlugin
        return OpenClawPlugin()
    if canonical == "hermes":
        from telos.harness.hermes import HermesPlugin
        return HermesPlugin()
    if canonical == "telos":
        from telos.harness.telos import TelosPlugin
        return TelosPlugin()
    raise ValueError(f"Unknown harness plugin: {name!r}")


def load_engine(name: str) -> "EngineAdapter":
    """Load an engine adapter by name.

    Supported:
    - Closed-source APIs: ``anthropic``, ``openai``, ``deepseek``
    - Open-source inference (bidirectionally aware): ``vllm``, ``sglang``
    """
    if name == "anthropic":
        from telos.engine.anthropic import AnthropicAdapter
        return AnthropicAdapter()
    if name == "openai":
        from telos.engine.openai import OpenAIAdapter
        return OpenAIAdapter()
    if name == "deepseek":
        from telos.engine.deepseek import DeepSeekAdapter
        return DeepSeekAdapter()
    if name == "vllm":
        from telos.engine.vllm import VLLMAdapter
        return VLLMAdapter()
    if name == "sglang":
        from telos.engine.sglang import SGLangAdapter
        return SGLangAdapter()
    raise ValueError(f"Unknown engine adapter: {name!r}")
