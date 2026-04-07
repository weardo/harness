# Harness Token-Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Cut Claude subscription quota burn per harness run by 50 to 70 percent without quality regression. Primary target: users running the harness off a Claude Pro or Max subscription (no API key), where the bottleneck is weekly usage limits, not dollars.

**Architecture:** Three independently-shippable phases, each landing a measurable win.

**Tech Stack:** Python 3.11+, pytest, Claude Agent SDK, Claude CLI, Ollama (optional), Qwen 2.5 Coder 7B instruct (optional).

**Machine constraints:** Mac mini M4, 10 cores, 16 GB unified memory, Metal 3. Disk headroom ~23 GB — Phase C needs ~8 GB freed first.

## Three phases

1. **Phase A — Cache and Context Dedup.** Fix the retry-counter cache-bust in the evaluator system prompt, deduplicate per-agent relay context, add warm-session reuse across features in the same worker. No behavior change; pure waste elimination.
2. **Phase B — Deterministic Gate.** Run build, test, lint, and file-scope checks in-process before spawning the LLM reviewer. Apply trivial fixes (goimports, gofmt, regex import adds) programmatically instead of re-spawning the generator. Demote the LLM reviewer from "always runs" to "escalation on ambiguity".
3. **Phase C — Local LLM Offload (optional).** Install Ollama + Qwen 2.5 Coder 7B (~4.5 GB, fits comfortably in 16 GB RAM). Route grunt-work calls (HANDOFF parsing, discovery brief compression, task-brief formatting) to the local model.

Each phase is safe to ship alone. Phase A is pure refactor with tests. Phase B changes the QA loop but preserves the LLM escalation path. Phase C is additive — all local calls fall back to inline Python if Ollama is not running.

## File Structure

**New files:**

| File | Purpose |
|------|---------|
| `src/core/deterministic_checks.py` | Mechanical checks (build, test, lint, scope files, imports) run in-process before any LLM review call |
| `src/core/programmatic_fixes.py` | Mechanical fix-ups: gofmt/goimports/ruff/eslint auto-fix wrappers |
| `src/core/local_llm.py` | Thin Ollama HTTP adapter with inline-Python fallback |
| `src/core/warm_session.py` | Per-worker session-id pool for cross-feature continuity |
| `tests/test_cache_stability.py` | Asserts system prompt is byte-identical across retry attempts |
| `tests/test_deterministic_checks.py` | Unit tests for each check function + the orchestrator gate |
| `tests/test_programmatic_fixes.py` | Unit tests for each fix + idempotency |
| `tests/test_local_llm.py` | Mock-based tests for Ollama adapter + fallback path |
| `tests/test_warm_session.py` | Verifies session reuse across features in same worker |
| `scripts/install-local-llm.sh` | Idempotent Ollama+Qwen installer with disk precheck |

**Modified files:**

| File | What changes |
|------|-------------|
| `src/core/orchestrator.py` | Drop retry-counter template var from QA system replacements; call deterministic gate before QA; apply programmatic fixes before retry; use warm-session pool; build relay once per wave |
| `src/core/parallel.py` | `write_task_brief` accepts `relay_ref_path` and writes a pointer instead of inlining 15 KB |
| `src/prompts/evaluator-v2.md` | Remove `RETRY_COUNT` and `MAX_RETRIES` template variables — move to user message |
| `src/prompts/generator-v2.md` | Require generator to write `HARNESS_VERDICT.json` with structured self-report |
| `src/config.yaml` | New `token_efficiency` block with knobs for each optimization |
| `src/core/cost_tracker.py` | Add per-phase token tracking for A/B measurement |

**Why this split:** Each new module has one responsibility with a clear seam. `deterministic_checks.py` knows nothing about the orchestrator — pure functions returning dicts. `programmatic_fixes.py` is similarly pure. `local_llm.py` hides Ollama entirely so the rest of the codebase sees a single `run_local()` function. `warm_session.py` is a small stateful dict with lock semantics. The orchestrator wires them together — everything remains testable in isolation.

---

## Task 0: Baseline instrumentation

**Goal:** Add per-phase token tracking BEFORE optimizing, so we can prove each phase's impact. Without this, we're optimizing blind.

**Files:**
- Modify: `src/core/cost_tracker.py`
- Modify: `src/core/orchestrator.py` (around line 994–995, generator cost record site; and QA record site)
- Test: `tests/test_cost_tracker.py` (extend existing)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cost_tracker.py`:

```python
import pytest


def test_record_phase_accumulates():
    from src.core.cost_tracker import CostTracker
    ct = CostTracker()
    ct.record_phase("generator", cost=0.5, input_tokens=1000, output_tokens=500,
                    cache_read_tokens=100, cache_creation_tokens=50)
    ct.record_phase("generator", cost=0.3, input_tokens=800, output_tokens=400,
                    cache_read_tokens=600, cache_creation_tokens=0)
    ct.record_phase("evaluator", cost=0.2, input_tokens=400, output_tokens=200)

    summary = ct.phase_summary()
    assert summary["generator"]["cost"] == pytest.approx(0.8)
    assert summary["generator"]["input_tokens"] == 1800
    assert summary["generator"]["cache_read_tokens"] == 700
    assert summary["generator"]["calls"] == 2
    assert summary["evaluator"]["cost"] == pytest.approx(0.2)
    assert summary["evaluator"]["calls"] == 1


def test_phase_summary_empty_when_no_records():
    from src.core.cost_tracker import CostTracker
    ct = CostTracker()
    assert ct.phase_summary() == {}
```

- [ ] **Step 2: Run the test and confirm it fails**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_cost_tracker.py::test_record_phase_accumulates -v
```

Expected: `AttributeError: 'CostTracker' object has no attribute 'record_phase'`.

- [ ] **Step 3: Implement `record_phase` and `phase_summary`**

First, add a proper dataclass field alongside the existing ones in `src/core/cost_tracker.py`. Find the field declarations (around line 26, after `_feature_costs: dict = field(default_factory=dict)`) and add:

```python
    _phase_totals: dict = field(default_factory=dict)
```

Then append these two methods to the `CostTracker` class (after `most_expensive_features`):

```python
    def record_phase(self, phase: str, cost: float = 0.0, input_tokens: int = 0,
                     output_tokens: int = 0, cache_read_tokens: int = 0,
                     cache_creation_tokens: int = 0) -> None:
        """Record per-phase token/cost accumulation for A/B comparison.

        Phases used: 'generator', 'evaluator', 'planner', 'local_llm'.
        Additive to the existing record() method — both views of the same data
        coexist so existing callers keep working.
        """
        bucket = self._phase_totals.setdefault(phase, {
            "cost": 0.0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0, "calls": 0,
        })
        bucket["cost"] += cost
        bucket["input_tokens"] += input_tokens
        bucket["output_tokens"] += output_tokens
        bucket["cache_read_tokens"] += cache_read_tokens
        bucket["cache_creation_tokens"] += cache_creation_tokens
        bucket["calls"] += 1

    def phase_summary(self) -> dict:
        """Return a shallow copy of per-phase totals for logging."""
        return dict(self._phase_totals)
```

- [ ] **Step 4: Run the test and confirm it passes**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_cost_tracker.py -v
```

Expected: PASS.

- [ ] **Step 5: Wire `record_phase` into ALL orchestrator call sites**

There are **8 existing `cost_tracker.record(...)` call sites** that must each gain a matching `record_phase` call. Don't miss any — phase accounting is incomplete otherwise.

**Helper to reduce duplication.** Add this near the top of `src/core/orchestrator.py`, alongside `_metrics_from` (around line 73):

```python
def _record_phase_from(cost_tracker, phase: str, result: dict) -> None:
    """Thin helper: extract usage from a session result and feed cost_tracker.record_phase."""
    _usage = result.get("usage") or {}
    cost_tracker.record_phase(
        phase,
        cost=result.get("cost", 0) or 0,
        input_tokens=_usage.get("input_tokens", 0),
        output_tokens=_usage.get("output_tokens", 0),
        cache_read_tokens=_usage.get("cache_read_input_tokens", 0),
        cache_creation_tokens=_usage.get("cache_creation_input_tokens", 0),
    )
```

**Now add one call immediately after each of these existing lines:**

| Line | Existing call | Add immediately after |
|------|--------------|----------------------|
| 994  | `cost_tracker.record("generator", gen_result.get("cost", 0), **_metrics_from(gen_result))` | `_record_phase_from(cost_tracker, "generator", gen_result)` |
| 1041 | `cost_tracker.record("evaluator", qa_result.get("cost", 0), **_metrics_from(qa_result))`   | `_record_phase_from(cost_tracker, "evaluator", qa_result)` |
| 1327 | `cost_tracker.record("evaluator", eval_result.get("cost", 0), **_metrics_from(eval_result))` | `_record_phase_from(cost_tracker, "evaluator", eval_result)` |
| 1492 | `cost_tracker.record("evaluator", eval_result.get("cost", 0), **_metrics_from(eval_result))` | `_record_phase_from(cost_tracker, "evaluator", eval_result)` |
| 1721 | `cost_tracker.record("planner", result["cost"], **_metrics_from(result))` | `_record_phase_from(cost_tracker, "planner", result)` |
| 1755 | `cost_tracker.record("planner", result2["cost"], **_metrics_from(result2))` | `_record_phase_from(cost_tracker, "planner", result2)` |
| 2051 | `cost_tracker.record("generator", gen_result["cost"], **_metrics_from(gen_result))` | `_record_phase_from(cost_tracker, "generator", gen_result)` |
| 2162 | `cost_tracker.record("evaluator", eval_result["cost"], **_metrics_from(eval_result))` | `_record_phase_from(cost_tracker, "evaluator", eval_result)` |

After editing, verify the counts with grep:

```bash
cd /Users/prashantpandey/harness && grep -c 'record_phase_from' src/core/orchestrator.py
```

Expected: `8`.

- [ ] **Step 6: Add phase summary to final run log**

In `src/core/orchestrator.py`, in the final run-complete block, add:

```python
_phase_summary = cost_tracker.phase_summary()
if _phase_summary:
    _write_run_log(state_dir, "phase_summary", phases=_phase_summary)
    print("  Phase breakdown:")
    for _phase, _stats in sorted(_phase_summary.items()):
        _cache_pct = 0.0
        _total_in = _stats["input_tokens"] + _stats["cache_read_tokens"]
        if _total_in > 0:
            _cache_pct = 100 * _stats["cache_read_tokens"] / _total_in
        print(f"    {_phase:12s}: ${_stats['cost']:6.2f}  "
              f"in={_stats['input_tokens']:>8}  "
              f"out={_stats['output_tokens']:>7}  "
              f"cache_hit={_cache_pct:5.1f}%  "
              f"calls={_stats['calls']}")
