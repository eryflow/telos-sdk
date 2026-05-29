# TELOS · DevCon Showcase Plan

> **Audience**: Agent / LLM application developers (engineers building production Agents on Anthropic / OpenAI / DeepSeek / vLLM / SGLang)
> **Core value anchors**: (1) **Save dollars** — absolute-dollar savings from KV-cache (2) **Cross-vendor portability** — one IR runs on five engines
> **Format**: Main-stage Live Demo + Booth Hands-on area
> **Core take-away**: *Context is yours — not rented. Pin down what's stable so the cache actually hits you.*

---

## 0. One-Pager — Reading This Section Is Enough

| Dimension | Content |
|---|---|
| **Main tagline** | **`$0.36 → $0.03` · same conversation, 92.7% saved.** (Estimated against Claude Opus 4.7 list price; source `showcase/usage.jsonl`) |
| **Sub-tagline** | One IR, Five Engines. Stop being a tenant in someone else's agent. |
| **Booth key visual** | Three-color stele (PIN / FOLD / DROP) + live dollar-savings counter |
| **Recommended booth size** | ≥ 3m × 2m, fits 2 demo machines + 1 large display |
| **On-site staff** | 2 in rotation (1 presenter + 1 hands-on Q&A); add a 3rd at peak hours |
| **Core deliverables** | 5 showcases (A–E), 1 large-display dashboard, 1 one-page cheatsheet, 1 USB experience pack |
| **Offline fallback** | Every Live Demo runs fully offline (`telos showcase` ships with replay data) |

---

## 1. Five Showcases at a Glance

| ID | Name | Duration | Format | Audience density | Core value |
|---|---|---|---|---|---|
| **A** | One IR, Five Engines | 5' | Main-stage Live | 50–200 | Cross-vendor portability |
| **B** | $0.36 → $0.03 Cash Counter | 3' | Booth large-display Live + loop playback | Constant draw | Save dollars |
| **C** | Break the Stele | 5–10' self-serve | Booth Hands-on | 1–2 per machine | Protocol spec + tactile feel |
| **D** | Claude Code 5-Minute Quick Integration | 5' | Main-stage Live | 50–200 | Zero-intrusion + save dollars |
| **E** | Cooperative Fold (advanced) | 3' | Main-stage / breakout room | 30–50 | Bidirectional engine capability |

> Suggested main-stage order: **A → D → B lightning round → E**; booth 24h loop: **B large display + C self-serve stations**.

---

## 2. Showcase A · One IR, Five Engines

### 2.1 One Sentence

*The same OpenClaw request is parsed exactly once into a `TelosIR`, then fed unmodified to 5 engine adapters — Anthropic / OpenAI / DeepSeek / vLLM / SGLang. Capability differences are handled by **deterministic adapter downgrades** — not silent loss; the semantics are preserved.*

### 2.2 Goal

Convince the audience: **an Agent written on Anthropic today can move to self-hosted vLLM tomorrow without losing anything.**

### 2.3 Live Script (5 minutes)

| Timestamp | Screen | Talking point |
|---|---|---|
| 0:00 | Open a terminal, black background, white text | "We're not building a prompt framework — we're building a portable protocol for Agent context." |
| 0:20 | Hit enter on `python -m telos.demo` | "One request, five engines, zero modifications." |
| 0:40 | Screen shows the `engine = anthropic` section, point at the BP slots | "Anthropic gets explicit breakpoints — all 4 BPs are used." |
| 1:30 | Flip to the `engine = openai` section | "OpenAI has no BP and **deterministically downgrades** to `prompt_cache_key` — the same IR automatically falls through to this capability." |
| 2:30 | Flip to the `engine = deepseek` section | "DeepSeek has neither — but the IR is still legal, the prefix is still byte-stable, and the cache still hits." |
| 3:20 | Flip to the `engine = vllm` / `sglang` section, point at `cache_policy` | "Self-hosted engines unlock more capabilities than closed-source APIs: probe / fold / cache hierarchy." |
| 4:00 | Highlight the `cooperative_fold` output | "On vLLM/SGLang the prefix KV is not recomputed — only the summary tail is. This is something closed-source APIs cannot do." |
| 4:40 | Wrap | "**Context is your asset, not something rented.**" |

