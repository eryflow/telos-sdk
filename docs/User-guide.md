# TELOS User Guide


## Installation

One-line install:

```bash
pip install telos-sdk
# or Homebrew (see packaging/, available after the tap is published):
# brew install telos-sdk
```

Development install from source:

```bash
cd /Users/george/Code/telos-sdk
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

[`pyproject.toml`](../pyproject.toml) maps the project root directory to the `telos` package. After install:

```bash
python -c "import telos; print(telos.__file__)"
# .../telos-sdk/__init__.py

telos --help
# usage: telos [<subcommand>] [...]
```

Requires Python ≥ 3.10. Depends on `anthropic ≥ 0.49`, `openai ≥ 1.72`, `aiohttp ≥ 3.10`.

### One-command integration (recommended)

```bash
telos init
```

`telos init` with no arguments will: auto-detect which harness CLIs are installed locally
(claude-code / codex / openclaw / hermes) → inject config pointing at the local gateway into each
→ start the gateway in the background → print the gateway and dashboard addresses.

Afterward:

```bash
telos              # pick a harness and enter its CLI (telos alias <harness> sets the default)
telos dashboard    # open the live dashboard in the browser
telos mode both    # switch the optimization gear, hot-reloading the running gateway
telos gateway status   # check the gateway's run status
```

---

## Path A: SDK Transport (in-code integration)

### 1 Anthropic client — Claude Code / Openclaw / Hermes / self-built agents

Replace `anthropic.Anthropic()` with `TelosAnthropicTransport`; all other `.messages.create()` calls stay the same:

```python
# before
import anthropic
client = anthropic.Anthropic()

# after
from telos.scripts.telos_anthropic_transport import TelosAnthropicTransport
client = TelosAnthropicTransport(
    session_id="my-agent-session",       # use the same id for the same conversation
    usage_log="logs/usage.jsonl",
    prompt_trace_log="logs/trace.jsonl",
    # harness_name="hermes",             # leave unset for auto-detect
)

# the call is completely unchanged
response = client.messages.create(
    model="claude-opus-4-7",
    max_tokens=8192,
    system=[{"type": "text", "text": "You are an engineer."}],
    tools=[...],
    messages=[...],
)
print(response.content[0].text)
```

Constructor parameters:

| Parameter | Default | Description |
|---|---|---|
| `api_key` | `$ANTHROPIC_API_KEY` | Anthropic API key |
| `base_url` | `None` (uses the SDK default) | can point at a local proxy for debugging |
| `session_id` | `"telos-session"` | keep the same id for the same conversation; the key for multi-turn cache accumulation |
| `harness_name` | `None` (auto-detect) | force `"openclaw"` / `"hermes"` |
| `engine_name` | `"anthropic"` | usually left alone |
| `usage_log` | `None` | jsonl path; one line appended per call (normalized usage) |
| `prompt_trace_log` | `None` | jsonl path; records IR layout / plan / accumulation state and other diagnostics |
| `session_state` | `None` (new'd internally) | pass explicitly when multiple transports share one conversation |

Harness auto-detection: system contains `<system-reminder>` or `<command-message>`, or a message has a `thinking` block → picks `hermes`; otherwise `openclaw`.

### 2 OpenAI client — telos / mini_swe_runner / self-built OpenAI-shape agents

```python
# before
from openai import OpenAI
client = OpenAI(base_url="https://openrouter.ai/api/v1")

# after
from telos.scripts.telos_transport import TelosOpenAITransport
client = TelosOpenAITransport(
    base_url="https://openrouter.ai/api/v1",
    session_id="telos-session",
    usage_log="logs/usage.jsonl",
    engine_name="deepseek",              # or "openai"
    harness_name="telos",                # fixed
)

response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[...],
    tools=[...],
)
```

### 3 Sharing one conversation across transports

If a conversation is handled by multiple transport instances (e.g. the client is rebuilt after a retry), pass `BridgeSessionState` in explicitly:

```python
from telos.bridge import BridgeSessionState
from telos.scripts.telos_anthropic_transport import TelosAnthropicTransport

