# TELOS 测试方案

> 目标：把现有 ~260 个测试整理成一个**能在每次 CI 自动跑、覆盖现有基本功能**的测试体系。
> 原则：务实、最小改动、不引入重型工具（不上 Hypothesis / mutation testing）。
>
> Last updated: 2026-05-30

---

## 1. 范围与原则

- **只覆盖现有基本功能**——固化当前行为，防回归。不追求穷尽边界。
- **每次 push / PR 自动跑**——秒级反馈，而不是等一次 Docker build。
- **保持简单**——纯 pytest，零额外依赖（`pytest` + `pytest-asyncio` 已在 `[project.optional-dependencies].test`）。

---

## 2. 现状速览（已实测）

| 项 | 现状 |
|---|---|
| 测试规模 | 35 个文件，约 260 个测试函数 |
| 实跑结果 | 根目录 `pytest` → **228 passed, ~17s**（pytest 默认只收集 `test_*.py`，不会误碰 `scripts/`，裸跑本身没问题） |
| **运行入口** | ⚠️ **CI 只在 `Dockerfile` 的 builder stage 里 `RUN pytest -q`**——埋在 Docker build 里，反馈慢，且 base 镜像固定 `python:3.12-slim`，**只有 3.12 一个版本被真正跑过**（README 宣称支持 3.10–3.13） |
| pytest 配置 | ❌ 无 `[tool.pytest.ini_options]`：无 `testpaths`、无 markers（live 测试只能靠散落的 `os.environ` 判断跳过） |
| conftest | ⚠️ 无 `tests/conftest.py`。init 测试已用显式 `settings_path=tempfile.mkdtemp()` 自我隔离；但默认落到 `~/.telos` 的 corpus / usage_log 类路径缺一层统一兜底 |
| 覆盖率 | ❌ 未测量 |

**一句话诊断**：测试本身能跑、质量不差。**真正缺的是「每次 CI 直接、快速、多版本地自动跑」**——现在它被埋在 Docker build 里，既慢又只验 3.12。其余（配置/markers/conftest/覆盖率）是锦上添花的卫生项。

---

## 3. 现有功能覆盖地图

现有测试已覆盖的功能区（即「基本功能」基线）：

| 功能区 | 对应模块 | 现有测试文件 | 状态 |
|---|---|---|---|
| IR / band 序 / 不变量 | `ir.py` | `test_band_order_regression`, `test_smoke` | ✅ |
| Bridge 状态 / canonicalize | `bridge.py` | `test_bridge_session_state`, `test_smoke` | ✅ |
| 引擎 emit / usage 归一 | `engine/*` | `test_smoke`（3 引擎 round-trip、bidirectional、vllm、usage） | ⚠️ 仅 smoke 级 |
| Harness 解析 / 探测 | `harness/*` | `test_harnesses`, `test_harness_header_detection`, `test_harness_multiblock`, `test_harness_presets`, `test_telos_harness_reasoning` | ✅ |
| Proxy 管道 / 模式 / 会话 | `proxy/*` | `test_proxy_pipeline`, `test_proxy_mode`, `test_proxy_openai_route`, `test_proxy_server`, `test_proxy_session_id`, `test_proxy_accumulation` | ✅ |
| 安装 / 卸载 | `init/*` | `test_init_claude_code`, `test_init_codex`, `test_init_hermes(_multi)`, `test_init_openclaw(_multi)`, `test_init_env_installers`, `test_init_gateway_url` | ✅ |
| 输出过滤 (RTK) | `output_filter/*` | `test_output_filter` | ✅ |
| 录制 / 回放 | `corpus.py`, `replay/*` | `test_corpus`, `test_replay` | ✅ |
| CLI / 网关 | `cli.py`, `gateway/*` | `test_cli_dispatch`, `test_gateway_control_mode`, `test_gateway_daemon` | ✅ |
| Dashboard / showcase | `scripts/*` | `test_developer_page`, `test_savings_dashboard`, `test_showcase` | ✅ |
| SDK transport 累加 | `scripts/telos_*_transport` | `test_sdk_transport_accumulation` | ✅ |
| 配置 / cast | `config.py`, `cast.py` | `test_config`, `test_cast` | ✅ |

**未有专属测试文件（仅靠 smoke 间接覆盖）的少量空白**（可选补，见 §6）：

- `refpool.py` — 无 `test_refpool.py`（slug 冻结、`register_or_skip`、`fold`、`lint` 仅在 smoke 里间接走到）
- `registry.py` — 无 `load_harness` / `load_engine` 的专属测试
- 各引擎适配器 — 无 per-adapter 文件（Anthropic BP 截断优先级、OpenAI routing_key、DeepSeek 空 plan 等只有 smoke 级断言）

---

## 4. 方案：核心 1 项 + 卫生 3 项

