# Developer Dashboard Metrics Reference

Entry point: `GET /developer` (HTML) or `/developer.json` (JSON).
Source: rendered by [scripts/build_developer_page.py](../scripts/build_developer_page.py),
with [proxy/inspector.py](../proxy/inspector.py) accumulating the in-memory state.

> Unlike the user-facing Savings Dashboard: this page **only shows the live in-memory state**,
> re-rendering from `SessionInspector` on every GET; it is lost on process restart.

---

## 1. Overview (session list)

Source: `SessionInspector.items()`, sorted by `last_seen` descending.

| Column | Meaning | Source |
|---|---|---|
| `session_id` | session identifier; click to enter the detail view | `SessionInspectorEntry.session_id` |
| `model` | the `ir.hints.model` of this session's most recent request | `entry.last_model` |
| `harness` | the harness identified for this session's most recent request (`hermes` / `openclaw` / `passthrough`) | `entry.last_harness` (see `_detect_harness`) |
| `calls` | the number of calls observed (capped at `INSPECTOR_HISTORY=25`; beyond that the sliding window drops the oldest) | `len(entry.calls)` |
| `tool calls` | the total number of `tool_use` blocks the assistant initiated across all calls | `sum(s.invocations)` |
| `distinct tools` | the number of unique tool names | `len(entry.tools_stat)` |
| `tool_result chars` | the cumulative character count of all `tool_result` content (response body volume) | `entry.tool_result_chars_total` |
| `last seen` | the relative time since the last call | `now - entry.last_seen` |

---

## 2. Session Detail · KPI strip

| KPI | Meaning |
|---|---|
| `model` | the model name of this session's most recent request |
| `harness` | the harness identified most recently |
| `calls seen` | the number of calls retained in memory (≤ `INSPECTOR_HISTORY`) |
| `plan slots` | the list of cache-breakpoint slot names actually placed by the most recent `EmitPlan` (see below) |
| `last raw_input` | the most recent response's `usage.input_tokens` (prompt tokens that missed the cache) |
| `last cache_read` | the most recent response's `usage.cache_read_input_tokens` (prompt tokens that hit the cache) |
| `last cache_write` | the most recent response's `usage.cache_creation_input_tokens` (prompt tokens newly written to the cache this time) |
| `last output` | the most recent response's `usage.output_tokens` |

### Plan slot names (BP-*)