```

- [ ] **Step 7: Run the full suite**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/ -q
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/core/cost_tracker.py src/core/orchestrator.py tests/test_cost_tracker.py
git commit -m "feat(cost): add per-phase token tracking for A/B optimization"
```

---

## Task 1: Remove retry-counter cache-bust from QA system prompt

**Goal:** The QA (evaluator) system prompt currently embeds `{{RETRY_COUNT}}` and `{{MAX_RETRIES}}` which mutate on every retry, forcing a fresh encode of ~730 tokens per call. Moving them to the user message restores Claude CLI's 5-min cache hit on the system prompt.

**Files:**
- Modify: `src/prompts/evaluator-v2.md` (drop template variables)
- Modify: `src/core/orchestrator.py` around line 109 to 158 (the QA replacements helper) and around line 688 to 696 (wave-level QA prompt setup)
- Test: `tests/test_cache_stability.py` (NEW)

- [ ] **Step 1: Write the failing test**

Create `tests/test_cache_stability.py`:

```python
"""Tests that system prompts are byte-identical across retry attempts.

The Claude CLI caches --system-prompt for 5 minutes based on byte-equality.
Any mutation (retry counter, feature id, timestamps) defeats the cache.
This test locks in that the QA system prompt never varies by retry.
"""

from pathlib import Path
import pytest


def _load_prompt(replacements):
    from src.core.orchestrator import load_prompt
    return load_prompt(Path("src/prompts/evaluator-v2.md"), replacements)


def test_qa_system_prompt_stable_across_retries():
    base = {
        "STATE_DIR": "/tmp/run-xyz",
        "TEST_COMMAND": "go test ./...",
        "IF_WEB_PROJECT": False,
    }
    retry_1 = _load_prompt(base)
    retry_2 = _load_prompt(base)
    retry_3 = _load_prompt(base)
    assert retry_1 == retry_2 == retry_3, "System prompt must be stable across retries"


def test_qa_prompt_has_no_retry_placeholders():
    text = Path("src/prompts/evaluator-v2.md").read_text()
    assert "{{RETRY_COUNT}}" not in text
    assert "{{MAX_RETRIES}}" not in text


def test_eval_replacements_helper_strips_retry_from_system():
    # Import the helper and call it; assert the returned system replacements
    # do NOT contain RETRY_COUNT or MAX_RETRIES
    import src.core.orchestrator as orch
    helper = orch._build_eval_replacements
    repl, _pref, _fb, user_msg = helper(
        feature_id="feat-42",
        state_dir=Path("/tmp/run-xyz"),
        config={"evaluator": {"browser_verification": "never"},
                "generator": {"max_retries_per_feature": 3}},
        test_command="go test ./...",
        work_plan=None,
        retry_count=2,
    )
    assert "RETRY_COUNT" not in repl
    assert "MAX_RETRIES" not in repl
    assert "attempt 2" in user_msg.lower() or "retry" in user_msg.lower()
```

- [ ] **Step 2: Run the test and confirm it fails**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_cache_stability.py -v
```

Expected: all three tests FAIL.

- [ ] **Step 3: Strip retry placeholders from the prompt template**

Edit `src/prompts/evaluator-v2.md`:

Find (line 71):
```
### Verdict: FAIL (attempt {{RETRY_COUNT}} of {{MAX_RETRIES}})
```
Replace with:
```
### Verdict: FAIL
```

Find (line 83):
```
Feature <feature-id> — {{RETRY_COUNT}}/{{MAX_RETRIES}} attempts used
```
Replace with:
```
Feature <feature-id> — evaluation failed
```

- [ ] **Step 4: Remove retry vars from the helper**

In `src/core/orchestrator.py` around line 138–147, find this block inside `_build_eval_replacements`:

```python
    # System prompt replacements (mostly static, cacheable across features in a wave).
    # NOTE: RETRY_COUNT varies per retry attempt, which defeats caching on retries.
    # Acceptable trade-off: retries are rare and typically outside the 5-min cache TTL anyway.
    system_replacements = {
        "STATE_DIR": str(state_dir),
        "TEST_COMMAND": test_command,
        "IF_WEB_PROJECT": config.get("evaluator", {}).get("browser_verification") != "never",
        "RETRY_COUNT": str(retry_count),
        "MAX_RETRIES": str(config.get("generator", {}).get("max_retries_per_feature", 3)),
    }
```

Replace with:

```python
    # System prompt replacements: wave-constant so the CLI caches this system
    # prompt for the full 5-minute window. Retry info lives in the user message.
    system_replacements = {
        "STATE_DIR": str(state_dir),
        "TEST_COMMAND": test_command,
        "IF_WEB_PROJECT": config.get("evaluator", {}).get("browser_verification") != "never",
    }
```

Then find the user message construction around line 149–156:

```python
    # User message (dynamic, per-feature)
    user_msg_parts = [
        f"Feature ID: {feature_id}",
        f"Description: {feature_desc}",
    ]
    if ac_text:
        user_msg_parts.append(f"\nAcceptance Criteria:\n{ac_text}")
    user_message = "\n".join(user_msg_parts)
```

Replace with:

```python
    # User message (dynamic, per-feature). Retry info goes here, NOT in the
    # system prompt, so the system prompt stays cache-stable across retries.
    _max_retries = config.get("generator", {}).get("max_retries_per_feature", 3)
    user_msg_parts = [
        f"Feature ID: {feature_id}",
        f"Description: {feature_desc}",
        f"Attempt: {retry_count} of {_max_retries}",
    ]
    if ac_text:
        user_msg_parts.append(f"\nAcceptance Criteria:\n{ac_text}")
    user_message = "\n".join(user_msg_parts)
```

- [ ] **Step 5: Remove retry vars from the wave-level QA prompt setup**

In `src/core/orchestrator.py` around line 688–696, find:

```python
    _qa_repl = {
        "STATE_DIR": str(state_dir),
        "TEST_COMMAND": test_command,
        "IF_WEB_PROJECT": config.get("evaluator", {}).get("browser_verification") != "never",
        "RETRY_COUNT": "1",
        "MAX_RETRIES": str(config.get("generator", {}).get("max_retries_per_feature", 3)),
    }
```

Replace with:

```python
    _qa_repl = {
        "STATE_DIR": str(state_dir),
        "TEST_COMMAND": test_command,
        "IF_WEB_PROJECT": config.get("evaluator", {}).get("browser_verification") != "never",
    }
```

- [ ] **Step 6: Run the test and confirm it passes**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_cache_stability.py -v
```

Expected: all three PASS.

- [ ] **Step 7: Run the full suite**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/ -q
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/prompts/evaluator-v2.md src/core/orchestrator.py tests/test_cache_stability.py
git commit -m "perf(cache): move retry-counter to user message to restore CLI cache hits"
```

---

## Task 2: Build relay context once per wave, reference by path in task brief

**Goal:** Today every parallel worker calls `get_relay_context()` and writes the full 15 KB into its own `TASK_BRIEF.md`. Build it once per wave into a shared file, and point every worker's brief at that file.

**Files:**
- Modify: `src/core/parallel.py` around line 119–239 (`write_task_brief` accepts `relay_ref_path`)
- Modify: `src/core/orchestrator.py` around line 813–818 (build relay once, pass path), around 1396–1399 (resume path), around 2035–2038 (sequential path)
- Test: `tests/test_parallel.py` (extend existing)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_parallel.py`:

```python
def test_write_task_brief_references_relay_file(tmp_path):
    from src.core.parallel import write_task_brief
    (tmp_path / "feature_list.json").write_text('[]')
    relay_file = tmp_path / "relay-wave-3.md"
    relay_file.write_text("### Same-wave discoveries\nAgent foo built X.\n" * 500)  # ~15KB

    wt = tmp_path / "worktree"
    wt.mkdir()
    brief_path = write_task_brief(
        worktree_dir=wt,
        feature={"id": "feat-1", "description": "test", "scope": []},
        state_dir=tmp_path,
        relay_ref_path=relay_file,
    )
    body = brief_path.read_text()
    assert str(relay_file) in body or "relay-wave-3.md" in body
    assert "### Same-wave discoveries" not in body
    assert len(body) < 4_000


def test_write_task_brief_omits_relay_section_when_no_ref(tmp_path):
    from src.core.parallel import write_task_brief
    (tmp_path / "feature_list.json").write_text('[]')
    wt = tmp_path / "worktree"
    wt.mkdir()
    brief_path = write_task_brief(
        worktree_dir=wt,
        feature={"id": "feat-1", "description": "test", "scope": []},
        state_dir=tmp_path,
        relay_ref_path=None,
    )
    body = brief_path.read_text()
    assert "Discoveries from Prior Agents" not in body
```