### 2.4 Key Numbers

- **1 IR** → **5 engines** → **0 lines of prompt rewriting**
- Capability matrix (screenshot from README): BP / prewarm / routing_key / probe / fold — five rows by five columns

### 2.5 Risks & Fallbacks

- **Network risk**: demo.py is entirely local with no API calls ✅
- **Output-too-long blow-up**: bump terminal font to 18pt; pre-stage `python -m telos.demo > /tmp/demo.out` as a backup so you can scroll
- **Q&A prep**: R1–R8 (see §7), especially R1 (OpenAI quota) and R2 (mid-segment anchor)

---

## 3. Showcase B · `$0.36 → $0.03` Cash Counter

### 3.1 One Sentence

*The same 6-turn conversation, 4 switch combinations each run once — byte-identical request sequence, the only variable is the switch itself. `none` burns $0.36, `both` only costs $0.03. **The savings are in absolute dollars. Ratios can be gamed by shrinking the denominator; absolute dollars can't.***

### 3.2 Format

**Booth large display in loop + main-stage 3-minute lightning round**. Large-display content:

```
┌─────────────────────────────────────────────────────────┐
│  TELOS · Live Savings Counter                            │
│  ───────────────────────────────────────────────────     │
│  Mode    raw_in   cache_read   est. cost    saved        │
│  none    24,151        0       $0.3623     baseline     │
│  rtk     22,841        0       $0.3426     -5.4%        │
│  telos        0   18,701       $0.0281     -92.3%       │
│  both         0   17,719       $0.0266     -92.7%       │
│  ───────────────────────────────────────────────────     │
│  $  Saved per 6-turn session:   $0.336                  │
│  $  Saved per 1k sessions:      $336                    │
└─────────────────────────────────────────────────────────┘
                  (numbers refresh every 30s from live replay)
```

### 3.3 Live Commands (3 minutes)

```bash
# Use the real-capture version (already included in showcase/replay_responses.json, marked source=real)
telos showcase --pace 1.5

# Or just keep it running at the booth (auto-loop):
while true; do telos showcase --pace 2 --quiet; sleep 30; done

# Or, more "engineer-flavored" — open the dashboard directly:
open showcase/dashboard.html
```

### 3.4 Pacing

| Timestamp | What to say |
|---|---|
| 0:00–0:30 | "How do we prove the savings? Not an estimate — record a real session, replay 4 times, byte-identical." |
| 0:30–1:30 | Screen scrolls through the 4-mode usage output, point at `cache_read` going from 0 up to 17,719 |
| 1:30–2:30 | Open dashboard.html; on the "saved $" board the four modes sit side-by-side; point at the $0.336 row |
| 2:30–3:00 | "Replay is a controlled experiment — CI-friendly, reproducible. Ratios can be gamed; absolute dollars cannot." |

### 3.5 Source of the Real Numbers (to head off skepticism)

The numbers come from `showcase/usage.jsonl`, an aggregation of values reported by Anthropic across 6 real-captured turns:

| Mode | raw_input | cache_read | Estimated $ ($15/M input + $1.5/M cache_read) |
|---|---|---|---|
| none | 24,151 | 0 | **$0.3623** |
| rtk | 22,841 | 0 | $0.3426 |
| telos | 0 | 18,701 | $0.0281 |
| both | 0 | 17,719 | **$0.0266** |

> Data comes from `showcase/usage.jsonl` (6 real-captured turns, aggregated by the mode field).
> **Saves $0.336 per 6-turn session · $336 per 1k sessions · -92.7%**. If you re-capture before rehearsal, remember to recompute this table.

