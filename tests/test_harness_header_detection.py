"""Unit tests for HTTP-header harness detection + proxy per-client memory (no network).

Reproduces and verifies the fix: Claude Code's auxiliary requests sent via Haiku (title
generation / topic detection) have no tools and no ``<system-reminder>`` tag, so pure
content detection misclassifies them as openclaw. After introducing HTTP-header detection
and proxy per-client memory, these requests are also correctly identified as claude-code.

These checks are now the **fallback path** — the primary attribution mechanism is the
``/_h/<harness>/`` URL tag stamped by ``handle_tagged_route``. See
``tests/test_url_tag_routing.py``.
"""

from __future__ import annotations

from telos.proxy.server import ProxyApp
from telos.scripts.telos_anthropic_transport import (
    _detect_harness,
    _detect_harness_from_headers,
    _detect_harness_signal,
)


# Claude Code main-conversation request: has tools + system-reminder
_MAIN_REQ = {
    "model": "claude-opus-4-7",
    "system": [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI."}],
    "tools": [{"name": n} for n in ("Bash", "Read", "Edit", "Write", "Grep")],
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "fix it <system-reminder>cwd=/repo</system-reminder>"}]}],
}

# Claude Code auxiliary request: Haiku title generation -- no tools, no tags, no harness signal in the content
_AUX_REQ = {
    "model": "claude-haiku-4-5-20251001",
    "system": [{"type": "text", "text": "Summarize this conversation in 5-10 words."}],
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "User asked to fix a login bug."}]}],
}


# ---------------------------------------------------------------------------
# _detect_harness_from_headers
# ---------------------------------------------------------------------------

def test_headers_user_agent_identifies_claude_code() -> None:
    assert _detect_harness_from_headers(
        {"User-Agent": "claude-cli/1.2.3 (external, cli)"}) == "claude-code"
    # key is case-insensitive
    assert _detect_harness_from_headers(
        {"user-agent": "Claude-CLI/9.9"}) == "claude-code"
    print("✓ test_headers_user_agent_identifies_claude_code")


def test_headers_x_app_identifies_claude_code() -> None:
    assert _detect_harness_from_headers({"x-app": "cli"}) == "claude-code"
    assert _detect_harness_from_headers({"X-App": "CLI"}) == "claude-code"
    print("✓ test_headers_x_app_identifies_claude_code")


def test_headers_unknown_returns_none() -> None:
    assert _detect_harness_from_headers({}) is None
    assert _detect_harness_from_headers({"user-agent": "python-httpx/0.27"}) is None
    assert _detect_harness_from_headers({"x-app": "web"}) is None
    print("✓ test_headers_unknown_returns_none")


# ---------------------------------------------------------------------------
# _detect_harness_signal -- distinguishes a confident signal vs a fallback
# ---------------------------------------------------------------------------

def test_signal_header_classifies_toolless_aux_request() -> None:
    # key reproduction: a tool-less auxiliary request, content detection will inevitably miss --
    ua = {"user-agent": "claude-cli/1.2.3"}
    assert _detect_harness_signal(_AUX_REQ) is None                # no header → no signal
    assert _detect_harness_signal(_AUX_REQ, ua) == "claude-code"   # has header → confident claude-code
    print("✓ test_signal_header_classifies_toolless_aux_request")


def test_signal_content_rules_still_work() -> None:
    # a content-rich request can be confidently identified even without a header
    assert _detect_harness_signal(_MAIN_REQ) == "claude-code"
    # a pure openclaw shape (no tools, no tags, no header) → no confident signal
    openclaw_req = {"model": "claude-opus-4-7",
                    "system": [{"type": "text", "text": "You are an agent."}],
                    "messages": [{"role": "user", "content": "hi"}]}
    assert _detect_harness_signal(openclaw_req) is None
    print("✓ test_signal_content_rules_still_work")


def test_detect_harness_backcompat() -> None:
    # single-argument calls (SDK transport / replay / old tests) still work, falling back to openclaw
    assert _detect_harness(_MAIN_REQ) == "claude-code"
    assert _detect_harness(_AUX_REQ) == "openclaw"                 # no header → fallback
    assert _detect_harness(_AUX_REQ, {"x-app": "cli"}) == "claude-code"
    print("✓ test_detect_harness_backcompat")


# ---------------------------------------------------------------------------
# proxy per-client memory
# ---------------------------------------------------------------------------

class _FakeRequest:
    """Exposes ``headers`` + ``get`` -- that is all ``_resolve_harness`` uses.

    ``get(key, default)`` mimics aiohttp's request mapping (used here to
    return the URL-tag stamp, which is absent for these fallback-path tests).
    """

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers

    def get(self, key, default=None):
        return default


def test_proxy_resolves_aux_request_via_header() -> None:
    proxy = ProxyApp()
    ua = {"x-api-key": "k1", "user-agent": "claude-cli/1.2.3"}
    # the auxiliary request carries Claude Code's User-Agent → directly judged claude-code
    assert proxy._resolve_harness(_FakeRequest(ua), _AUX_REQ) == "claude-code"
    print("✓ test_proxy_resolves_aux_request_via_header")


def test_proxy_client_memory_covers_headerless_aux_request() -> None:
    proxy = ProxyApp()
    # turn 1: main conversation, with Claude Code UA → pin this client to claude-code
    main_hdr = {"x-api-key": "k2", "user-agent": "claude-cli/1.2.3"}
    assert proxy._resolve_harness(_FakeRequest(main_hdr), _MAIN_REQ) == "claude-code"
    # turn 2: the same client (same x-api-key) sends a tool-less auxiliary request, and this time
    # it doesn't even have a User-Agent -- relying on per-client memory inheritance, it is no longer misclassified as openclaw.
    aux_hdr = {"x-api-key": "k2"}
    assert proxy._resolve_harness(_FakeRequest(aux_hdr), _AUX_REQ) == "claude-code"
    print("✓ test_proxy_client_memory_covers_headerless_aux_request")


def test_proxy_unknown_client_falls_back_to_openclaw() -> None:
    proxy = ProxyApp()
    # a brand-new client with no signal and no memory → falls back to openclaw
    hdr = {"x-api-key": "k3"}
    assert proxy._resolve_harness(_FakeRequest(hdr), _AUX_REQ) == "openclaw"
    print("✓ test_proxy_unknown_client_falls_back_to_openclaw")


def test_proxy_explicit_override_wins() -> None:
    proxy = ProxyApp(harness_override="claude-code")
    # an explicit override takes top priority; the canonical name flows through as-is
    # (alias resolution was removed in 2026-05).
    hdr = {"x-api-key": "k4", "user-agent": "python-httpx/0.27"}
    assert proxy._resolve_harness(_FakeRequest(hdr), _AUX_REQ) == "claude-code"
    print("✓ test_proxy_explicit_override_wins")


if __name__ == "__main__":
    test_headers_user_agent_identifies_claude_code()
    test_headers_x_app_identifies_claude_code()
    test_headers_unknown_returns_none()
    test_signal_header_classifies_toolless_aux_request()
    test_signal_content_rules_still_work()
    test_detect_harness_backcompat()
    test_proxy_resolves_aux_request_via_header()
    test_proxy_client_memory_covers_headerless_aux_request()
    test_proxy_unknown_client_falls_back_to_openclaw()
    test_proxy_explicit_override_wins()
    print("all harness header-detection tests passed")