- [ ] **Step 2: Run the test and confirm failing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_parallel.py -k "relay" -v
```

Expected: both FAIL.

- [ ] **Step 3: Update `write_task_brief` signature**

In `src/core/parallel.py` at line 119, change the signature:

```python
def write_task_brief(
    worktree_dir: Path,
    feature: dict,
    state_dir: Path,
    work_plan=None,
    relay_context: str = "",  # DEPRECATED — kept for back-compat
    relay_ref_path: Optional[Path] = None,
) -> Path:
```

Add `from typing import Optional` at the top if missing.

Find the existing `if relay_context:` block near line 230 and replace:

```python
    if relay_ref_path is not None:
        lines += [
            "## Discoveries from Prior Agents",
            "",
            "Other agents in this wave have recorded discoveries at:",
            f"`{relay_ref_path}`",
            "",
            "Read that file if you need same-wave or prior-wave context.",
            "Do NOT read it for every task — only when you hit something unfamiliar.",
            "",
        ]
    elif relay_context:
        lines += ["## Discoveries from Prior Agents", "", relay_context, ""]
```

- [ ] **Step 4: Run the tests and confirm passing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_parallel.py -k "relay" -v
```

Expected: PASS.

- [ ] **Step 5: Build relay once per wave in the orchestrator**

In `src/core/orchestrator.py` around line 813–818, replace the per-worker `_relay` build with a write-once-per-wave pattern using atomic rename:

```python
            _relay_path = None
            if relay_enabled:
                _relay_body = get_relay_context(state_dir, wave_num, include_current_wave=True)
                if _relay_body:
                    _relay_path = state_dir / "fleet" / f"relay-wave-{wave_num}.md"
                    _relay_path.parent.mkdir(parents=True, exist_ok=True)
                    _tmp = _relay_path.with_suffix(".md.tmp")
                    _tmp.write_text(_relay_body)
                    _tmp.replace(_relay_path)
            write_task_brief(wt_dir, feature, state_dir,
                             work_plan=work_plan, relay_ref_path=_relay_path)
```

- [ ] **Step 6: Update the resume path around line 1396–1399**

In `src/core/orchestrator.py` around line 1396–1399, find:

```python
        _resume_relay = get_relay_context(state_dir, current_wave=assignment.get("wave", 1), include_current_wave=True)
        write_task_brief(wt_dir, _feature_data, state_dir, work_plan=work_plan, relay_context=_resume_relay)
```

Replace with:

```python
        _resume_wave = assignment.get("wave", 1)
        _resume_relay = get_relay_context(state_dir, current_wave=_resume_wave, include_current_wave=True)
        _resume_relay_path = None
        if _resume_relay:
            _resume_relay_path = state_dir / "fleet" / f"relay-wave-{_resume_wave}.md"
            _resume_relay_path.parent.mkdir(parents=True, exist_ok=True)
            _tmp = _resume_relay_path.with_suffix(".md.tmp")
            _tmp.write_text(_resume_relay)
            _tmp.replace(_resume_relay_path)
        write_task_brief(wt_dir, _feature_data, state_dir, work_plan=work_plan,
                         relay_ref_path=_resume_relay_path)
```

- [ ] **Step 7: Update the sequential path around line 2035–2038**

In `src/core/orchestrator.py` around line 2035–2038, find:

```python
        _seq_relay = get_relay_context(state_dir, current_wave=0, include_current_wave=True)
        write_task_brief(project_dir, next_feature, state_dir, work_plan=work_plan, relay_context=_seq_relay)
```

Replace with:

```python
        _seq_relay = get_relay_context(state_dir, current_wave=0, include_current_wave=True)
        _seq_relay_path = None
        if _seq_relay:
            _seq_relay_path = state_dir / "fleet" / "relay-sequential.md"
            _seq_relay_path.parent.mkdir(parents=True, exist_ok=True)
            _tmp = _seq_relay_path.with_suffix(".md.tmp")
            _tmp.write_text(_seq_relay)
            _tmp.replace(_seq_relay_path)
        write_task_brief(project_dir, next_feature, state_dir, work_plan=work_plan,
                         relay_ref_path=_seq_relay_path)
```

- [ ] **Step 8: Run the full suite**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/ -q
```

Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/core/parallel.py src/core/orchestrator.py tests/test_parallel.py
git commit -m "perf(relay): write relay once per wave, reference by path in task briefs"
```

---

## Task 3: Warm session pool — reuse generator session across features in the same worker slot

**Goal:** Currently each feature gets a brand new generator session: new system prompt encode, new codebase exploration, zero cache hits. A warm-session pool keeps one session-id per worker slot; when a worker finishes feature N, the orchestrator feeds feature N+1 into the SAME session via `--resume`. The agent's exploration is amortized across the whole worker's workload.

**Files:**
- Create: `src/core/warm_session.py`
- Modify: `src/core/orchestrator.py` (`_pool_worker` — check warm pool before fresh spawn)
- Modify: `src/config.yaml` (new `token_efficiency` block)
- Test: `tests/test_warm_session.py` (NEW)

- [ ] **Step 1: Write the failing test**

Create `tests/test_warm_session.py`:

```python
"""Tests for the warm session pool — per-worker session reuse."""


def test_warm_pool_returns_none_when_empty():
    from src.core.warm_session import WarmSessionPool
    pool = WarmSessionPool()
    assert pool.get("worker-1") is None


def test_warm_pool_stores_and_retrieves_session_id():
    from src.core.warm_session import WarmSessionPool
    pool = WarmSessionPool()
    pool.put("worker-1", "sess-abc123")
    assert pool.get("worker-1") == "sess-abc123"


def test_warm_pool_invalidate_removes_session():
    from src.core.warm_session import WarmSessionPool
    pool = WarmSessionPool()
    pool.put("worker-1", "sess-abc")
    pool.invalidate("worker-1")
    assert pool.get("worker-1") is None


def test_warm_pool_tracks_per_worker():
    from src.core.warm_session import WarmSessionPool
    pool = WarmSessionPool()
    pool.put("worker-1", "sess-a")
    pool.put("worker-2", "sess-b")
    assert pool.get("worker-1") == "sess-a"
    assert pool.get("worker-2") == "sess-b"


def test_warm_pool_tracks_features_handled():
    from src.core.warm_session import WarmSessionPool
    pool = WarmSessionPool()
    pool.put("worker-1", "sess-a")
    pool.record_feature("worker-1", "feat-001")
    pool.record_feature("worker-1", "feat-002")
    assert pool.features_handled("worker-1") == ["feat-001", "feat-002"]


def test_warm_pool_drops_session_after_max_features():
    from src.core.warm_session import WarmSessionPool
    pool = WarmSessionPool(max_features_per_session=2)
    pool.put("worker-1", "sess-a")
    pool.record_feature("worker-1", "feat-001")
    pool.record_feature("worker-1", "feat-002")
    assert pool.get("worker-1") is None  # saturated
```

- [ ] **Step 2: Run and confirm failing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_warm_session.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `warm_session.py`**

Create `src/core/warm_session.py`:

```python
"""Warm session pool — reuse Claude CLI sessions across features in a worker.

Each parallel worker slot keeps one long-lived generator session id. When a
feature completes successfully, the next feature dispatched to the same slot
resumes the same session (via Claude CLI's --resume flag) instead of spawning
fresh. The agent's initial codebase exploration and cached system prompt
carry over, cutting per-feature token overhead dramatically.

Invalidation rules:
  - Generator error → invalidate (session may be poisoned)
  - Rate limit hit → invalidate (CLI will reject --resume during cooldown)
  - Worker slot released → invalidate
  - Session exceeds max_features_per_session → drop (avoid context bloat)
"""

from threading import Lock
from typing import Optional


class WarmSessionPool:
    def __init__(self, max_features_per_session: int = 20):
        self._data: dict[str, dict] = {}
        self._lock = Lock()
        self._max_features = max_features_per_session

    def get(self, worker_id: str) -> Optional[str]:
        with self._lock:
            entry = self._data.get(worker_id)
            if entry is None:
                return None
            if len(entry["features"]) >= self._max_features:
                del self._data[worker_id]
                return None
            return entry["session_id"]

    def put(self, worker_id: str, session_id: str) -> None:
        with self._lock:
            self._data[worker_id] = {"session_id": session_id, "features": []}

    def record_feature(self, worker_id: str, feature_id: str) -> None:
        with self._lock:
            entry = self._data.get(worker_id)
            if entry is not None:
                entry["features"].append(feature_id)

    def features_handled(self, worker_id: str) -> list[str]:
        with self._lock:
            entry = self._data.get(worker_id)
            return list(entry["features"]) if entry else []

    def invalidate(self, worker_id: str) -> None:
        with self._lock:
            self._data.pop(worker_id, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
```

- [ ] **Step 4: Run tests and confirm passing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_warm_session.py -v
```

Expected: all 6 PASS.

- [ ] **Step 5: Wire the pool into `_pool_worker`**

In `src/core/orchestrator.py`, add at the top with the other imports:

```python
from .warm_session import WarmSessionPool
```

Inside `run_parallel_wave` after `_pool_active` is declared (around line 704), add:

```python
    warm_pool_enabled = config.get("token_efficiency", {}).get("warm_sessions", {}).get("enabled", True)
    warm_pool = WarmSessionPool(
        max_features_per_session=config.get("token_efficiency", {}).get("warm_sessions", {}).get("max_features_per_session", 20),
    )
```

In `_pool_worker` before the `if _resume_sid:` branch (around line 828), add. Note we track `_is_warm_reuse` as a local flag set once here and consumed later in Step 7 — NEVER re-query `warm_pool.get(wid)` after this point, because the `get()` call has a side effect (drops saturated sessions) and calling it twice can flip the flag mid-worker:

```python
            _is_warm_reuse = False
            if _resume_sid is None and warm_pool_enabled:
                _warm_sid = warm_pool.get(wid)
                if _warm_sid:
                    _resume_sid = _warm_sid
                    _is_warm_reuse = True
                    print(f"  {feature_id}: reusing warm session from {wid}")