> The narrator must call out: "Estimated against Anthropic's published list price; **actual billing may differ slightly**. The dashboard shows tokens, not dollars — the dollar figure is derived from public pricing × tokens."

### 3.6 Risks & Fallbacks

- **If asked "is this number real?"**: open `showcase/replay_responses.json` on the spot and point at `_meta.source = "real"`
- **If asked "why doesn't rtk alone save money?"**: rtk only compresses tool_result volume, it does not stabilize the prefix; the prefix is still recomputed every turn → see playbook §5
- **If the large display dies**: keep a PDF / single-file HTML backup, copy-able straight from a USB stick

---

## 4. Showcase C · Break the Stele (Hands-on Area)

### 4.1 One Sentence

*The stele has just one hard rule: within each segment, `PIN → FOLD → DROP`. Try it yourself — move FOLD before PIN and watch the protocol bite back.*

### 4.2 Format

**2 demo machines at the booth** (a MacBook Air is fine); each has a QR code + one-page cheatsheet on the side. Visitors walk up at will.

### 4.3 Primary Flow (5–10 minutes self-serve)

Guidance text (stuck on the side of the screen):

```
┌─────────────────────────────────────┐
│  Try it in 3 minutes:                 │
│                                     │
│  (1) In the terminal: telos showcase --interactive
│  (2) Pick [3] — intentionally put FOLD before PIN
│      watch the protocol raise TelosInvariantError
│  (3) Pick [2] — change expected_turns (2/20/60)
│      watch the mark plan adapt
│  (4) Pick [4] — 4-mode replay A/B
│  (5) Pick [5] — generate and open the dashboard
│                                     │
│  Won't hang; fully offline.          │
└─────────────────────────────────────┘
```

### 4.4 Key Talking Points for the Attendant (avg. 2 minutes per hands-on visitor)

- **Menu [3] is the most visceral**: the protocol has exactly one hard constraint; violations tell you precisely which block is wrong
- **Menu [2] demonstrates adaptivity**: at `expected_turns=2` the mark plan only places 2 anchors; at 60 it enables mid-segment rolling anchors (R2)
- **Menu [4] is the small-screen version of B**: the visitor sees the 4-mode numerical differences themselves

### 4.5 Materials Checklist

- [ ] 2 demo machines, repo already `pip install -e .`, offline-verified
- [ ] Cheatsheet stuck to the side of the screen (the box in 4.3)
- [ ] USB experience pack stacks on the table (containing repo zip + cheatsheet PDF + QR code to the video)
- [ ] QR codes: (1) GitHub repo (2) Quickstart docs (3) lead-capture entry
- [ ] Keep an asciinema cast `showcase/demo.cast` handy — plays on the demo machine even fully offline

### 4.6 KPIs

- Hands-on visitors: ≥ 30 / day
- Average dwell: ≥ 4 minutes
- Lead-capture rate: ≥ 25%

---

## 5. Showcase D · Claude Code 5-Minute Quick Integration

### 5.1 One Sentence

*Take an npm-globally installed Claude Code, **without changing a single line of code**, and within 5 minutes route it through the TELOS cache — then watch cache_read climb in real time on the dashboard.*

### 5.2 Goal

Turn "save money" from an abstract number into a "I can install this right now" actionable demo. The audience is people currently using Claude Code / Cursor / Gemini CLI.

### 5.3 Live Script (5 minutes)

| Timestamp | Command / Screen | Talking point |
|---|---|---|
| 0:00 | Open two terminals | "Install on the left, watch the cache on the right." |
| 0:20 | `telos gateway start --usage-log ~/.telos/usage.jsonl` | "Start a reverse proxy on 127.0.0.1." |
| 0:50 | `telos init --agent claude-code` | "One command, patches the env field in `~/.claude/settings.json` — **does not touch the npm package; npm update won't lose the config.**" |
| 1:30 | `telos init --agent claude-code --status` | "Confirm status: enabled." |
| 2:00 | Left: `claude` starts a coding task (pre-recorded or live) | "Usage is completely unchanged." |
| 3:00 | Right: `jq -c '{call: .call_index, cache_read: .normalized.cache_read}' < ~/.telos/usage.jsonl` | "Watch cache_read climb line by line. From the 4th turn on, each turn hits 6000+ tokens." |
| 4:00 | Browser opens `http://127.0.0.1:7171/__telos/dashboard` | "Live dashboard — PIN/FOLD/DROP distribution at a glance." |
| 4:40 | `telos uninstall --harness claude-code` | "Can be cleanly uninstalled at any time, restoring the pre-install state exactly." |