From the `plan_marks` in [engine/anthropic.py:41-96](../engine/anthropic.py#L41-L96). Anthropic
allows only 4 `cache_control` breakpoints, cut by the priority in the table below:

| Slot | Position | TTL | Trigger condition |
|---|---|---|---|
| **BP-T** | the last block of the `tools` segment | `1h` | the request carries `tools` |
| **BP-S** | the last **PIN** block of the `system` segment | `1h` | the system segment has a PIN block (excluding the ref-pool) |
| **BP-R** | the last **FOLD** block of the `system` segment | `1h` | the system segment has a FOLD block (typically the end of the ref-pool) |
| **BP-mid** | the last non-DROP block within `messages[len-19]` | `5m` | `len(messages) ≥ 19` (fixes R2, ensuring it is still within the 20-block lookback window next time) |
| **BP-X** | the last non-DROP block within the last message | `5m` | a non-empty message exists |

Priority `BP-T > BP-S > BP-R > BP-mid > BP-X`. The physical order is guaranteed by
`tools → system → messages`; the long TTL (1h) necessarily precedes the short one (5m), which holds naturally from the segment order.

---

## 3. "Prompt regions · pin·fold·drop chars per segment"

Draws one stacked bar for each of the three segments `tools` / `system` / `messages`:
- **P (PIN)**: `#d29922` gold, long-term stable content (system prompt, tool defs, the main body of the user's question)
- **F (FOLD)**: `#58a6ff` blue, ref-pool / previous-turn tool_result / assistant history responses
- **D (DROP)**: `#7d8590` gray, the per-turn-changing envelope (`<system-reminder>`, `<environment_info>`, timestamps)

Each segment title shows the segment's total character count + the (chars, blocks) numbers for each of the three bands.
The delta comes from [scripts/build_developer_page.py:159-164](../scripts/build_developer_page.py#L159-L164):
a red `+N` means this turn is longer than the last; a green `−N` means it shrank.

---

## 4. "Recent calls" (in reverse order of call time)

Each row is a snapshot of one call, source: `entry.calls` (the most recent 25 retained, [INSPECTOR_HISTORY](../proxy/inspector.py#L21)).

| Column | Meaning |
|---|---|
| `#` | call index (monotonically increasing since the session started) |
| `lat` | call latency (seconds) |
| `raw_in` / `cache_read` / `cache_write` / `output` | the `usage` quadruple of this call's response (normalized) |
| `tools chars · Δ` | the total chars of this turn's `tools` segment + the difference from the last turn |
| `system chars · Δ` | same as above, the `system` segment |
| `messages chars · Δ` | same as above, the `messages` segment |
| `plan slots` | the 4 BP slot names actually placed this turn (see §2) |
| `uses` | the number of `tool_use` blocks in this turn's assistant response (assistant → tool) |
| `results` | the number of `tool_result` blocks in this turn's user message (tool → assistant) |

> `uses` and `results` are **off by one turn** in time: the `tool_use` the assistant initiates in turn N
> generally has to wait until the turn N+1 user to have the `tool_result` sent back.

---

## 5. "Latest IR · per-message blocks (band · kind · chars)"

A **snapshot of TelosIR.messages** for the most recent request, laid out in message index order. For each message,
the left side is `msg[index]`, the middle is `role` (user / assistant), and the right side is the block pill sequence.

Each pill looks like `P·text 1,234c [openclaw/user-query]`:

| Field | Meaning |
|---|---|
| color / first letter | band: **P** gold = PIN · **F** blue = FOLD · **D** gray = DROP |
| `kind` | block type: `text` / `tool_use` / `tool_result` / `thinking` / `image` / `tool_def` |
| `Nc` | the block payload character count |
| gray tail | `source_tag` or `ref_slug`, recording which harness and which slice of logic carved out this block |

The prefix of `source_tag` is the harness name (`openclaw/...` / `hermes/...` / `harness/...`),
which can be used to check whether the determination in [§7](#7-source_tag-reference-table) is correct.

---

## 6. "Tool calls in this session"

Source: `SessionInspectorEntry.tools_stat` ([proxy/inspector.py:26-53](../proxy/inspector.py#L26-L53)).
Each `tool_use` of the assistant → `absorb_use(args_chars)`; each `tool_result` of the user →
looks up the tool name in reverse via `tool_use_id` → `absorb_result(result_chars)`.

| Column | Meaning |
|---|---|
| `tool name` | the tool name |
| `invocations` | the number of calls (the number of assistant → tool requests) |
| `args chars total` | the cumulative input-argument JSON character count |
| `args avg` | `args_chars_total / invocations` |
| `args last` | the most recent input-argument character count |
| `result chars total` | the cumulative returned-content character count (all `tool_result` content summed) |
| `result avg` | `result_chars_total / invocations` (the denominator uses invocations rather than the result count; in a few cases the result has not arrived) |
| `result max` | the maximum character count of a single return (catches "output explosion" tools) |
| `result last` | the most recent return character count |

> **Note**: `tool_use` and `tool_result` are associated via `tool_use_id`, and the reverse-lookup window is `entry.calls`
> (≤ 25). If a tool's result arrives 25 calls later than its corresponding use, it cannot be attributed to the correct name and
> is only counted into `tool_result_chars_total` (visible in the Overview column).

---

## 7. "Last API usage · cache-related fields (raw)"

JSON-dumps the following raw fields from `entry.last_usage_raw` directly for you to see:
`input_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens` /
`output_tokens` / `cache_creation`.

The most informative are `cache_creation.ephemeral_5m_input_tokens` and
`ephemeral_1h_input_tokens`: used to verify whether the TTL allocation of the BP slots took effect.

---

## 7. source_tag reference table

The prefix of `source_tag` = the harness name; the suffix describes the slice source.

| Harness | Segment | Common tag |
|---|---|---|
| `openclaw` | tools | `openclaw/tools` |
| `openclaw` | system | `openclaw/system-large` / `openclaw/system-ref-stub` / `openclaw/system` |
| `openclaw` | messages | `openclaw/tool-result` / `openclaw/assistant-text` / `openclaw/assistant-tool-use` / `openclaw/other` |
| `hermes`   | tools | `hermes/tools` |
| `hermes`   | system | `hermes/system` / `hermes/file-block` (`<file path=…>` ref-pool blocks >2KB) |
| `hermes`   | messages | `hermes/tool-result` / `hermes/assistant-text` / `hermes/assistant-tool-use` / `hermes/thinking` / `hermes/other` |
| shared | user message splitting | `harness/user-query` (PIN) · `harness/system_reminder` / `command_message` / `command_name` / `env_info` / `current_time` (DROP) · `harness/prev_result` (FOLD) |

If you expect Claude Code (hermes) but see an `openclaw/*` prefix, it is most likely that
`_detect_harness` misjudged (see the "Known issues" section).

---

## Known issues

- `_detect_harness` only checks whether the `system` segment text contains `<system-reminder>` / `<command-message>`,
  but Claude Code actually injects these tags into the **user message**, so most hermes traffic
  gets identified as openclaw. See the to-fix list of the detection function in `scripts/telos_anthropic_transport.py` for details.