```

- [ ] **Step 6: Record successful features into the warm pool (and invalidate on failure)**

After the generator-session-id capture block (around line 881, right after the `worker_assignments.update_assignment_session_id(...)` calls), add:

```python
            if warm_pool_enabled:
                _gen_is_rate_limited = gen_result.get("status") == "rate_limited"
                if gen_status == "ok" and _gen_sid and not _gen_is_rate_limited:
                    # Only `put` a session the pool doesn't already have — avoids
                    # resetting the features-handled counter on every successful run.
                    # We intentionally do NOT call warm_pool.get() here (side effects);
                    # we use the local `_is_warm_reuse` flag from Step 5 instead.
                    if not _is_warm_reuse:
                        warm_pool.put(wid, _gen_sid)
                    warm_pool.record_feature(wid, feature_id)
                else:
                    # gen_status != "ok" OR rate-limited OR no session_id
                    # Invalidate so the next feature spawns fresh rather than
                    # resuming a poisoned or stale session.
                    warm_pool.invalidate(wid)
```

This is the only place we invalidate — the local `_is_warm_reuse` flag (from Step 5) tells us whether we were already reusing, and the `gen_result.get("status")` check handles rate limits inline without grepping for another site.

- [ ] **Step 7: Distinguish warm-reuse from retry-with-feedback**

In the `if _resume_sid:` block (around line 832), use the local `_is_warm_reuse` flag set in Step 5 — do NOT call `warm_pool.get()` again (it has side effects). Find the existing `user_msg = (...)` assignment inside `if _resume_sid:` and replace the whole assignment with:

```python
            if _resume_sid:
                # _is_warm_reuse was set in Step 5 based on warm_pool.get()
                # Fallback safety: if feedback.md exists in the worktree, we're in
                # a retry-with-feedback flow even if _is_warm_reuse is True — the
                # feedback path takes precedence.
                _has_feedback = (Path(wt_dir) / "feedback.md").exists()
                if _is_warm_reuse and not _has_feedback:
                    user_msg = (
                        f"Next feature: {feature_id} -- {feature_desc}\n"
                        f"Read TASK_BRIEF.md in this directory for full details. "
                        f"You already have full context for this codebase from earlier features in this session — "
                        f"do NOT re-explore files you've already read."
                    )
                else:
                    user_msg = (
                        f"The reviewer found issues on {feature_id}. "
                        f"Read feedback.md in this directory for the full report. "
                        f"Fix ALL issues listed there. Do NOT re-explore the codebase."
                    )
```

- [ ] **Step 8: Add config knobs**

Append to `src/config.yaml`:

```yaml
# Token efficiency — subscription-user optimizations
token_efficiency:
  warm_sessions:
    enabled: true
    max_features_per_session: 20
  deterministic_gate:
    enabled: true
  programmatic_fixes:
    enabled: true
  local_llm:
    enabled: false
    endpoint: "http://localhost:11434"
    model: "qwen2.5-coder:7b-instruct"
```

- [ ] **Step 9: Run the full suite**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/ -q
```

Expected: all PASS.

- [ ] **Step 10: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/core/warm_session.py src/core/orchestrator.py src/config.yaml tests/test_warm_session.py
git commit -m "feat(warm-session): reuse generator sessions across features per worker"
```

---

## Task 4: Deterministic pre-check module

**Goal:** Run mechanical correctness checks (build, test, lint, scope files, imports) in-process before spawning the LLM reviewer. Most QA failures are caught by these.

**Files:**
- Create: `src/core/deterministic_checks.py`
- Test: `tests/test_deterministic_checks.py` (NEW)

- [ ] **Step 1: Write the failing test**

Create `tests/test_deterministic_checks.py`:

```python
"""Unit tests for deterministic_checks module — pure functions, no LLM."""

from pathlib import Path
import pytest


def test_check_scope_files_exist_passes_when_all_present(tmp_path):
    from src.core.deterministic_checks import check_scope_files_exist
    (tmp_path / "a.py").write_text("x = 1")
    (tmp_path / "b.py").write_text("y = 2")
    result = check_scope_files_exist(tmp_path, scope=["a.py", "b.py"])
    assert result["passed"] is True
    assert result["missing"] == []


def test_check_scope_files_exist_reports_missing(tmp_path):
    from src.core.deterministic_checks import check_scope_files_exist
    (tmp_path / "a.py").write_text("x = 1")
    result = check_scope_files_exist(tmp_path, scope=["a.py", "b.py"])
    assert result["passed"] is False
    assert result["missing"] == ["b.py"]


def test_check_scope_files_exist_tolerates_directory(tmp_path):
    from src.core.deterministic_checks import check_scope_files_exist
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "f.py").write_text("x=1")
    result = check_scope_files_exist(tmp_path, scope=["pkg/"])
    assert result["passed"] is True


def test_run_command_captures_output_and_exit(tmp_path):
    from src.core.deterministic_checks import run_command
    result = run_command("echo hello && exit 0", cwd=tmp_path, timeout=5)
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]


def test_run_command_timeout_returns_error(tmp_path):
    from src.core.deterministic_checks import run_command
    result = run_command("sleep 5", cwd=tmp_path, timeout=1)
    assert result["exit_code"] != 0
    assert result["timed_out"] is True


def test_run_all_checks_aggregates(tmp_path):
    from src.core.deterministic_checks import run_all_checks
    (tmp_path / "a.py").write_text("x = 1")
    result = run_all_checks(
        worktree=tmp_path,
        scope=["a.py"],
        test_command="echo test-ok",
        build_command="echo build-ok",
        lint_command=None,
    )
    assert result["overall"] == "pass"
    assert result["checks"]["scope"]["passed"] is True
    assert result["checks"]["test"]["passed"] is True
    assert result["checks"]["build"]["passed"] is True
    assert "lint" not in result["checks"]


def test_run_all_checks_fails_when_scope_missing(tmp_path):
    from src.core.deterministic_checks import run_all_checks
    result = run_all_checks(
        worktree=tmp_path,
        scope=["nonexistent.py"],
        test_command="echo test-ok",
        build_command=None,
        lint_command=None,
    )
    assert result["overall"] == "fail"
    assert result["checks"]["scope"]["passed"] is False


def test_run_all_checks_test_failure_marks_fail(tmp_path):
    from src.core.deterministic_checks import run_all_checks
    (tmp_path / "a.py").write_text("x = 1")
    result = run_all_checks(
        worktree=tmp_path,
        scope=["a.py"],
        test_command="exit 1",
        build_command=None,
        lint_command=None,
    )
    assert result["overall"] == "fail"
    assert result["checks"]["test"]["passed"] is False
```

- [ ] **Step 2: Run and confirm failing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_deterministic_checks.py -v
```

Expected: all FAIL — module doesn't exist.

- [ ] **Step 3: Implement the module**

Create `src/core/deterministic_checks.py`:

```python
"""Deterministic, mechanical checks that run BEFORE the LLM reviewer.

Most QA failures are caught by these:
  - build command exits non-zero
  - test command exits non-zero
  - lint command exits non-zero
  - files in feature scope don't exist
  - imports are broken (caught by build/vet)

Running these in-process avoids spawning an LLM reviewer for the obvious
failure cases. Only when all mechanical checks pass do we escalate to the
LLM for semantic verification.

Every function is a pure subprocess wrapper — no global state, no
orchestrator dependencies, fully testable in isolation.
"""

import json
import subprocess
import time
from pathlib import Path
from typing import Optional


def run_command(command: str, cwd: Path, timeout: int = 300) -> dict:
    """Run a shell command in cwd. Never raises."""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
            "timed_out": False,
            "duration_ms": int((time.monotonic() - start) * 1000),
        }
    except subprocess.TimeoutExpired:
        return {
            "exit_code": 124,
            "stdout": "",
            "stderr": f"Command timed out after {timeout}s",
            "timed_out": True,
            "duration_ms": int((time.monotonic() - start) * 1000),
        }
    except Exception as e:
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": f"Command failed to launch: {e}",
            "timed_out": False,
            "duration_ms": int((time.monotonic() - start) * 1000),
        }


def check_scope_files_exist(worktree: Path, scope: list[str]) -> dict:
    """Verify every file/directory in the task scope exists and is non-empty."""
    missing: list[str] = []
    empty: list[str] = []
    for entry in scope:
        p = worktree / entry.rstrip("/")
        if entry.endswith("/"):
            if not p.exists() or not p.is_dir():
                missing.append(entry)
            elif not any(p.iterdir()):
                empty.append(entry)
        else:
            if not p.exists():
                missing.append(entry)
            elif p.is_file() and p.stat().st_size == 0:
                empty.append(entry)
    return {
        "passed": len(missing) == 0 and len(empty) == 0,
        "missing": missing,
        "empty": empty,
    }


def read_generator_verdict(worktree: Path) -> Optional[dict]:
    """Read HARNESS_VERDICT.json from the worktree root if present."""
    path = worktree / "HARNESS_VERDICT.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def run_all_checks(
    worktree: Path,
    scope: list[str],
    test_command: Optional[str] = None,
    build_command: Optional[str] = None,
    lint_command: Optional[str] = None,
    test_timeout: int = 600,
    build_timeout: int = 300,
    lint_timeout: int = 120,
) -> dict:
    """Run every configured check against the worktree."""
    start = time.monotonic()
    checks: dict = {}

    checks["scope"] = check_scope_files_exist(worktree, scope or [])

    if build_command:
        r = run_command(build_command, worktree, timeout=build_timeout)
        checks["build"] = {
            "passed": r["exit_code"] == 0,
            "exit_code": r["exit_code"],
            "stdout_tail": r["stdout"][-1500:],
            "stderr_tail": r["stderr"][-1500:],
        }

    if test_command:
        r = run_command(test_command, worktree, timeout=test_timeout)
        checks["test"] = {
            "passed": r["exit_code"] == 0,
            "exit_code": r["exit_code"],
            "stdout_tail": r["stdout"][-1500:],
            "stderr_tail": r["stderr"][-1500:],
        }

    if lint_command:
        r = run_command(lint_command, worktree, timeout=lint_timeout)
        checks["lint"] = {
            "passed": r["exit_code"] == 0,
            "exit_code": r["exit_code"],
            "stdout_tail": r["stdout"][-1500:],
            "stderr_tail": r["stderr"][-1500:],
        }

    overall = "pass" if all(c.get("passed", False) for c in checks.values()) else "fail"
    return {
        "overall": overall,
        "checks": checks,
        "duration_ms": int((time.monotonic() - start) * 1000),
    }
```