### 5.4 Key Numbers (visible on stage)

- **Integration time**: ≤ 60 seconds (1 init command)
- **Footprint**: only the `env` field of `~/.claude/settings.json`
- **Reversibility**: `telos uninstall` restores exactly

### 5.5 Risks & Fallbacks

- **No on-site network**: pre-record a 30-second video "claude runs 6 turns → cache_read climbs" and play it when offline
- **`claude` really needs to hit Anthropic**: use a cheap short prompt ("list the current directory"); billing is controlled
- **If asked "what if the proxy dies?"**: non-strict mode (default) automatically passes through; the agent is unaffected

---

## 6. Showcase E · Cooperative Fold (Advanced Track)

### 6.1 One Sentence

*With closed-source APIs, fold is a client-side rewrite, and the server has to re-prefill the entire segment every time; on vLLM/SGLang, `cooperative_fold` lets the server **keep the prefix KV in place** and only recompute the summary tail.*

### 6.2 Audience

vLLM / SGLang users, engineers working on inference infrastructure. Expect a smaller audience (30–50), but a precise one.

### 6.3 Live Script (3 minutes)

```bash
# In the terminal, run the vLLM / SGLang parts of demo.py
python -c "
from telos import Bridge, load_engine, load_harness
from telos.demo import RAW_REQUEST

ir = load_harness('openclaw').parse(RAW_REQUEST, session_id='fold-demo', engine='sglang', model='deepseek-ai/DeepSeek-V3', expected_turns=20)
b = Bridge(ir, load_engine('sglang'))

# 1) probe
probe = b.probe_cache()
print(f'probe → hit={probe.hit} cached={probe.cached_token_count} tier={probe.tier}')

# 2) cooperative_fold
ctrl = b.cooperative_fold(message_range=(1,3), summary='<prev turns folded>')
print(f'server-side cache_control fragment:')
import json; print(json.dumps(ctrl, indent=2))
"
```

### 6.4 Pacing

- **0:00** "Closed-source APIs let you do fold — but the server doesn't know you folded. On the next request, that prefix is brand-new to the server, and the KV is fully recomputed."
- **0:40** Screen: `probe_cache` output — "First see what the server-side cache already has, then decide how to change it."
- **1:30** Screen: `cooperative_fold` output; point at the `cache_policy`/`cache_control` fragment — "The client tells the server: leave the prefix alone, just fold this segment."
- **2:30** Wrap: "This is **bidirectional engine capability** — only self-hosted engines have it. **For self-hosted engines, TELOS is an upgrade, not a downgrade.**"

### 6.5 When to Run It