shared = BridgeSessionState()
t1 = TelosAnthropicTransport(session_id="conv-1", session_state=shared)
# ... t1 errors and is destroyed ...
t2 = TelosAnthropicTransport(session_id="conv-1", session_state=shared)
# t2 can see the ref-pool and R8 counts accumulated by t1
```

---

## Path B: HTTP reverse proxy (zero-intrusion integration)

### Starting the gateway

`telos init` already starts the gateway in the background automatically. You can also manage it manually:

```bash
telos gateway start                       # background start (host/port/mode taken from ~/.telos/config.json)
telos gateway start --port 7171           # specify the port (and write it back as the new default)
telos gateway status                      # check the run status
telos gateway restart                     # restart
telos gateway stop                        # stop
telos gateway start --foreground          # run in the foreground, blocking (debugging / containers)
```

After a background start, the state is recorded in `~/.telos/gateway.json` and the log in `~/.telos/gateway.log`.

> The old `telos proxy ...` (foreground blocking) is still kept as a hidden alias, fully flag-compatible.

After starting, the log (`~/.telos/gateway.log`) outputs:

```
TELOS gateway listening on http://127.0.0.1:7171 → https://api.anthropic.com
usage log → /Users/.../usage.jsonl
```

The gateway accepts all Anthropic protocol paths; `/v1/messages` is rewritten by TELOS, everything else is passed through unchanged.

> **harness auto-detection**: the gateway automatically determines which harness each request belongs to, with no manual specification needed.
> Besides the main conversation, Claude Code also sends auxiliary requests via Haiku (conversation-title generation, new-topic detection, etc.).
> These requests have no tools and no `<system-reminder>` tag — the gateway identifies them via the HTTP `User-Agent`
> header (`claude-cli/...`) and remembers the identification result per client, so auxiliary requests are also
> correctly attributed to Claude Code, and won't be mistakenly shown as `openclaw` on the dashboard.

### Integrating Claude Code

`telos init` automatically integrates the harnesses it detects. You can also integrate Claude Code only:

```bash
telos init --harness claude-code
```

It writes into the `env` field of `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:7171",
    "__telos_installed": true
  }
}
```

Afterward any process that starts Claude Code automatically uses the local gateway. **No change to the npm package**, **no change to PATH**, and **`npm update` won't lose it**.

To undo / check status:

```bash
telos init --harness claude-code --uninstall   # restore the state before install
telos init --harness claude-code --status      # view only, don't modify files
```

### Integrating other clients

`telos init --harness codex` adds a `model_provider = "telos"` custom provider
to `~/.codex/config.toml`, pointing Codex at
`http://127.0.0.1:7171/upstreams/openai/v1`. Codex currently uses the OpenAI
Responses API by default; the gateway routes that path as passthrough.
OpenAI ChatCompletions-compatible traffic through the same gateway path is
TELOS-processed.

openclaw / hermes patch their own provider configs. Any client that respects
`ANTHROPIC_BASE_URL` or `OPENAI_BASE_URL` can also use
`telos init --harness generic` to get a set of manual export instructions.

### Full CLI reference

```
telos                        bare command: pick a harness and enter its CLI
telos <harness>              enter a harness directly (claude-code / codex / openclaw / hermes)
telos alias <harness>        set the harness that bare telos enters by default

telos init [options]
  --harness {claude-code,codex,openclaw,hermes,generic}
                       operate on the specified harness only (default: auto-detect all)
  --gateway-url URL    gateway address (default taken from ~/.telos/config.json)
  --uninstall          restore the state before install
  --status             view only, don't modify files
  --no-gateway         only inject config, don't start the gateway automatically

telos gateway [start|stop|status|restart] [options]
  --host HOST          listen address (default 127.0.0.1)
  --port PORT          listen port (default 7171)
  --mode {none,telos,rtk,both}   default optimization gear
  --usage-log PATH     append one jsonl line per call
  --foreground         run in the foreground, blocking (no backgrounding)

telos mode [none|telos|rtk|both]   switch the optimization gear; hot-reloads the running gateway and persists it
telos dashboard [--static] [--no-open]   open the dashboard in the browser

telos proxy [options]        hidden alias: runs the gateway in the foreground, blocking; fully compatible with the old flags
```

For `telos gateway` / `telos init`, any host/port/mode not passed defaults to `~/.telos/config.json`;
explicitly passed values are written back as the new default.

---

## Multi-turn state accumulation (a key capability)

The ref-pool persistence and the R8 adaptive refresh mentioned in §4 / §6 of the TELOS protocol design doc both depend on **cross-turn state accumulation**. This section explains the mechanism and how to observe it.

### Where the state lives

```python
@dataclass
class BridgeSessionState:
    refpool: RefPool          # ref-pool slug registry (kept across turns once frozen, kept across folds too)
    stats: _SessionStats      # cumulative_cache_creation + real_requests_since_refresh
```

### Held automatically on Path A

A `TelosAnthropicTransport` / `TelosOpenAITransport` instance = one session. `__init__` creates a `BridgeSessionState` internally; each `_do_create` passes it to `Bridge`, and when the response returns `bridge.absorb_usage(...)` accumulates the cache_creation.

Access: `transport.session_state.stats.cumulative_cache_creation`.

### Held automatically on Path B, keyed by session-id

Inside the proxy, `_SessionRegistry` (an OrderedDict LRU, default 10000) holds the state keyed by session_id. session_id derivation priority:

1. the `x-telos-session` HTTP header (explicit override)
2. `metadata.user_id` (an Anthropic SDK built-in field)
3. `blake2b(api_key + system + tools + messages[0])` → `telos-<16 hex>`

The semantics of the derivation rule:
- the N turns of the same conversation (only appending to the tail of `messages[]`) → the same session_id ✓
- a different initial prompt (`messages[0]` changes) → a different session_id ✓
- two users with different API keys → different session_ids ✓

Once the LRU cap is exceeded the oldest session is evicted, with an INFO log emitted.

### Observing accumulation

usage_log adds a `cumulative` block per line:

```json
{
  "session_id": "telos-46bbb9d3d3df581e",
  "call_index": 4,
  "harness": "openclaw",
  "normalized": {"raw_input": 50, "cache_read": 6500, "cache_write": 0, "output": 5},
  "cumulative": {
    "cache_creation": 6500,
    "real_requests_since_refresh": 4,
    "refpool_slugs": ["system-doc-1"]
  }
}
```

`cache_creation` increasing monotonically shows accumulation is working; the `refpool_slugs` array should not grow repeatedly across turns (the same document should not be registered again and again).

### Disabling accumulation (a fresh Bridge per turn)

Not passing `session_state`, or restarting the proxy, makes the behavior fall back to newing a fresh state per turn. This was the default behavior before 1.0, and it does not break wire bytes.