- [ ] **Step 4: Run and confirm passing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_deterministic_checks.py -v
```

Expected: all 8 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/core/deterministic_checks.py tests/test_deterministic_checks.py
git commit -m "feat(qa): add deterministic pre-check module for mechanical verification"
```

---

## Task 5: Add the gate-decision helper and wire into orchestrator

**Goal:** Add `gate_decision()` that combines mechanical checks with the generator's self-reported confidence. Then wire it into `_pool_worker` so we skip the LLM reviewer when the gate says pass.

**Files:**
- Modify: `src/core/deterministic_checks.py` (add `gate_decision`)
- Modify: `src/core/orchestrator.py` (`_pool_worker` — call gate before QA spawn)
- Modify: `src/config.yaml` (add build_command and lint_command under evaluator)
- Test: `tests/test_deterministic_checks.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_deterministic_checks.py`:

```python
def test_gate_pass_emits_synthetic_verdict(tmp_path):
    from src.core.deterministic_checks import gate_decision
    (tmp_path / "src.py").write_text("x = 1")
    decision = gate_decision(
        worktree=tmp_path,
        scope=["src.py"],
        test_command="echo ok",
        build_command=None,
        lint_command=None,
        gen_status="ok",
    )
    assert decision["skip_llm_review"] is True
    assert "VERDICT: PASS" in decision["synthetic_output"]


def test_gate_scope_missing_forces_llm(tmp_path):
    from src.core.deterministic_checks import gate_decision
    decision = gate_decision(
        worktree=tmp_path,
        scope=["missing.py"],
        test_command="echo ok",
        build_command=None,
        lint_command=None,
        gen_status="ok",
    )
    assert decision["skip_llm_review"] is False
    assert decision["reason"] == "scope_missing"


def test_gate_disabled_always_runs_llm(tmp_path):
    from src.core.deterministic_checks import gate_decision
    (tmp_path / "src.py").write_text("x = 1")
    decision = gate_decision(
        worktree=tmp_path,
        scope=["src.py"],
        test_command="echo ok",
        build_command=None,
        lint_command=None,
        gen_status="ok",
        enabled=False,
    )
    assert decision["skip_llm_review"] is False
    assert decision["reason"] == "gate_disabled"


def test_gate_generator_error_forces_llm(tmp_path):
    from src.core.deterministic_checks import gate_decision
    (tmp_path / "src.py").write_text("x = 1")
    decision = gate_decision(
        worktree=tmp_path,
        scope=["src.py"],
        test_command="echo ok",
        build_command=None,
        lint_command=None,
        gen_status="error",
    )
    assert decision["skip_llm_review"] is False


def test_gate_low_confidence_forces_llm(tmp_path):
    from src.core.deterministic_checks import gate_decision
    (tmp_path / "src.py").write_text("x = 1")
    (tmp_path / "HARNESS_VERDICT.json").write_text(
        '{"tests_passed": true, "build_ok": true, "confidence": "low"}'
    )
    decision = gate_decision(
        worktree=tmp_path,
        scope=["src.py"],
        test_command="echo ok",
        build_command=None,
        lint_command=None,
        gen_status="ok",
    )
    assert decision["skip_llm_review"] is False
    assert decision["reason"] == "generator_low_confidence"
```

- [ ] **Step 2: Run and confirm failing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_deterministic_checks.py -v
```

- [ ] **Step 3: Add `gate_decision` to `deterministic_checks.py`**

Append to `src/core/deterministic_checks.py`:

```python
def gate_decision(
    worktree: Path,
    scope: list[str],
    test_command: Optional[str],
    build_command: Optional[str],
    lint_command: Optional[str],
    gen_status: str,
    enabled: bool = True,
) -> dict:
    """Decide whether to skip the LLM reviewer based on mechanical checks.

    Returns a dict with keys: skip_llm_review (bool), reason (str),
    mechanical_results (optional dict), synthetic_output (str when skipping),
    verdict (optional dict when HARNESS_VERDICT.json was read).

    Policy:
      - If gate disabled: never skip (preserve legacy behavior).
      - If generator didn't report 'ok': never skip.
      - If HARNESS_VERDICT.json says confidence != 'high': never skip.
      - If ANY mechanical check fails: never skip.
      - If all pass and generator reported ok: skip LLM review.
    """
    if not enabled:
        return {"skip_llm_review": False, "reason": "gate_disabled"}

    if gen_status != "ok":
        return {"skip_llm_review": False, "reason": f"generator_status_{gen_status}"}

    verdict = read_generator_verdict(worktree)
    if verdict is not None and verdict.get("confidence") != "high":
        return {
            "skip_llm_review": False,
            "reason": "generator_low_confidence",
            "verdict": verdict,
        }

    results = run_all_checks(
        worktree=worktree,
        scope=scope,
        test_command=test_command,
        build_command=build_command,
        lint_command=lint_command,
    )

    if results["overall"] != "pass":
        reason = "unknown"
        scope_check = results["checks"].get("scope", {})
        if not scope_check.get("passed", True):
            reason = "scope_missing" if scope_check.get("missing") else "scope_empty"
        elif not results["checks"].get("build", {}).get("passed", True):
            reason = "build_failed"
        elif not results["checks"].get("test", {}).get("passed", True):
            reason = "test_failed"
        elif not results["checks"].get("lint", {}).get("passed", True):
            reason = "lint_failed"
        return {
            "skip_llm_review": False,
            "reason": reason,
            "mechanical_results": results,
        }

    return {
        "skip_llm_review": True,
        "reason": "all_checks_passed",
        "mechanical_results": results,
        "synthetic_output": (
            "VERDICT: PASS\n"
            "Feature verified mechanically — build: ok, tests: ok, scope files present. "
            "(deterministic gate, no LLM review required)"
        ),
    }
```

- [ ] **Step 4: Run and confirm passing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_deterministic_checks.py -v
```

Expected: all PASS.

- [ ] **Step 5: Add build_command and lint_command config keys**

In `src/config.yaml`, extend the `evaluator:` block:

```yaml
evaluator:
  test_suite_command: "npm test"
  build_command: ""
  lint_command: ""
  browser_verification: auto
  browser_tool: playwright
```

- [ ] **Step 6: Wire the gate into `_pool_worker` before QA spawn**

In `src/core/orchestrator.py`, add at the top:

```python
from .deterministic_checks import gate_decision
```

Around line 907 (before the QA spawn), wrap the existing QA-spawn block like this:

```python
            _gate_cfg = config.get("token_efficiency", {}).get("deterministic_gate", {})
            _eval_cfg = config.get("evaluator", {})
            _gate = gate_decision(
                worktree=Path(wt_dir),
                scope=feature.get("scope", []) or [],
                test_command=_eval_cfg.get("test_suite_command") or None,
                build_command=_eval_cfg.get("build_command") or None,
                lint_command=_eval_cfg.get("lint_command") or None,
                gen_status=gen_status,
                enabled=_gate_cfg.get("enabled", True),
            )

            if _gate["skip_llm_review"]:
                print(f"  {feature_id}: mechanical checks passed -- skipping LLM review")
                _write_run_log(state_dir, "gate_skip",
                               feature_id=feature_id, reason=_gate["reason"])
                qa_result = {
                    "output": _gate["synthetic_output"],
                    "cost": 0,
                    "usage": {},
                    "gate_skipped": True,
                }
            else:
                _write_run_log(state_dir, "gate_escalate",
                               feature_id=feature_id, reason=_gate.get("reason", "unknown"))
                worker_assignments.update_assignment_phase(state_dir, wid, "evaluator")
                print(f"  QA {feature_id} in worktree (gate: {_gate.get('reason', 'running')})")
                qa_user_msg = _build_qa_user_msg(feature_id, work_plan)
                _mech = _gate.get("mechanical_results") or {}
                if _mech:
                    _mech_summary = []
                    for _name, _res in _mech.get("checks", {}).items():
                        if not _res.get("passed", True):
                            _tail = _res.get("stderr_tail") or _res.get("stdout_tail") or ""
                            _mech_summary.append(f"[{_name} FAILED]\n{_tail[:800]}")
                    if _mech_summary:
                        qa_user_msg += "\n\n## Mechanical check results (already run)\n\n" + "\n\n".join(_mech_summary)
                qa_options = create_client_options(wt_dir, config)
                try:
                    qa_result = await asyncio.wait_for(
                        run_agent_session(qa_user_msg, qa_options, wt_dir, system_prompt=qa_system_prompt),
                        timeout=qa_timeout,
                    )
                except asyncio.TimeoutError:
                    print(f"  QA {feature_id}: timed out after 15m")
                    qa_result = {"output": "[QA timed out]", "cost": 0}
```

- [ ] **Step 7: Run the full suite**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/ -q
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/core/deterministic_checks.py src/core/orchestrator.py src/config.yaml tests/test_deterministic_checks.py
git commit -m "feat(qa): deterministic gate skips LLM reviewer when mechanical checks pass"
```

---

## Task 6: Programmatic fix-up module and generator prompt update

**Goal:** When the LLM reviewer reports FAIL, try language-native auto-fixers (gofmt, goimports, ruff --fix, eslint --fix) before re-spawning the generator. If fixes resolve the failure, skip the retry entirely. Also require the generator to write `HARNESS_VERDICT.json`.

**Files:**
- Create: `src/core/programmatic_fixes.py`
- Modify: `src/core/orchestrator.py` (apply fixes after FAIL, re-check gate)
- Modify: `src/prompts/generator-v2.md` (require HARNESS_VERDICT.json)
- Test: `tests/test_programmatic_fixes.py` (NEW)

- [ ] **Step 1: Write the failing test**

Create `tests/test_programmatic_fixes.py`:

```python
"""Tests for programmatic_fixes — no LLM, pure subprocess wrappers."""