- Option 1: as an extension of A (after A finishes, invite interested attendees to the breakout room)
- Option 2: standalone slot on day 2 afternoon (inference engineers' track)

---

## 7. Q&A Prep · Protocol Risks R1–R8

> All already landed in `docs/showcase-runbook.md §5`. Each on-site presenter carries a printed copy.

| ID | Likely question | One-sentence answer | Code location |
|---|---|---|---|
| R1 | Doesn't OpenAI `prompt_cache_key` only expand slots at ≥15 RPM? What about low traffic? | We set `KEY_RPM_SOFT_CAP=12` and auto-shard | `engine/openai.py` |
| R2 | Anthropic only has 4 BPs — how do you cover dozens of turns? | Mid-segment rolling anchors `_MID_ANCHOR_STRIDE=19`, adaptive | `engine/anthropic.py` |
| R3 | Child Agent and parent Agent share session_id — won't they cross-contaminate? | Child IR is parsed independently (hermes) | `harness/hermes.py` |
| R4 | What if a mark slot lands inside a folded region after fold? | `fold()` forces a re-run of `mark()` | `bridge.py` |
| R5 | Won't unstable tool-field ordering break the cache? | `_canonicalize_ir()` pins key/tool ordering | `bridge.py` |
| R6 | Does the thinking block get lost across non-tool_result calls? | Adapters have a `thinking_preserved_across_non_tool_result` flag | `engine/base.py` |
| R7 | When BP candidates exceed 4, who gets kept? | `plan_marks` priority + truncation | `engine/anthropic.py` |
| R8 | Could refresh end up maxing out the quota? | `REFRESH_THRESHOLD=11`, adaptive gating | `bridge.py` |

**The two most-asked questions whose answers are not in R1–R8:**

- **"Can replay measure end-to-end task cost?"** → No. Replay pins down the trajectory and measures "the cost of the same conversation under different encodings". For end-to-end measurement, use dual sessions (see `docs/replay-comparison.md §4`).
- **"Are the numbers real?"** → Open `showcase/replay_responses.json`; `_meta.source` is either `real` or `synthetic`. Today we're on `real`.

---

## 8. On-Site Flow (suggested)

```
                    Main stage (2x 25-min keynotes per day)
                     ┌─────────────────────────┐
                     │   25min Keynote          │
                     │   = A (5) + D (5) + B    │
                     │     lightning (3) + E (5)│
                     │     + Q&A (7)            │
                     └─────────────────────────┘
                              │
              (post-talk traffic) ↓
                     ┌─────────────────────────┐
                     │  TELOS booth 3m × 2m      │
                     │                          │
                     │  [Large display · B cash counter]    │ ← Constant draw
                     │  [Table · C Hands-on 1]      │ ← Engineers try it
                     │  [Table · C Hands-on 2]      │
                     │  [Standee · 3-color stele visual]    │
                     │  [Under table · USB experience-pack pile]    │
                     └─────────────────────────┘
```

### 8.1 Staffing Roster

| Role | Count | Responsibilities |
|---|---|---|
| Main-stage presenter | 1 | Lead presenter for A / D / B / E, ready on R1–R8 |
| Booth presenter | 1 | Narrate at the B large display + funnel traffic to Hands-on |
| Hands-on Q&A | 1 | Stand by C, accompany visitors, answer questions on the spot |
| Firefighter / recorder | 1 (peak hours) | Handle equipment failures, capture noteworthy Q&A |

### 8.2 Daily Rhythm

| Time slot | Main stage | Booth |
|---|---|---|
| 09:00–10:00 | — | C self-serve open + B large display looping |
| 10:00–10:25 | **Keynote 1** (A+D+B+E) | — |
| 10:25–12:00 | — | Peak hours: 3-person rotation |
| 12:00–13:30 | Lunch break | B large display keeps looping (unattended OK) |
| 13:30–14:00 | — | C self-serve open |
| 14:00–14:25 | **Keynote 2** | — |
| 14:25–18:00 | — | Peak hours + wind-down |

---

## 9. Materials Checklist (procurement / prep)

### 9.1 Hardware

- [ ] 2 demo laptops (macOS / Linux both fine), 16GB+ RAM
- [ ] 1 large display ≥ 32" + HDMI adapters
- [ ] Presenter clickers ×2
- [ ] Power strips ×2
- [ ] Backup power / UPS (in case of show-floor power outages)

### 9.2 Software / Data

- [ ] Repo `pip install -e .` verified runnable on each demo machine
- [ ] All three entry points work: `telos showcase` / `telos showcase --interactive` / `telos showcase --cast`
- [ ] `showcase/replay_responses.json` is the `_meta.source = "real"` version
- [ ] `showcase/dashboard.html` opens offline by double-clicking
- [ ] Keep an ASCII text dump of `python -m telos.demo` output (in case the terminal blows up and you have to scroll)
- [ ] Record a 30s video "claude + telos gateway → cache_read climbs" (Showcase D fallback)

### 9.3 Printed Materials

- [ ] **One-page cheatsheet** (A4 double-sided) — front: 3-color stele visual + 30s quickstart; back: thumbnails of the 5 showcases + QR codes
- [ ] **Standee key visual**: 3-color stele + main tagline `$0.36 → $0.03 · same conversation, 92.7% saved`
- [ ] **Table signs**: guidance text for each Hands-on machine (§4.3)
- [ ] **Stickers**: TELOS 3-color logo (PIN-blue / FOLD-yellow / DROP-red), ≥ 500 pcs

### 9.4 Digital Materials

- [ ] USB experience pack (repo zip + cheatsheet PDF + video) ×100
- [ ] QR codes: (1) GitHub repo (2) Quickstart docs (3) lead-capture survey (4) video replay
- [ ] Wrap-up email template (sent within 48 hours to lead-captured attendees)

---

## 10. On-Site Offline Rehearsal Checklist (must be completed 24 hours before departure)

> Mirrors `docs/showcase-runbook.md §4`, with one extra item for the large-display check.

- [ ] **Wi-Fi off**, `telos showcase` runs end-to-end, the dashboard opens in the browser at the end
- [ ] **Wi-Fi off**, `telos showcase --interactive` — click through all five menu items
- [ ] `showcase/replay_responses.json` is the real-captured version (`_meta.source == "real"`)
- [ ] `showcase/dashboard.html` opens offline by double-clicking; the "saved $" board shows 4 mode sessions
- [ ] The cast file `asciinema play showcase/demo.cast` plays back fine
- [ ] The demo machine has `pip install -e .` (editable), and the `telos` command works
- [ ] **Large-display loop**: `while true; do telos showcase --pace 2 --quiet; sleep 30; done` runs for ≥ 30 minutes without crashing
- [ ] **Showcase D video fallback**: MP4 is playable from the USB stick
- [ ] **Slides fallback**: in case every terminal blows up, there is a PDF that tells the whole story

---

## 11. Success Metrics (KPI)

| Dimension | Metric | Target |
|---|---|---|
| **Reach** | Cumulative main-stage audience | ≥ 300 |
| **Reach** | Booth visitors dwelling > 30s | ≥ 200 / day |
| **Hands-on** | People completing all 5 hands-on steps | ≥ 30 / day |
| **Leads** | Email / GitHub follow captures | ≥ 100 |
| **Quality** | On-site GitHub star bump | ≥ +200 |
| **Quality** | Joins to Discord / chat groups on-site | ≥ 50 |
| **Media** | Attendees voluntarily posting on X / social screenshots | ≥ 10 |

### 11.1 On-Site Real-Time Tally

After each `telos showcase` run, the counter at the bottom of dashboard.html ticks +1; on the booth wall, post a small whiteboard reading "shown N times today".

---

## 12. Risk Register

| Risk | Probability | Impact | Fallback |
|---|---|---|---|
| Show-floor Wi-Fi unstable | High | Medium | All demos run offline ✓ |
| Anthropic API rate-limited / unreachable | Medium | High (D only) | D has a 30s video fallback prepared |
| Demo-machine hardware failure | Low | High | Keep 1 cold-spare machine; repo zip on USB stick |
| Large-display HDMI not recognized | Medium | Medium | Bring 3 kinds of adapters + 1 USB-C → HDMI long cable |
| Deep technical drill-down on-site | High | Low (opportunity) | R1–R8 prep + invite to breakout room for a deeper chat (E) |
| Numbers accused of being fake | Low | High | Open `replay_responses.json` on the spot; `_meta.source = "real"` |
| Lead presenter loses voice | Medium | Medium | Keep a pre-recorded video (A's 5-minute version) on hand |

---

## 13. One-Page Cheatsheet (printed on the back; front is the 3-color stele visual)

```
┌───────────────────────────────────────────────────────┐
│  TELOS — Portable Agent Context                       │
│  ─────────────────────────────────────────────────    │
│  ONE problem  Your Agent's turn 20 and turn 19 are 95% byte-identical │
│  ONE rule     Within each segment: PIN → FOLD → DROP                  │
│  ONE IR       Runs Anthropic / OpenAI / DeepSeek /                    │
│               vLLM / SGLang — 5 vendors, not a line of prompt changed │
│                                                       │
│  30s quickstart:                                      │
│    pip install -e .                                   │
│    telos gateway start                                │
│    telos init --agent claude-code                     │
│    claude   # Usage unchanged; cache auto-hits        │
│                                                       │
│  see for yourself:                                    │
│    telos showcase                  # 5-min pre-recorded            │
│    telos showcase --interactive    # hands-on yourself             │
│    telos replay --session <id>     # 4-mode A/B                    │
│                                                       │
│  ───────────────────────────────────────────────────  │
│  ### PIN   tool / system / current question (prefix stable)        │
│  ::: FOLD  history / tool_result / large docs (foldable)           │
│  ... DROP  timestamp / cwd / env (regenerated every turn)          │
│  ───────────────────────────────────────────────────  │
│  GitHub: github.com/<...>/telos-sdk                   │
│  Docs:   docs/playbook.md / docs/User-guide.md        │
└───────────────────────────────────────────────────────┘
```

---

## 13.5 Reproducing the Numbers (type this when questioned)

```bash
cd telos-sdk
python3 - <<'PY'
import json, collections
agg = collections.defaultdict(lambda: {"cache_read":0,"raw_input":0,"calls":0})
for line in open("showcase/usage.jsonl"):
    r = json.loads(line); m = r["mode"]; n = r["normalized"]
    agg[m]["cache_read"] += n["cache_read"]; agg[m]["raw_input"] += n["raw_input"]; agg[m]["calls"] += 1
print(f"{'mode':6s} {'calls':>5s} {'raw_in':>8s} {'cache_read':>11s} {'est_USD':>10s}")
for m in ("none","rtk","telos","both"):
    v = agg[m]; est = (v["raw_input"]*15 + v["cache_read"]*1.5) / 1_000_000
    print(f"{m:6s} {v['calls']:>5d} {v['raw_input']:>8d} {v['cache_read']:>11d}  ${est:>8.4f}")
PY
```

Expected output:

```
mode   calls   raw_in  cache_read    est_USD
none       6    24151           0  $  0.3623
rtk        6    22841           0  $  0.3426
telos      6        0       18701  $  0.0281
both       6        0       17719  $  0.0266
```

Source-data validity: `head -c 200 showcase/replay_responses.json` should show `"_meta": {"source": "real", ...}`.

---

## 14. Action List (T-7 → T-0)

| T- | Owner | Item |
|---|---|---|
| T-7 days | Lead presenter | Rehearse A / D live scripts 3 times; record once for self-review |
| T-7 days | Design | Standee + cheatsheet + QR code materials finalized and sent to print |
| T-5 days | Engineering | Reimage demo machines + `pip install -e .` + offline rehearsal |
| T-5 days | Engineering | `demo_capture` online to refresh real replay numbers (if stale) |
| T-3 days | Lead presenter | Full keynote (A+D+B+E) rehearsal ×2 |
| T-2 days | All | Offline rehearsal checklist (§10) executed once |
| T-1 day | All | All 100 USB experience packs burned |
| T-0 morning | All | On-site setup + HDMI test + 30-minute large-display stability test |
| T+1 | Lead presenter | Lead-capture email sent (with video replay + GitHub link) |

---

<div align="center">
<sub>—— TELOS —— hold the stable parts stable, drive the unstable parts to the tail ——</sub>
</div>
