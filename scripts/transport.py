"""Unified SDK Transport entry point — one-click integration of openclaw / hermes / claude-code.

Usage:

    from telos.scripts.transport import TelosTransport

    # Option 1: pick a harness directly (recommended)
    transport = TelosTransport.for_harness("claude-code")
    resp = transport.messages.create(model="claude-opus-4-7", system=[...], messages=[...], tools=[...])

    # Option 2: auto-detect (guess the harness from request content)
    transport = TelosTransport.auto(session_id="my-session")

Each harness preset comes pre-configured with:
- the corresponding harness plugin (request parsing)
- the default engine adapter (wire generation + cache policy)
- the default base_url and API key environment variable

Any default can also be overridden via kwargs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

from telos.bridge import BridgeSessionState


# ---------------------------------------------------------------------------
# Harness preset definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HarnessPreset:
    """The complete default configuration of a harness."""
    harness_name: str
    engine_name: str
    wire_protocol: Literal["anthropic", "openai"]
    default_base_url: str | None = None
    api_key_env: str = "ANTHROPIC_API_KEY"
    description: str = ""


PRESETS: dict[str, HarnessPreset] = {
    "openclaw": HarnessPreset(
        harness_name="openclaw",
        engine_name="anthropic",
        wire_protocol="anthropic",
        default_base_url=None,
        api_key_env="ANTHROPIC_API_KEY",
        description="OpenClaw agent — Anthropic /v1/messages wire, explicit cache breakpoints",
    ),
    "claude-code": HarnessPreset(
        harness_name="claude-code",
        engine_name="anthropic",
        wire_protocol="anthropic",
        default_base_url=None,
        api_key_env="ANTHROPIC_API_KEY",
        description="Claude Code — Anthropic /v1/messages wire with system-reminder / command envelope tags",
    ),
    "hermes": HarnessPreset(
        harness_name="hermes",
        engine_name="anthropic",
        wire_protocol="anthropic",
        default_base_url=None,
        api_key_env="ANTHROPIC_API_KEY",
        description="Hermes-CLI — Anthropic /v1/messages wire (independent of Claude Code as of 2026-05)",
    ),
}


# ---------------------------------------------------------------------------
# Unified Transport
# ---------------------------------------------------------------------------

class TelosTransport:
    """Unified SDK Transport — one-click integration of openclaw / hermes / claude-code.

    The instance returned by ``for_harness("claude-code")`` carries the
    ``messages.create()`` interface, and also uniformly exposes a
    ``create(**kwargs)`` method.
    """

    def __init__(
        self,
        *,
        preset: HarnessPreset,
        api_key: str | None = None,
        base_url: str | None = None,
        session_id: str = "telos-session",
        engine_name: str | None = None,
        usage_log: str | None = None,
        prompt_trace_log: str | None = None,
        session_state: BridgeSessionState | None = None,
    ):
        from telos.scripts.telos_anthropic_transport import TelosAnthropicTransport

        self._preset = preset
        effective_engine = engine_name or preset.engine_name
        effective_base_url = base_url or preset.default_base_url
        effective_api_key = api_key or os.environ.get(preset.api_key_env, "")

        kwargs: dict[str, Any] = {
            "api_key": effective_api_key,
            "harness_name": preset.harness_name,
            "engine_name": effective_engine,
            "session_id": session_id,
            "usage_log": usage_log,
            "prompt_trace_log": prompt_trace_log,
            "session_state": session_state,
        }
        if effective_base_url is not None:
            kwargs["base_url"] = effective_base_url
        self._inner = TelosAnthropicTransport(**kwargs)
        self.messages = self._inner.messages

    @property
    def session_state(self) -> BridgeSessionState:
        return self._inner.session_state

    @property
    def preset(self) -> HarnessPreset:
        return self._preset

    def create(self, **kwargs) -> Any:
        """Unified call entry point — routes to messages.create()."""
        return self.messages.create(**kwargs)

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def for_harness(
        cls,
        name: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        session_id: str = "telos-session",
        engine_name: str | None = None,
        usage_log: str | None = None,
        prompt_trace_log: str | None = None,
        session_state: BridgeSessionState | None = None,
    ) -> "TelosTransport":
        """Create a transport with one click by harness name.

        Supported harness names:
        - ``"openclaw"``    — OpenClaw agent
        - ``"hermes"``      — Claude Code / Hermes
        - ``"claude-code"`` — same as hermes

        All configuration items have sensible defaults; pass kwargs to override.

        Example::

            transport = TelosTransport.for_harness("claude-code")
            resp = transport.messages.create(
                model="claude-opus-4-7",
                system=[{"type": "text", "text": "You are helpful."}],
                messages=[{"role": "user", "content": "Hello"}],
                max_tokens=1024,
            )
        """
        preset = PRESETS.get(name)
        if preset is None:
            available = ", ".join(sorted(PRESETS))
            raise ValueError(
                f"Unknown harness {name!r}. Available: {available}"
            )
        return cls(
            preset=preset,
            api_key=api_key,
            base_url=base_url,
            session_id=session_id,
            engine_name=engine_name,
            usage_log=usage_log,
            prompt_trace_log=prompt_trace_log,
            session_state=session_state,
        )

    @classmethod
    def auto(
        cls,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        session_id: str = "telos-session",
        usage_log: str | None = None,
        prompt_trace_log: str | None = None,
        session_state: BridgeSessionState | None = None,
    ) -> "TelosTransport":
        """Auto-detect mode — harness_name=None, inferred from content on the first request.

        Defaults to the Anthropic wire (openclaw / hermes switch automatically).
        """
        from telos.scripts.telos_anthropic_transport import TelosAnthropicTransport
        preset = HarnessPreset(
            harness_name="",
            engine_name="anthropic",
            wire_protocol="anthropic",
            api_key_env="ANTHROPIC_API_KEY",
            description="Auto-detect harness from request content",
        )
        transport = cls.__new__(cls)
        transport._preset = preset
        transport._wire = "anthropic"

        kwargs: dict[str, Any] = {
            "session_id": session_id,
            "harness_name": None,
            "engine_name": "anthropic",
            "usage_log": usage_log,
            "prompt_trace_log": prompt_trace_log,
            "session_state": session_state,
        }
        if api_key is not None:
            kwargs["api_key"] = api_key
        if base_url is not None:
            kwargs["base_url"] = base_url
        transport._inner = TelosAnthropicTransport(**kwargs)
        transport.messages = transport._inner.messages
        return transport

    @staticmethod
    def available_presets() -> dict[str, str]:
        """Return a mapping of all available preset names → descriptions."""
        return {name: p.description for name, p in PRESETS.items()}
