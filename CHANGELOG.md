# CHANGELOG

This file records user-visible code changes; for protocol-level design changes see
[`docs/2026-05-06-telos-protocol.md`](docs/2026-05-06-telos-protocol.md).

The format follows Keep a Changelog; dates are absolute.

---

## [0.1.7] — 2026-05-31

### Added

- **`telos status`** — new CLI command. Shows gateway running state, per-harness
  install status, and live traffic summary in one place
  (`telos status --json` for machine-readable output).
- **`telos uninstall`** — new CLI command. Removes the proxy injection from the
  detected (or explicitly specified) harness and restores the original config.
- **`telos version` / `telos -V`** — prints the installed SDK version.

### Fixed

- **Gateway URL resolution in `telos status` and `telos uninstall`** — the commands
  now resolve the correct gateway URL even when the proxy runs on a non-default
  port or behind a custom `ANTHROPIC_BASE_URL`.
- **Claude TTL block** — `cache_control` blocks with `ttl: "1h"` were being emitted
  after `ttl: "ephemeral"` (5 m) blocks in some tool-heavy sessions, violating the
  Anthropic ordering requirement and triggering HTTP 400. Fixed by enforcing the
  `long-before-short` invariant in `AnthropicAdapter.plan_marks`.
- **Codex HTML passthrough guard** — Codex `/v1/responses` upstream now detects
  accidental HTML error responses from the backend (e.g. 403 Cloudflare pages) and
  converts them to a structured JSON error instead of forwarding raw HTML.
- **Uninstall / harness CLI hardening** — uninstall correctly handles partially-
  modified configs and external rewrites without data loss; CLI validates harness
  names and suggests the closest match on typo.

### Changed

- **Test suite overhaul** — 228 → 272 tests (all green). Added dedicated unit tests
  for `refpool` (slug-freeze invariants, `lint` fail-fast), bridge canonicalization
  determinism (the "byte-stable prefix" property), and Anthropic BP-trim priority
  (R7 / R2 mid-anchor). Removed 8 stale `EnvInstaller` tests, 3 cosmetic HTML-
  string assertions, and 3 true duplicates. Proxy/gateway monolithic async tests
  (previously invisible to pytest) split into individually collectable functions.
- **CI matrix** — pytest now runs on every push/PR across Python 3.10 / 3.11 /
  3.12 / 3.13 (previously only 3.12 inside Docker). PyPI publish requires all four
  matrix legs to pass (`needs: [build, test]`).

---

## [0.1.6] — 2026-05-29

### Fixed

- **`telos init` now restarts the gateway when an installer registers a new upstream.**
  `telos init codex` (chatgpt mode) / `telos init openclaw` / `telos init hermes`
  inject new entries into `~/.telos/config.json`, but the gateway only reads
  `upstreams` at boot. If the daemon was already running, the previous
  idempotent `start_detached()` was a no-op, so requests to a freshly-added
  slug (e.g. `/upstreams/codex-chatgpt/responses`) kept 404ing with
  `unknown upstream slug` until the user manually ran `telos gateway restart`.
  `init/__main__.py` now detects that an installer mutated the telos config
  and calls `daemon.restart()` instead.

---

## [0.1.5] — 2026-05-28

### Changed

- **Live dashboard separated from replay comparisons.** Dashboard now distinguishes
  live gateway traffic from A/B replay results, with refreshed branding and
  improved table layout.

### Fixed

- **Uninstall flow** correctly removes Codex/Hermes/OpenClaw harness configs.
- **Install method** updates for Codex (`init/codex.py`), Hermes
  (`init/hermes.py`), and OpenClaw (`init/openclaw.py`).

---

## [0.1.4] — 2026-05-27

Codex (`wire_api = "responses"`) is now a first-class harness on the gateway.

### Fixed

- **Codex.app with ChatGPT login could not reach the gateway** ([init/codex.py](init/codex.py)):
  the installer assumed an `OPENAI_API_KEY` provider, so it pointed Codex's
  custom `base_url` at `/upstreams/openai/v1` → `api.openai.com/v1/responses`.
  Codex.app users (the common case) authenticate with a ChatGPT JWT, which
  `api.openai.com` rejects (`403 "Missing scopes: api.model.read"`).
  The installer now reads `~/.codex/auth.json` and, when `auth_mode = "chatgpt"`,
  routes through a new `codex-chatgpt` upstream pointing at
  `https://chatgpt.com/backend-api/codex` (no `/v1` prefix). API-key mode is
  unchanged.