> **改动 B 是核心**（直接满足「每次 CI 自动跑」）；A / C / D 是配套卫生项，可后续补。

### 改动 B（核心）— 加独立的快速 CI job（每次 push / PR 自动跑、多版本）

现有 `.github/workflows/ci.yml` 只有 Docker build。新增一个**轻量 pytest job**，在每次 push / PR 上跑，矩阵覆盖 README 声明的 3.10–3.13：

```yaml
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10", "3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: pip install -e ".[test]"
      - run: pytest          # ~17s，根目录直接跑通
```

- Docker `build` job **保留**——继续负责构 wheel / 推镜像 / 发 PyPI（发布前的最终校验）。
- 日常 PR 反馈从「等一次 buildx」缩短到「~1 分钟」，且四个 Python 版本都被真正跑到（弥补 Docker 只验 3.12 的缺口）。

### 改动 A（卫生）— 加 pytest 配置 + markers

在 `pyproject.toml` 追加，把「live 测试如何跳过」从散落的 env 判断收敛为统一约定：

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]          # 显式限定收集范围，收集更快、意图更清晰
addopts = "-q"
markers = [
    "integration: 起 aiohttp 假上游 / 文件系统的较重测试",
    "live: 需要真实 API key，默认跳过（TELOS_LIVE_TESTS=1 才跑）",
]
```

> 注：现有 async 测试已能通过（多用 `asyncio.run(...)` 直跑），故**不强制**加 `asyncio_mode`；除非后续改写成 `async def test_`，再按需开启。

### 改动 C（卫生）— 加 `tests/conftest.py` 兜底隔离

init 安装器测试已通过显式 `settings_path=tempfile.mkdtemp()` 自我隔离，**无需**再动它们。conftest 的价值是给「默认落到 `~/.telos`」的 corpus / usage_log 类路径加一层统一兜底，防止某个测试忘了隔离就污染开发机：

```python
import pytest

@pytest.fixture(autouse=True)
def _isolate_telos_home(tmp_path, monkeypatch):
    """默认重定向 HOME 到临时目录，杜绝污染真实 ~/.telos / ~/.claude。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TELOS_LIVE_TESTS", raising=False)
    return tmp_path
```

> 落地时先确认重定向 HOME 不会让已显式传路径的 init 测试出现意外；若有冲突，改为非 autouse 的按需 fixture。

### 改动 D（卫生）— 覆盖率报告（先观测，不卡门槛）

CI test job 里加一步（非阻塞，仅出报告）：

```yaml
      - run: pip install pytest-cov
      - run: pytest --cov=telos --cov-report=term-missing --cov-report=xml
```

第一阶段**不设硬门槛**，先看核心模块（`ir` / `bridge` / `refpool` / `engine` / `harness` / `proxy`）的真实覆盖数；稳定后再考虑对这几个核心模块设一个温和下限（如 ≥85%）。

---

## 5. 落地步骤

1. **B（核心）**：加 CI `test` job，推一个 PR 看四个 Python 版本（3.10–3.13）全绿——「每次 CI 自动跑」即落地。
2. **A**：改 `pyproject.toml`（testpaths + markers），确认本地 228 passed 不回退。
3. **C**：加 `tests/conftest.py` 兜底隔离，确认仍 228 passed。
4. **D**：CI 接 `--cov`，记录基线覆盖率数字。
5.（可选）**§6** 补三个空白专属测试文件。

每步独立可合入，互不阻塞；只做第 1 步就已满足核心诉求。

---

## 6. 可选补缺（非必须，按 §3 空白）

若想把「基本功能」补齐到每个核心模块都有专属文件，新增三个小文件即可，每个 5–10 个用例：

- `tests/test_refpool.py` — slug 注册即冻结、重复注册报错、`register_or_skip` 幂等、`fold` 不动 slug、`lint_blocks` 对未注册引用 fail-fast。
- `tests/test_registry.py` — `load_harness` / `load_engine` 按名加载、未知名报错。
- `tests/test_engine_adapters.py` — 表驱动断言 §7.2 能力矩阵（`explicit_breakpoints` / `max_breakpoints` / bidirectional），Anthropic BP>4 截断顺序。

---

## 7. 验收标准

- [ ] **（核心）** 每次 push / PR，GitHub Actions 直接跑全套 pytest 并显示结果，~1 分钟内反馈。
- [ ] 四个 Python 版本（3.10–3.13）矩阵全绿（不再只验 3.12）。
- [ ] `live` 测试有统一 marker，默认跳过、`TELOS_LIVE_TESTS=1` 才跑。
- [ ] 测试运行不污染开发机的 `~/.claude` / `~/.telos`。
- [ ] CI 输出覆盖率数字（观测用，暂不卡门槛）。