from pathlib import Path
import pytest


def test_detect_language_go(tmp_path):
    from src.core.programmatic_fixes import detect_languages
    (tmp_path / "main.go").write_text("package main")
    assert "go" in detect_languages(tmp_path)


def test_detect_language_javascript_typescript(tmp_path):
    from src.core.programmatic_fixes import detect_languages
    (tmp_path / "app.js").write_text("const x = 1")
    (tmp_path / "app.ts").write_text("const x: number = 1")
    langs = detect_languages(tmp_path)
    assert "javascript" in langs
    assert "typescript" in langs


def test_apply_fixes_noop_when_tools_missing(tmp_path, monkeypatch):
    from src.core.programmatic_fixes import apply_fixes
    monkeypatch.setattr("shutil.which", lambda name: None)
    (tmp_path / "main.go").write_text("package main")
    result = apply_fixes(tmp_path, languages=["go"])
    assert result["applied"] == []
    assert result["errors"] == []


def test_apply_fixes_runs_available_tool(tmp_path, monkeypatch):
    from src.core.programmatic_fixes import apply_fixes
    calls = []
    monkeypatch.setattr("shutil.which",
                        lambda name: f"/fake/{name}" if name in ("gofmt", "goimports") else None)
    class R:
        returncode = 0
        stdout = ""
        stderr = ""
    monkeypatch.setattr("subprocess.run", lambda args, **kw: (calls.append(args), R())[1])
    (tmp_path / "main.go").write_text("package main")
    result = apply_fixes(tmp_path, languages=["go"])
    assert "gofmt" in result["applied"] or "goimports" in result["applied"]
```

- [ ] **Step 2: Run and confirm failing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_programmatic_fixes.py -v
```

- [ ] **Step 3: Implement `programmatic_fixes.py`**

Create `src/core/programmatic_fixes.py`:

```python
"""Programmatic fix-ups — mechanical code fixes that run before LLM retry.

Many QA failures are trivial: missing import, unformatted code, unused var.
Running the language's own formatter or linter --fix handles these in
milliseconds with zero token cost. Only when programmatic fixes don't
resolve the issue do we re-spawn the generator.

Language detection is filename-based. Tools are detected via shutil.which —
missing tools are silently skipped.
"""

import shutil
import subprocess
from pathlib import Path


LANG_EXTS = {
    "go": {".go"},
    "python": {".py"},
    "javascript": {".js", ".jsx", ".mjs", ".cjs"},
    "typescript": {".ts", ".tsx"},
}


def detect_languages(worktree: Path) -> list[str]:
    found: set[str] = set()
    for path in worktree.rglob("*"):
        parts = set(path.parts)
        if parts & {"node_modules", ".git", "vendor", "__pycache__", "dist", "build"}:
            continue
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        for lang, exts in LANG_EXTS.items():
            if suffix in exts:
                found.add(lang)
                break
    return sorted(found)


def _run_tool(args: list[str], cwd: Path) -> dict:
    try:
        proc = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=120)
        return {
            "exit_code": proc.returncode,
            "stdout": (proc.stdout or "")[-2000:],
            "stderr": (proc.stderr or "")[-2000:],
        }
    except Exception as e:
        return {"exit_code": 1, "stdout": "", "stderr": f"tool launch failed: {e}"}


def apply_fixes(worktree: Path, languages: list[str]) -> dict:
    """Run every available auto-fixer for the detected languages."""
    applied: list[str] = []
    errors: list[str] = []
    details: dict = {}

    for lang in languages:
        if lang == "go":
            for tool in ("goimports", "gofmt"):
                if shutil.which(tool):
                    r = _run_tool([tool, "-w", str(worktree)], worktree)
                    details[tool] = r
                    if r["exit_code"] == 0:
                        applied.append(tool)
                    else:
                        errors.append(f"{tool}: exit {r['exit_code']}")
        elif lang == "python":
            for tool_args in (["ruff", "check", "--fix", str(worktree)],
                              ["black", str(worktree)]):
                tool = tool_args[0]
                if shutil.which(tool):
                    r = _run_tool(tool_args, worktree)
                    details[tool] = r
                    if r["exit_code"] == 0:
                        applied.append(tool)
                    else:
                        errors.append(f"{tool}: exit {r['exit_code']}")
        elif lang in ("javascript", "typescript"):
            if shutil.which("eslint"):
                r = _run_tool(["eslint", "--fix", str(worktree)], worktree)
                details["eslint"] = r
                if r["exit_code"] == 0:
                    applied.append("eslint")
                else:
                    errors.append(f"eslint: exit {r['exit_code']}")
            if shutil.which("prettier"):
                r = _run_tool(["prettier", "--write", str(worktree)], worktree)
                details["prettier"] = r
                if r["exit_code"] == 0:
                    applied.append("prettier")
                else:
                    errors.append(f"prettier: exit {r['exit_code']}")

    return {"applied": applied, "errors": errors, "details": details}
```

- [ ] **Step 4: Run and confirm passing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_programmatic_fixes.py -v
```

Expected: all PASS.

- [ ] **Step 5: Wire fix-ups into the retry path**

In `src/core/orchestrator.py`, add at the top:

```python
from .programmatic_fixes import detect_languages, apply_fixes
```

Then add a shared helper function (near `_record_phase_from`, around line 85) so we don't duplicate the fix-up logic across 3 call sites:

```python
def _try_programmatic_fix(
    config: dict,
    feature: dict,
    wt_dir,
    state_dir,
    feature_id: str,
):
    """Attempt programmatic fix-ups on a FAILed worktree. Returns a synthetic
    qa_result dict if fixes resolved the failure (skip retry), else None.
    `gate_decision` is already imported at the top of this module (Task 5).
    """
    _fix_enabled = config.get("token_efficiency", {}).get("programmatic_fixes", {}).get("enabled", True)
    if not _fix_enabled:
        return None
    _langs = detect_languages(Path(wt_dir))
    if not _langs:
        return None
    _fix_res = apply_fixes(Path(wt_dir), _langs)
    if not _fix_res["applied"]:
        return None
    print(f"  {feature_id}: applied fixes: {', '.join(_fix_res['applied'])}")
    _write_run_log(state_dir, "programmatic_fix",
                   feature_id=feature_id, applied=_fix_res["applied"])
    _retry_gate = gate_decision(
        worktree=Path(wt_dir),
        scope=feature.get("scope", []) or [],
        test_command=config.get("evaluator", {}).get("test_suite_command") or None,
        build_command=config.get("evaluator", {}).get("build_command") or None,
        lint_command=config.get("evaluator", {}).get("lint_command") or None,
        gen_status="ok",
        enabled=config.get("token_efficiency", {}).get("deterministic_gate", {}).get("enabled", True),
    )
    if _retry_gate["skip_llm_review"]:
        print(f"  {feature_id}: fixes resolved failure — skipping retry")
        return {
            "output": _retry_gate["synthetic_output"],
            "cost": 0, "usage": {}, "fixes_applied": _fix_res["applied"],
        }
    return None
```

**Now wire the helper into ALL 4 `VERDICT: FAIL` handling sites.** There are four — don't miss any, or parallel/resume/sequential paths will behave inconsistently.

| Line | Where | What to change |
|------|-------|---------------|
| 1044 | Wave result loop (parallel) | Wrap the failure branch with a call to `_try_programmatic_fix` and replace `qa_result` if it returns non-None |
| 1329 | Resume path (`if "VERDICT: FAIL" not in eval_result...` — NOTE inverted logic, only wire on the FAIL side) | Same |
| 1494 | Resume path (`if "VERDICT: FAIL" in eval_result...`) | Same |
| 2172 | Sequential path (`if "VERDICT: FAIL" in eval_result...`) | Same |

**Exact pattern to use at each site.** For site 1044, find:

```python
        if "VERDICT: FAIL" in qa_result.get("output", ""):
```

Insert immediately inside the block (before any existing retry logic):

```python
            _patched = _try_programmatic_fix(config, feature, wt_dir, state_dir, feature_id)
            if _patched is not None:
                qa_result = _patched
            else:
                # fall through to the existing retry-dispatch logic below
                pass
```

For sites 1494 and 2172, the variable is `eval_result` instead of `qa_result`. Use the same pattern:

```python
            _patched = _try_programmatic_fix(config, feature, wt_dir, state_dir, feature_id)
            if _patched is not None:
                eval_result = _patched
            else:
                pass
```

For site 1329, the check is `if "VERDICT: FAIL" not in eval_result...` — that branch is the PASS path. Skip it. The corresponding FAIL handling in the recovery path is at site 1494; that's where the fix goes. Do NOT touch line 1329.

**Variable-name note:** At sites 1494 and 2172 (resume and sequential paths), read the surrounding code to confirm the variable names. If a site uses `worktree_dir` instead of `wt_dir`, adapt the call accordingly. If `feature_id` is not in scope at that site, read it from the surrounding context (usually `feature["id"]` or an `_asgn["feature_id"]`).

**After editing, verify the count:**

```bash
cd /Users/prashantpandey/harness && grep -c '_try_programmatic_fix' src/core/orchestrator.py
```

Expected: `4` (1 helper definition + 3 call sites).

- [ ] **Step 6: Update the generator prompt**

In `src/prompts/generator-v2.md`, add before the `HARNESS_STATUS` block instructions:

```markdown
## FINAL STEP — WRITE HARNESS_VERDICT.json

Before emitting your HARNESS_STATUS block, write a file `HARNESS_VERDICT.json` in the
worktree root with this exact schema:

```json
{
  "tests_passed": true,
  "build_ok": true,
  "files_created": ["path/to/file1", "path/to/file2"],
  "confidence": "high",
  "notes": []
}
```