### Added

- **Dashboard shows Codex traffic.** The Responses-API endpoint (`POST .../responses`)
  is no longer a silent passthrough on the gateway: it parses the terminal
  `response.completed` SSE event, normalizes the usage block
  (`input_tokens` − `input_tokens_details.cached_tokens` → raw / cache_read /
  cache_write / output), and writes a `usage.jsonl` row tagged with the slug's
  `via` (`codex`). [registry.py](registry.py) adds the `codex` → "Codex"
  display name so the dashboard's "Breakdown by harness" table picks it up.
- New tests: `_test_openai_responses_logs_usage`
  ([tests/test_proxy_openai_route.py](tests/test_proxy_openai_route.py)),
  `test_detect_auth_mode` and `test_install_chatgpt_mode_routes_to_backend_codex`
  ([tests/test_init_codex.py](tests/test_init_codex.py)).

## [0.1.3] — 2026-05-22

Patch release published to PyPI.

### Changed

- Bumped package version to `0.1.3` in `pyproject.toml` for a new publishable artifact set.
- Updated project metadata description to a concise repository summary.
- Added LEAP Lab attribution and homepage link in README headers (English and Chinese).

## [Unreleased] — 2026-05-18

This batch reworks the CLI experience: it upgrades `proxy` into a "gateway + multi-harness manager",
so that telos installs with one command, integrates with one command, and typing `telos` drops you
straight into your usual harness.

### Added

- **`telos init` (no arguments)** — auto-detects the harness CLIs installed locally (claude-code / codex /
  openclaw / hermes), injects gateway-pointing config into each, starts the gateway in the background, and prints
  the gateway and dashboard addresses.
- **`telos gateway start|stop|status|restart`** — gateway daemon management.
  Background start (`subprocess` + `start_new_session`), PID / state written to `~/.telos/`,
  idempotent, with `--foreground` for foreground operation.
- **bare `telos` / `telos <harness>`** — an interactive menu to pick a harness (or enter one directly); telos
  injects the gateway environment variables and then `exec`s into the corresponding harness's CLI.
- **`telos alias <harness>`** — sets the harness that bare `telos` enters by default.
- **`telos mode [none|telos|rtk|both]`** — switch the optimization gear; via a localhost control endpoint it
  **hot-reloads the running gateway** (no restart needed), and persists to config.
- **`telos dashboard`** — opens the dashboard in the browser (live if the gateway is running, otherwise builds a static HTML).
- **`~/.telos/config.json`** — new global user config (`telos.config`): default mode,
  gateway host/port, favorite harness, harness executable-name overrides.
- **gateway control endpoint** `GET/POST /__telos/control/mode` — loopback only, hot-switches the default mode.
- **Homebrew formula template** `packaging/telos-sdk.rb`; `pip install telos-sdk` works out of the box.

### Changed

- `proxy` is renamed **gateway** for users: startup banner, CLI, and docs are unified. `telos proxy`
  is kept as a hidden alias (foreground blocking, fully compatible with the old flags).
- `telos init`'s `--agent` is renamed `--harness` (`--agent` kept as a hidden alias),
  and `--proxy-url` is renamed `--gateway-url`; new codex / openclaw / hermes installers added.

---

## [Unreleased] — 2026-05-14

This batch centers on two things: **a zero-intrusion integration path (HTTP reverse proxy)**, and **real cross-turn state accumulation**.
All mid-term goals are complete; the SDK transport and proxy paths are functionally equivalent (except SDK streaming, which is not yet complete).

### Added

