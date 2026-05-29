<div align="center">

<img src="assets/logo.svg" alt="TELOS — Portable Agent Context" width="460"/>

### Context is yours &nbsp;·&nbsp; Agents are hired

**No rewrite. No compression. 90% token billing saving.**

<sub>One canonical IR — tools, system, turns, and memory — runs unchanged across Anthropic · OpenAI · DeepSeek · vLLM · SGLang<br/>Real 6-turn session −92.3% · Cost reported in absolute $/query-resolved — ratios can be gamed; dollars can't</sub>

<sub>LEAP Lab @ Tsinghua University — a research group focused on machine learning, multimodal learning, and embodied intelligence · <a href="https://www.leaplab.ai/">leaplab.ai</a></sub>

<br/>

[![Core](https://img.shields.io/badge/core-Apache%202.0-2C5F66?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-4FB3BF?style=flat-square)](pyproject.toml)
[![Status](https://img.shields.io/badge/status-Beta-d8851f?style=flat-square)](CHANGELOG.md)
[![Protocol](https://img.shields.io/badge/protocol-TELOS%20IR-7FD8E0?style=flat-square)](docs/2026-05-06-telos-protocol.md)

[**Quickstart**](#quickstart) · [**Support Matrix**](#support-matrix) · [**Why**](#why-telos) · [**Benchmark**](#benchmark) · [**Protocol**](#protocol) · [**Roadmap**](#roadmap) · [**Citation**](#citation)

<sub>📖 &nbsp;**English** · [Simplified Chinese](README.zh-CN.md)</sub>

</div>

---

<a id="problem"></a>

## ⬢ &nbsp;2 a.m. — where did all the money go?

2 a.m., agent still running. The counter in the bottom-right corner climbs to 2,847,103 — you convert it to dollars and your stomach drops. Worse: the line above reads `cache_read: 0`. All night long, every turn fed the same 4,000-token system prompt **from scratch** to the model, billed at full price.

Take the exact same **6-turn** real conversation, drop it into openclaw, flip two switches:

| Mode | raw input tokens | cache_read | Cost for 6 turns |
|---|:--:|:--:|:--:|
| passthrough (today's default) | 24,151 | 0 | **$0.3623** |
| with TELOS | 0 | 18,701 | **$0.0281 (−92.3%)** |

Scale to 1,000 sessions: **$362 → $26**. In a controlled A/B/C/D run (`showcase/dashboard.html`, 2026-05-19) — 48 calls across 4 sessions, counterfactual bill **$5.90**, actual **$3.74** — net saved **$2.16 (−36.6%)**. One dev machine, one afternoon. Multiply by team scale, and that's a real server invoice every month.

**Stop measuring in "X× fewer tokens."** In 2026, the pricing gap between tiers of the same model family already spans **80×–150×**. Anyone can inflate a ratio by stuffing the cheapest tier in the denominator — absolute dollars are the only number that doesn't lie.

<p align="center">
  <img src="assets/01-waste.en.svg" alt="Today's agent token efficiency is only 25%" width="100%"/>
</p>

<a id="quickstart"></a>

## ⬢ &nbsp;3-step to save 90%

#### ❶ &nbsp;Install

```bash
# Linux / macOS / WSL2 / Android (Termux)
curl -fsSL https://raw.githubusercontent.com/learningCatHD/telos-sdk/main/scripts/install.sh | bash
```

<sub>Prefer pip? &nbsp;`pip install -U telos-sdk`</sub>

#### ❷ &nbsp;Connect

```bash
telos init
```

Auto-detects **claude-code / codex / openclaw / hermes** on this machine, injects config into each, and starts the local gateway in the background (state written to `~/.telos/gateway.json`). No changes to your agent code.

#### ❸ &nbsp;Observe

```bash
telos dashboard
```

Opens an offline HTML dashboard in your browser showing savings per call in absolute dollars. Every invocation is automatically appended to `~/.telos/usage.jsonl` and aggregated in real time.

<p align="center">
  <img src="assets/05-dashboard.png" alt="TELOS savings dashboard — absolute dollars broken down by harness / model / session" width="100%"/>
</p>

<p align="center"><sub><strong>Every saving pinned to an absolute dollar figure</strong> · No cloud server required · Opens offline · <code>~/.telos/usage.jsonl</code> fed directly into a single-file HTML page</sub></p>


**TELOS is open source. Run it on your own workflow — see whether that 92% is real, or just another "X× tokens" claim.**

---

<a id="support-matrix"></a>

## ⬢ &nbsp;Support Matrix

### Harness support

| Harness | Typical usage | `telos init` auto-connect | Status |
|---|---|:---:|:---:|
| Claude Code | Anthropic-native coding agent workflow | ✅ | 🟢 First-class |
| OpenClaw | Open-source agent runtime with TELOS parser integration | ✅ | 🟢 First-class |
| Hermes | Multi-agent orchestration with independent sub-IR handling | ✅ | 🟢 First-class |
| Codex | OpenAI-style coding workflow via local gateway injection | ✅ | 🟢 Supported |

### Frontier model support

| Model family | Provider | Through TELOS engine adapter | Notes |
|---|---|:---:|---|
| Claude (4.x / 4.6+) | Anthropic | ✅ | Explicit breakpoints and prewarm path |
| GPT (4+/5.x) | OpenAI | ✅ | Uses `prompt_cache_key` routing strategy |
| DeepSeek (V3+) | DeepSeek | ✅ | Deterministic byte-stable prefix behavior |

### Inference framework support

| Framework | Deployment style | Through TELOS | Cache-aware capabilities |
|---|---|:---:|---|
| vLLM | Self-hosted OpenAI-compatible serving | ✅ | Explicit anchors, prewarm, cache probe/evict, partial fork-and-replace |
| SGLang | Self-hosted high-throughput serving | ✅ | Explicit anchors, prewarm, cache probe/evict, full fork-and-replace |

<sub>Need another harness or model backend? TELOS is adapter-driven: keep the same IR and add an engine/harness adapter without rewriting your agent logic.</sub>

---

<a id="why-telos"></a>

## ⬢ &nbsp;TELOS solves exactly two things

**① Push token efficiency to the limit.** 6-turn real session **−92.3%**; controlled 48-call run **−36.6% (net −$2.16)**. Every cent accounted for in absolute $/query-resolved — ratios can be faked; dollars can't.

**② Return context sovereignty to you.** `TelosIR` is an engine-agnostic, serializable, portable context representation. Your persona, your tools, your 20-turn mid-session thread — everything packed into the same **stone tablet**. Hand it to Claude today, move it to DeepSeek tomorrow, run it on a local vLLM tonight. **Your context; agents are just hired help.**

---

<a id="benchmark"></a>

## ⬢ &nbsp;SWE-bench Verified — TELOS does not regress task correctness

Token savings are only useful if the agent still solves the problem. We ran a pre-registered A/B on **SWE-bench Verified** with the Hermes harness and `deepseek/deepseek-v4-flash` — 100 instances per arm, seeded sample across 8 repos (sphinx, matplotlib, xarray, pytest, requests, pylint, seaborn, flask). **99 instances per arm were graded under the official Docker harness** (1 instance excluded due to a missing per-instance docker image upstream).

#### Resolved rate (docker-graded, n=99/arm, paired)

| Arm | Resolved | Rate | 95% Wilson CI |
|---|---:|---:|---|
| **TELOS** | 45 / 99 | **45.5%** | [36.0%, 55.2%] |
| Vanilla | 42 / 99 | 42.4% | [33.2%, 52.3%] |

Paired 2×2 on the same 99 instances: both resolved 33; TELOS-only 12; vanilla-only 9; neither 45. Exact McNemar two-sided **p = 0.66** — the +3 pp absolute gap is **not statistically significant**, i.e. TELOS does not regress resolved rate at this sample size.

#### Token efficiency (agent-side, n=99/arm, same instances)

| Per-task | TELOS | Vanilla | Δ |
|---|---:|---:|---:|
| **new_input** (post-cache, billed) | 93,712 | 198,706 | **−52.8%** |
| prompt_tokens (raw + cache) | 352,400 | 515,953 | −31.7% |
| output_tokens | 24,975 | 25,218 | −1.0% |
| api_calls | 32.6 | 32.1 | +1.4% |
| **cache_share** | **73.4%** | 61.5% | **+11.9 pp** |
| reported cost (USD) | $2.29 | $3.85 | **−40.5%** |

**Read this honestly.** The 99-instance subset gives a Wilson CI of roughly ±10 pp on each arm. This run can rule out an absolute regression worse than ~6 pp at 95% confidence (the lower bound of the paired difference), but cannot pin Δ to ±2 pp. What it shows with high confidence is the **input-token bill is roughly halved, and end-to-end cost drops ~40%, at the same correctness band**. A larger run (n ≥ 400/arm) is on the roadmap to tighten the resolved-rate confidence interval further.

<sub>Raw outputs: agent runs in [`/tmp/telos-ab-n100/{with,without}/`](/tmp/telos-ab-n100/), docker-graded reports in [`/tmp/telos-ab-n100/docker-eval/`](/tmp/telos-ab-n100/docker-eval/). Reproduce: `scripts/run_swebench_batch.py -n 100 --seed 7`. Full technical report (pre-registered design, statistical detail, related work): [docs/2026-05-26-swebench-ab.md](docs/2026-05-26-swebench-ab.md).</sub>

---

<a id="protocol"></a>

## ⬢ &nbsp;The protocol: not compression, but never breaking the prefix

Most agent frameworks treat KV-cache as a runtime gift the inference engine may or may not give you. TELOS inverts this:

> **Cache reuse is a structural property of the prompt itself, not a matter of runtime luck. If you never touch bytes already submitted, the cache *cannot* be invalidated.**

That principle materializes in three interlocking ideas.

### Three-color bands

<p align="center">
  <img src="assets/03-banding.en.svg" alt="PIN / FOLD / DROP bands" width="100%"/>
</p>

Every content block declares its cache lifetime **at birth** — not post-hoc heuristics, not LLM guessing, but first-class structural annotation:

| Band | Color | Semantics | Cache behavior |
|---|:---:|---|---|
| **PIN** | 🟢 | Tool defs · system prompt · current question | Permanent. Never evicted. The immutable base of every request's prefix hash |
| **FOLD** | 🟡 | Conversation history · tool results · large docs | Cacheable, compactable. Under pressure, replaced by a summary — PIN prefix bytes stay untouched |
| **DROP** | 🔴 | Timestamps · CWD · git status · PIDs | Ephemeral. **Excluded entirely from the prefix hash.** Must follow all BPs; never contaminates upstream bytes |

The ordering invariant is absolute: **PIN\* → FOLD\* → DROP\*** — within each message, across the full prompt, at every layer. This is the **only** structural rule that wins the cache — everything else is implementation detail.

### Monotonic append

The prompt is an **append-only stream**. New turns only add blocks to the tail — **no mutation of already-submitted bytes**. A "modification" is expressed as a new block (summary, redaction), never an in-place rewrite.

<p align="center">
  <img src="assets/04-append.en.svg" alt="Monotonic append: cache hit rate is monotonically non-decreasing with session length" width="100%"/>
</p>

Because earlier blocks are immutable and bytes are identical across turns, the inference engine's prefix-matching algorithm finds the longest common prefix on **every** request — not by luck, but by construction. **Cache hit rate is therefore a monotonically non-decreasing function of session length: longer sessions, more reuse, never regression.**

---

<a id="roadmap"></a>

## ⬢ &nbsp;Roadmap

TELOS makes exactly one claim: **context is yours, agents are hired.** The current roadmap stays entirely within the *cost-saving gateway* narrative, with the seed of *trajectory as a portable asset* planted only in the last phase. **Anything that can be checked off goes on the roadmap; anything that can't, doesn't.**

| Phase | Thesis |
|---|---|
| **Phase 1** · Protocol correctness hardening | Turn "cache cannot be invalidated" from a slogan into a CI red/green light |
| **Phase 2** · Production reliability & observability | Make the gateway safe to leave on someone else's prod traffic |
| **Phase 3** · Take over the call chain | Go from prompt rewriter to the agent's traffic plane |
| **Phase 4** · Context becomes an asset | Trajectories are no longer logs — they're forkable code |

---

<a id="citation"></a>

## Citation

Core contributors: Zheng Wang, Shenzhi Wang, Yue Wu, Shiji Song, Gao Huang

```
@misc{wang2026telos-agent,
  title        = {Telos: A Cost-Aware Inference Infrastructure for AI Agent},
  author       = {Zheng Wang, Shenzhi Wang, HongTao Zhong, Shiji Song, Gao Huang},
  howpublished = {\url{https://github.com/learningCatHD/telos-sdk.git}},
  year         = {2026}
}
```

---

<div align="center">
<a href="https://github.com/learningCatHD/telos-sdk"><img src="https://img.shields.io/badge/⭐%20Star%20on%20GitHub-learningCatHD%2Ftelos--sdk-1F4A50?style=for-the-badge&logo=github&logoColor=white" alt="Star on GitHub"/></a>