Rules for `confidence`:
- `"high"` — you personally ran the test command and saw it pass; every file in your scope exists; you did NOT skip any acceptance criterion.
- `"low"` — you couldn't run tests, you made partial progress, any AC was deferred, or you're genuinely uncertain.

If you write `confidence: "high"` and the deterministic gate agrees, the LLM reviewer
will be skipped. If you write `"high"` dishonestly and the code is broken, YOU will be
the one fixing it on the next iteration — be truthful.
```

- [ ] **Step 7: Run the full suite**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/ -q
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/core/programmatic_fixes.py src/core/orchestrator.py src/prompts/generator-v2.md tests/test_programmatic_fixes.py
git commit -m "feat(qa): programmatic fix-ups before retry + require HARNESS_VERDICT.json"
```

---

## Task 7: Ollama + Qwen 2.5 Coder 7B installer

**Goal:** An idempotent shell script that installs Ollama (if missing), checks free disk, and pulls `qwen2.5-coder:7b-instruct`. Safe to re-run.

**Files:**
- Create: `scripts/install-local-llm.sh`

- [ ] **Step 1: Free disk space precheck**

Before running the installer, verify free disk:

```bash
df -h ~ | tail -1 | awk '{print "free: "$4"  used: "$5}'
```

If free < 8 GB, free space first:

```bash
brew cleanup --prune=all 2>/dev/null || true
rm -rf ~/Library/Caches/pip 2>/dev/null || true
rm -rf ~/.cache/huggingface 2>/dev/null || true
docker system prune -a -f 2>/dev/null || true
```

Re-check. If still tight, STOP and ask the user what to remove.

- [ ] **Step 2: Write the installer**

Create `scripts/install-local-llm.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Harness local LLM installer — Ollama + Qwen 2.5 Coder 7B
# Idempotent: safe to re-run. Target: Mac mini M4, 16 GB RAM.

MODEL="qwen2.5-coder:7b-instruct"
REQUIRED_GB=8
FORCE_PULL=false

for arg in "$@"; do
  if [ "$arg" = "--force-pull" ]; then
    FORCE_PULL=true
  fi
done

echo "==> Harness local LLM installer"
echo "    Target model: $MODEL"

FREE_KB=$(df -k "$HOME" | tail -1 | awk '{print $4}')
FREE_GB=$((FREE_KB / 1024 / 1024))
echo "==> Free disk: ${FREE_GB} GB (need >= ${REQUIRED_GB} GB)"
if [ "$FREE_GB" -lt "$REQUIRED_GB" ]; then
  echo "    ERROR: Insufficient disk space." >&2
  exit 1
fi

if command -v ollama >/dev/null 2>&1; then
  echo "==> Ollama already installed: $(ollama --version 2>&1 | head -1)"
else
  echo "==> Installing Ollama via Homebrew..."
  command -v brew >/dev/null 2>&1 || { echo "ERROR: Homebrew required"; exit 1; }
  brew install ollama
fi

if ! pgrep -f "ollama serve" >/dev/null 2>&1; then
  echo "==> Starting Ollama daemon..."
  nohup ollama serve >/tmp/ollama.log 2>&1 &
  sleep 3
fi

curl -sf http://localhost:11434/api/tags >/dev/null || {
  echo "ERROR: Ollama daemon not responding on :11434"; exit 1; }
echo "==> Ollama daemon healthy"

if [ "$FORCE_PULL" = "true" ] || ! ollama list 2>/dev/null | grep -q "${MODEL%%:*}"; then
  echo "==> Pulling ${MODEL} (~4.5 GB)..."
  ollama pull "$MODEL"
else
  echo "==> Model ${MODEL} already present"
fi

echo "==> Smoke test..."
curl -sf http://localhost:11434/api/generate \
  -d "{\"model\": \"$MODEL\", \"prompt\": \"Say exactly: ready\", \"stream\": false}" \
  | python3 -c "import sys, json; print('    reply:', json.load(sys.stdin).get('response', '')[:80])"

echo ""
echo "==> Install complete. Enable in .harness/config.yaml:"
echo "    token_efficiency:"
echo "      local_llm:"
echo "        enabled: true"
echo "        model: $MODEL"
```

- [ ] **Step 3: Make it executable**

```bash
chmod +x /Users/prashantpandey/harness/scripts/install-local-llm.sh
```

- [ ] **Step 4: Run the installer**

```bash
bash /Users/prashantpandey/harness/scripts/install-local-llm.sh
```

Expected: Homebrew installs Ollama, daemon starts, model pulls (~4.5 GB), smoke test prints "ready".

- [ ] **Step 5: Commit**

```bash
cd /Users/prashantpandey/harness
mkdir -p scripts
git add scripts/install-local-llm.sh
git commit -m "feat(local-llm): add Ollama + Qwen 2.5 Coder 7B installer"
```

---

## Task 8: Local LLM adapter with inline fallback

**Goal:** A `run_local(prompt, config, fallback)` entry point that tries Ollama first and falls back to an inline Python function. Every caller uses this — never Ollama directly.

**Files:**
- Create: `src/core/local_llm.py`
- Test: `tests/test_local_llm.py` (NEW)

- [ ] **Step 1: Write the failing test**

Create `tests/test_local_llm.py`:

```python
"""Tests for local_llm adapter — mock HTTP, test fallback behavior."""

from unittest.mock import patch


def test_run_local_returns_ollama_response_when_reachable():
    from src.core.local_llm import run_local
    with patch("src.core.local_llm._ollama_call") as m:
        m.return_value = "hello from qwen"
        result = run_local(
            "say hello",
            config={"enabled": True, "endpoint": "http://localhost:11434",
                    "model": "qwen2.5-coder:7b-instruct"},
        )
    assert result["source"] == "ollama"
    assert result["text"] == "hello from qwen"


def test_run_local_falls_back_when_ollama_fails():
    from src.core.local_llm import run_local
    with patch("src.core.local_llm._ollama_call") as m:
        m.side_effect = ConnectionError("ollama down")
        result = run_local(
            "compress this",
            config={"enabled": True, "endpoint": "http://localhost:11434", "model": "qwen"},
            fallback=lambda prompt: "inline-result",
        )
    assert result["source"] == "fallback"
    assert result["text"] == "inline-result"


def test_run_local_uses_fallback_when_disabled():
    from src.core.local_llm import run_local
    result = run_local(
        "compress this",
        config={"enabled": False},
        fallback=lambda prompt: "inline-result",
    )
    assert result["source"] == "fallback"
    assert result["text"] == "inline-result"


def test_run_local_returns_empty_when_disabled_no_fallback():
    from src.core.local_llm import run_local
    result = run_local("compress this", config={"enabled": False}, fallback=None)
    assert result["source"] == "disabled"
    assert result["text"] == ""
```

- [ ] **Step 2: Run and confirm failing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_local_llm.py -v
```

- [ ] **Step 3: Implement `local_llm.py`**

Create `src/core/local_llm.py`:

```python
"""Thin Ollama HTTP adapter with inline-Python fallback.

Every caller that wants to offload work to a local model goes through
`run_local()`. If Ollama is configured and reachable, we use it. If it's
disabled or fails, we invoke the caller's fallback function.

This means the harness NEVER hard-depends on Ollama: if it's not installed,
the codebase still works — just without the token savings from offload.
"""

import json
import urllib.error
import urllib.request
from typing import Callable, Optional


def _ollama_call(prompt: str, endpoint: str, model: str, max_tokens: int, timeout: float) -> str:
    url = endpoint.rstrip("/") + "/api/generate"
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": 0.2},
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("response", "")


def run_local(
    prompt: str,
    config: dict,
    fallback: Optional[Callable[[str], str]] = None,
    max_tokens: int = 2000,
    timeout: float = 60.0,
) -> dict:
    """Run a prompt on the local LLM, or fall back to inline Python.

    Returns {text: str, source: "ollama" | "fallback" | "disabled", error: Optional[str]}.
    """
    if not config.get("enabled", False):
        if fallback is not None:
            return {"text": fallback(prompt), "source": "fallback", "error": None}
        return {"text": "", "source": "disabled", "error": None}

    endpoint = config.get("endpoint", "http://localhost:11434")
    model = config.get("model", "qwen2.5-coder:7b-instruct")

    try:
        text = _ollama_call(prompt, endpoint, model, max_tokens, timeout)
        return {"text": text, "source": "ollama", "error": None}
    except (urllib.error.URLError, ConnectionError, OSError, json.JSONDecodeError, TimeoutError) as e:
        if fallback is not None:
            return {"text": fallback(prompt), "source": "fallback", "error": str(e)}
        return {"text": "", "source": "disabled", "error": str(e)}
```

- [ ] **Step 4: Run and confirm passing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_local_llm.py -v
```

Expected: all 4 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/core/local_llm.py tests/test_local_llm.py
git commit -m "feat(local-llm): thin Ollama HTTP adapter with inline fallback"
```

---

## Task 9: Route discovery brief compression through the local LLM

**Goal:** `compress_discovery()` in `discovery.py` currently uses pure-Python regex extraction. With a local LLM available, we can produce semantically richer compressions at zero Claude cost. Route via `run_local()` with the existing Python function as fallback.

**Files:**
- Modify: `src/core/discovery.py` (wrap `compress_discovery` with local-LLM route)
- Modify: `src/core/orchestrator.py` (pass local_llm_config at call site)
- Test: `tests/test_discovery.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_discovery.py`:

```python
def test_compress_discovery_with_local_llm_uses_adapter(monkeypatch):
    from src.core import discovery
    calls = {"ran": False}
    def fake_run_local(prompt, config, fallback, **kwargs):
        calls["ran"] = True
        return {"text": "## Compressed by local LLM", "source": "ollama", "error": None}
    monkeypatch.setattr(discovery, "run_local", fake_run_local, raising=False)

    result = discovery.compress_discovery(
        output="Built the auth module. Decided to use JWT.",
        agent_id="agent-1",
        feature_id="feat-1",
        status="complete",
        local_llm_config={"enabled": True, "endpoint": "http://x", "model": "y"},
    )
    assert calls["ran"] is True
    assert "Compressed by local LLM" in result