- **`telos.output_filter`** — an RTK-style tool-result filtering layer (absorbing ideas from rtk-ai/rtk).
  - `TelosMode` four-state switch: `none` / `telos` / `rtk` / `both`, two independent booleans (telos prefix caching + rtk tool filtering).
  - `RtkFilter`: shells out to the `rtk` binary; `FallbackFilter`: a dependency-free pure-Python filter (consecutive duplicate-line folding, head/tail truncation, pytest summary), ensuring the switch still works when rtk is not installed.
  - `apply_filter(raw, flt) -> (new_raw, FilterStats)`: shortens large bash output in `messages[].tool_result` before it enters the TELOS pipeline. On failure it always degrades to pass-through.
  - proxy adds a `--mode {none,telos,rtk,both}` CLI switch; a single request can override it with the `X-Telos-Mode` header (the first request's value is sticky to that session).
  - proxy adds an `X-Telos-Compare-Group` header: a grouping label for comparison experiments.
- **comparison metadata in usage logs**: usage_log adds `mode` / `compare_group` / `tool_output_reduction` / `replay` fields for replay/showcase analysis. The live savings dashboard remains focused on aggregate savings rather than rendering A/B panels.
- **`telos.corpus`** — a session corpus. The proxy by default records each call's **original request** to `~/.telos/corpus/<session>.jsonl` (records requests only, not responses), for replay.
  - proxy adds `--corpus-dir` / `--no-record` switches.
- **`telos.replay` + the `telos replay` subcommand** — a record → replay comparison engine.
  - replays a given real session from the corpus once per mode: byte-identical turn sequences, `max_tokens=1` to measure only prefill/cache billing, and injects a unique system prefix per mode for cache isolation.
  - results are appended to usage_log with `compare_group` and `replay` metadata for downstream comparison tooling.
  - a controlled experiment that avoids the trajectory divergence noise of dual sessions; see [docs/replay-comparison.md](docs/replay-comparison.md) for the principle and limits.
  - CLI: `telos replay --list` / `telos replay --session <id> --modes none,telos,rtk,both`.
- **`telos.proxy`** — an aiohttp SSE-aware Anthropic reverse proxy (path B).
  - listens on `POST /v1/messages`, auto-detects the harness (openclaw / hermes), runs the TELOS pipeline, then forwards to Anthropic
  - transparently passes through non-`/v1/messages` paths
  - SSE streaming response support; side-channel parsing of `message_start` / `message_delta` to extract usage
  - LRU session registry (default 10000 cap), keyed by session_id
  - CLI: `python -m telos.proxy` / `telos proxy`
- **`telos.init`** — an agent config injector, same pattern as RTK.
  - `claude-code` installer: patches `env.ANTHROPIC_BASE_URL` in `~/.claude/settings.json`, preserves the user's original value, idempotent, can `--uninstall` to restore
  - `generic` installer: prints shell export instructions
  - CLI: `python -m telos.init --agent <name>` / `telos init --agent <name>`
- **the unified `telos` CLI**: dispatches the `proxy` / `init` subcommands, registered by `pyproject.toml` `[project.scripts]`.
- **`TelosAnthropicTransport`** ([scripts/telos_anthropic_transport.py](scripts/telos_anthropic_transport.py)) — the Anthropic end of the SDK transport (path A), symmetric with the existing `TelosOpenAITransport`.
  - `messages.create(**kwargs)` duck-typed interface
  - auto-detects the harness (hermes markers → hermes, otherwise openclaw); can be explicitly overridden with `harness_name=`
- **`BridgeSessionState`** (public dataclass, [bridge.py](bridge.py)) — a container for Bridge state persisted across turns. Wraps `RefPool` + `_SessionStats`.
  - `Bridge.__init__` adds an optional `session_state` parameter; when omitted it news one internally (behavior degrades to the old per-turn-independent mode)
  - `Bridge.session_state` property exposes the state to the caller
- **`Bridge.emit_with_plan() -> (wire, plan)`** — a two-tuple-returning version of `emit()`, internally bundling the full `_canonicalize_ir → assert_invariants → plan_marks → engine.emit` flow.
- **`RefPool.register_or_skip(slug, block) -> bool`** — idempotent registration, skipping a slug that already exists. Essential for sharing a RefPool across turns.
- **`ir.enforce_band_order(blocks)`** — stably sorts as `pin* → fold* → drop*`, a public helper function.
- **stable session-id derivation** ([proxy/server.py](proxy/server.py)): a content-derivation strategy `blake2b(api_key + system + tools + messages[0])`, keeping the same session_id across turns of a multi-turn conversation. Priority chain: `x-telos-session` header → `metadata.user_id` → derived hash.
- **`pyproject.toml`** — a standard PEP 517 package; `pip install -e .` makes `telos` globally importable.
- **observable accumulation fields**: both the proxy usage log and the transport trace log add a `cumulative.{cache_creation, real_requests_since_refresh, refpool_slugs}` block.
- **8 new test suites** (45 test functions):
  - [tests/test_proxy_pipeline.py](tests/test_proxy_pipeline.py) (5) — pure pipeline functions
  - [tests/test_proxy_server.py](tests/test_proxy_server.py) (6) — mock-upstream end to end
  - [tests/test_proxy_session_id.py](tests/test_proxy_session_id.py) (9) — session-id derivation stability
  - [tests/test_proxy_accumulation.py](tests/test_proxy_accumulation.py) (2) — HTTP-path multi-turn accumulation
  - [tests/test_bridge_session_state.py](tests/test_bridge_session_state.py) (6) — Bridge state sharing semantics
  - [tests/test_sdk_transport_accumulation.py](tests/test_sdk_transport_accumulation.py) (3) — SDK transport multi-turn accumulation
  - [tests/test_harness_multiblock.py](tests/test_harness_multiblock.py) (4) — §5 ordering regression
  - [tests/test_init_claude_code.py](tests/test_init_claude_code.py) (8) — installer idempotency / restore

### Fixed

- **harness §5 ordering violation** ([harness/openclaw.py](harness/openclaw.py), [harness/hermes.py](harness/hermes.py)): when a user message contains multiple content blocks, each block expands on its own into `(PIN, FOLD*, DROP*)`, and the old code concatenated them directly, producing `PIN, DROP, PIN, DROP, ...` which violates `pin* → fold* → drop*`. This is a bug guaranteed to trigger on real Claude Code traffic (multi-part content is the norm). Fix: a message-level `enforce_band_order` fallback sort.
- **canonicalize bug (present in both SDK transport and proxy)**: the old code, after `bridge.mark()`, produced wire directly with `engine.emit(snapshot_ir, plan)`, **skipping `_canonicalize_ir`** (tools order, payload key order). This left the multi-server / builtin / user interleaved order of the tool array unstable, silently breaking the prefix cache.
  - [proxy/pipeline.py](proxy/pipeline.py) switched to `bridge.emit_with_plan()`
  - [scripts/telos_anthropic_transport.py](scripts/telos_anthropic_transport.py) switched to `bridge.emit_with_plan()`
  - [scripts/telos_transport.py](scripts/telos_transport.py) keeps its custom chat-completions wire builder, but adds a `_canonicalize_ir(snapshot)` pass before feeding it
- **multi-turn Bridge state always zeroed**: both the proxy and the SDK transport newed a `Bridge` every time, so R8 cache_creation accumulation and the real_requests count were always 0, and the refresh adaptive gate never triggered. `BridgeSessionState` externalizes these two fields to session scope; the proxy holds them in an LRU registry keyed by session_id; the transport holds them in an instance field.
- **proxy 500 storm**: when the TELOS pipeline threw an exception the old code returned 500, and the Anthropic SDK crashed after 10 retries. Added a **passthrough fallback**: the default behavior degrades to raw pass-through, ensuring an optimization-layer failure does not break correctness. The `--strict` flag restores the 500 behavior (for testing/debugging).
- **proxy log noise**: on consecutive TELOS failures the old code printed a full traceback each time. New behavior: full traceback on the first failure, then a single-line WARNING for each subsequent one.

### Changed

- **`Bridge.__init__` signature extended**: adds an optional keyword-only parameter `session_state`. Defaults to `None`, newing one internally → fully backward-compatible, existing callers need no changes.
- **`PipelineResult` adds fields**: `cumulative_cache_creation`, `real_requests_since_refresh`. The old fields are unchanged.
- **`TelosOpenAITransport.__init__` adds an optional parameter** `session_state`.

### Removed

- The old `uuid4()` fallback in `proxy/server.py` has been replaced by content-derived session-id.
- `Bridge._refpool` and `Bridge._stats` instance attributes are internally changed to properties forwarding to `_state.refpool` / `_state.stats`. External access points are unchanged (old code keeps working).

---

## [0.1.0] — 2026-05-06 (initial public release)

- Python reference implementation of the TELOS protocol
- 3 harness plugins: `openclaw` / `hermes` / `telos`
- 5 engine adapters: `anthropic` / `openai` / `deepseek` / `vllm` / `sglang`
- `Bridge` 5 primitives: `place` / `pin` / `mark` / `fold` / `refresh`
- `BidirectionalEngineAdapter` mixin for vLLM / SGLang
- `TelosOpenAITransport` (OpenAI shape only, for telos / mini_swe_runner)
- `test_smoke.py` with 9 tests covering the R1–R8 fix points