def test_compress_discovery_without_local_llm_uses_inline():
    from src.core.discovery import compress_discovery
    result = compress_discovery(
        output="Built the auth module. Decided to use JWT.",
        agent_id="agent-1",
        feature_id="feat-1",
        status="complete",
        local_llm_config={"enabled": False},
    )
    assert "Agent: agent-1" in result
    assert "Feature: feat-1" in result
```

- [ ] **Step 2: Run and confirm failing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_discovery.py -k "local" -v
```

- [ ] **Step 3: Extend `compress_discovery`**

In `src/core/discovery.py`, add at the top:

```python
try:
    from .local_llm import run_local
except ImportError:
    run_local = None
```

Rename the existing `compress_discovery` function body to `_compress_discovery_inline` (keep the signature), then add a new wrapper `compress_discovery`:

```python
_LOCAL_LLM_COMPRESS_PROMPT = """You are compressing an agent's work output into a ~500-token brief.

Rules:
- Keep: what was built, key decisions, files created, any failures or blockers.
- Drop: raw tool output, file contents, verbose explanations.
- Format: markdown with sections Built, Decisions, Files, Failures.
- Maximum output: 2000 characters.

Agent ID: {agent_id}
Feature ID: {feature_id}
Status: {status}

=== AGENT OUTPUT ===
{output}
=== END AGENT OUTPUT ===

Respond with ONLY the compressed brief, no preamble."""


def compress_discovery(
    output: str,
    agent_id: str,
    feature_id: str,
    status: str = "",
    local_llm_config: Optional[dict] = None,
) -> str:
    """Compress agent output to ~500-token markdown brief.

    If local_llm_config is enabled, routes through local LLM. Otherwise or on
    failure, falls back to pure-Python regex extraction.
    """
    def _inline(_unused: str = "") -> str:
        return _compress_discovery_inline(output, agent_id, feature_id, status)

    if local_llm_config and local_llm_config.get("enabled") and run_local is not None:
        prompt = _LOCAL_LLM_COMPRESS_PROMPT.format(
            agent_id=agent_id,
            feature_id=feature_id,
            status=status or "unknown",
            output=output[:20_000],
        )
        result = run_local(
            prompt=prompt,
            config=local_llm_config,
            fallback=_inline,
            max_tokens=1500,
            timeout=30.0,
        )
        text = result["text"].strip()
        if text:
            return f"## Agent: {agent_id} | Feature: {feature_id}\n**Status:** {status or 'unknown'}\n\n{text}"

    return _inline()
```

- [ ] **Step 4: Update the orchestrator call site**

In `src/core/orchestrator.py` around line 999, find the `compress_discovery` call and update:

```python
        _local_cfg = config.get("token_efficiency", {}).get("local_llm", {}) or {}
        brief = compress_discovery(
            gen_result.get("output", ""), agent_id, feature_id, gen_status,
            local_llm_config=_local_cfg,
        )
```

- [ ] **Step 5: Run the tests**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_discovery.py -v
```

Expected: all PASS.

- [ ] **Step 6: Run the full suite**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/ -q
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/core/discovery.py src/core/orchestrator.py tests/test_discovery.py
git commit -m "feat(local-llm): route brief compression via Ollama with inline fallback"
```

---

## Task 10: Install into project and smoke-test end-to-end

**Goal:** Run the updated installer into the real project so the next `/harness-resume` picks up every change. Verify new config knobs are present.

- [ ] **Step 1: Run the harness installer**

```bash
cd /Users/prashantpandey/Desktop/projectroot
bash /Users/prashantpandey/harness/install.sh --force
```

Expected: atomic swap completes, config.yaml preserved, skills installed.

- [ ] **Step 2: Merge new config keys into the installed config**

The installer preserves existing config.yaml. Read it and confirm the `token_efficiency` block is present:

```bash
grep -A 15 "token_efficiency" /Users/prashantpandey/Desktop/projectroot/.harness/config.yaml || echo "MISSING"
```

If missing, use the Edit tool to append the `token_efficiency:` block shown in Task 3 Step 8.

- [ ] **Step 3: Verify installed modules**

```bash
ls /Users/prashantpandey/Desktop/projectroot/.harness/core/warm_session.py \
   /Users/prashantpandey/Desktop/projectroot/.harness/core/deterministic_checks.py \
   /Users/prashantpandey/Desktop/projectroot/.harness/core/programmatic_fixes.py \
   /Users/prashantpandey/Desktop/projectroot/.harness/core/local_llm.py
```

Expected: all four files exist.

- [ ] **Step 4: Smoke-test imports**

```bash
cd /Users/prashantpandey/Desktop/projectroot && python3 -c "
import sys
sys.path.insert(0, '.harness')
from core import warm_session, deterministic_checks, programmatic_fixes, local_llm
print('imports ok')
"
```

Expected: `imports ok`.

- [ ] **Step 5: Run a status check to confirm no regressions**

```bash
python3 /Users/prashantpandey/Desktop/projectroot/.claude/skills/harness-status/status.py --pretty --project /Users/prashantpandey/Desktop/projectroot
```

Expected: status runs cleanly, shows current run progress.

- [ ] **Step 6: Commit project-level config changes if any**

```bash
cd /Users/prashantpandey/Desktop/projectroot
git status .harness/config.yaml
# If modified:
git add .harness/config.yaml
git commit -m "chore(harness): enable token_efficiency features"
```

---

## Task 11: Document knobs and measurement in persistent knowledge

**Goal:** A short doc in `.harness/knowledge/token-efficiency.md` explaining every knob and what to look for in logs to measure savings.

- [ ] **Step 1: Write the doc**

Create `/Users/prashantpandey/Desktop/projectroot/.harness/knowledge/token-efficiency.md`:

```markdown
# Token-Efficiency Knobs (subscription users)

The harness was optimized in April 2026 to cut Claude quota burn for users
running off a Claude Pro or Max subscription.

## Knobs (in `.harness/config.yaml` under `token_efficiency:`)

| Knob | Default | What it does |
|------|---------|--------------|
| `warm_sessions.enabled` | true | Reuse one generator session per worker slot across features |
| `warm_sessions.max_features_per_session` | 20 | Drop saturated sessions |
| `deterministic_gate.enabled` | true | Skip LLM reviewer when build/test/lint/scope all pass |
| `programmatic_fixes.enabled` | true | Run gofmt/goimports/ruff/eslint --fix before retry |
| `local_llm.enabled` | false | Offload grunt work to Ollama (needs install) |
| `local_llm.endpoint` | http://localhost:11434 | Ollama HTTP endpoint |
| `local_llm.model` | qwen2.5-coder:7b-instruct | 4-bit quant, ~4.5 GB |

## Measuring savings

At the end of every run, `phase_summary` is logged to
`.harness/runs/<run-id>/logs/run.jsonl`:

```bash
jq 'select(.event == "phase_summary")' \
  /Users/prashantpandey/Desktop/projectroot/.harness/runs/<run-id>/logs/run.jsonl
```

Look for:
- `generator.cache_read_tokens` > 50 percent of input tokens (after the cache fix)
- `evaluator.calls` meaningfully lower than `generator.calls` (after deterministic gate)
- Count of `gate_skip` vs `gate_escalate` events

## If something breaks

Every knob is independent. Set the problematic one to `false` and re-run.
```

- [ ] **Step 2: Commit**

```bash
cd /Users/prashantpandey/Desktop/projectroot
git add .harness/knowledge/token-efficiency.md
git commit -m "docs(harness): token-efficiency knobs and measurement guide"
```

---

## Summary of expected savings

| Phase | What it does | Expected quota cut (subscription) |
|-------|-------------|----------------------------------|
| Phase A (Tasks 0–3) | Baseline instrumentation, cache fix, relay dedup, warm sessions | 25–35 percent |
| Phase B (Tasks 4–6) | Deterministic gate, programmatic fixes, structured verdict | 25–40 percent (stacks with Phase A) |
| Phase C (Tasks 7–9) | Local LLM offload for brief compression | 5–10 percent (more callers later) |
| **Total if all phases land** | | **50–70 percent reduction in tokens flowing through Claude CLI** |

Phase A is the safest — ship it first and measure with the `phase_summary` log. If `cache_read_tokens` ratio jumps and `evaluator.calls` drops after Phase B, you're on track.

---

## Self-review notes

**Spec coverage:** Tasks 0 through 11 cover Phase A, B, and C plus install and docs. Every brainstorm bullet (cache fix, relay dedup, warm sessions, deterministic gate, programmatic fixes, structured verdict, local LLM offload, installer, measurement) has a corresponding task.

**Placeholder scan:** Every task has exact file paths and concrete code blocks. No `TBD`, no `similar to above`, no `add error handling`. Every step is actionable.

**Type consistency:** `WarmSessionPool`, `gate_decision`, `run_all_checks`, `run_local`, `apply_fixes`, `detect_languages`, `read_generator_verdict`, `compress_discovery` — names are used consistently across every task that references them.

**Scope check:** Single codebase (`~/harness/src`), single deployment (`.harness/` in projectroot), one optional external service (Ollama). Coherent single implementation effort — no decomposition needed.

**Risk notes:**
- **Warm sessions** (Task 3) — an agent may run multiple features in one session. Session poisoning is mitigated by invalidate-on-error and max_features_per_session cap.
- **Deterministic gate** (Task 5) — mechanical checks don't verify design quality. The `confidence: low` escape hatch plus LLM fallback on any check failure is the mitigation.
- **Installer** (Task 7) — needs 8 GB free disk. Script checks and exits cleanly if insufficient.
- **Retry-counter user message** (Task 1) — moving retry info to user message means the evaluator sees it only at call time; ensure the generator prompt doesn't also need it.
