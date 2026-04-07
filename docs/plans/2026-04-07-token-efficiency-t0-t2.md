# Token Efficiency — T0 + T1 + T2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship three low-risk token-efficiency wins in the harness — per-session token telemetry, prompt-cache preservation on evaluator retries, and per-wave relay-context deduplication — without changing any decision logic, so the in-flight `run-legacy-migrated` run remains safe to resume.

**Architecture:** Three independent changes, three separate commits, one branch. T0 adds a JSONL log inside `CostTracker.record()` so we can prove every subsequent optimization works. T1 removes the `retry_count` value from the evaluator *system prompt* (where it busts the prompt cache) and either drops it or moves it to the user message. T2 caches the per-wave discovery relay so parallel workers don't rebuild the same 15KB string N times, and adds cheap hash-based dedup so identical briefs are not concatenated twice.

**Tech Stack:** Python 3.11+, Claude Agent SDK, Claude CLI (`--resume`), existing harness modules (`cost_tracker.py`, `discovery.py`, `orchestrator.py`). No new dependencies.

**Branch:** `feature/token-efficiency-t0-t2` off current master.

**Scope guardrail — what this plan does NOT touch:**
- Orchestrator VERDICT decision logic (preserved bit-for-bit)
- State file schemas (`session.json`, `state.json`, `feature_list.json`, `work_plan.json`)
- Planner pipeline, evaluator grading rules, generator behavior
- Circuit breaker, rate-limit handling, worktree creation/merge
- Config file defaults (except adding one optional feature flag off by default)

---

## File Structure

### Files modified
- `src/core/cost_tracker.py` — add `log_path` field and JSONL append inside `record()`
- `src/core/orchestrator.py` — wire `log_path` at the two CostTracker construction sites, strip RETRY_COUNT/MAX_RETRIES from system replacements, swap in `_get_cached_relay` helper
- `src/core/discovery.py` — add optional hash-based brief-content dedup inside `get_relay_context()`
- `src/prompts/evaluator-v2.md` — remove or neutralize `{{RETRY_COUNT}}` / `{{MAX_RETRIES}}` template vars at lines 71 and 83

### Files created
- `docs/plans/2026-04-07-token-efficiency-t0-t2.md` — this file

### Files extended (tests added to existing files)
- `tests/test_cost_tracker.py` — append `TestCostTrackerLog` class with JSONL-emission tests
- `tests/test_discovery.py` — append hash-dedup tests for `get_relay_context()`
- `tests/test_orchestrator.py` — append `_get_cached_relay` invalidation tests
- `tests/test_eval_prompt_cache.py` — NEW file (evaluator prompt-cache stability tests — conceptually distinct enough to keep separate)

> NOTE on test layout: harness tests live at the flat `tests/*.py` level, not under `tests/core/`. Match existing convention: class-based `TestFoo` grouping, `from src.core.foo import Bar` imports, no conftest required for these tests.

### Responsibility
- `cost_tracker.py` owns per-session cost/token accumulation and now the JSONL log writer. One new method: `_append_log()`. One new field: `log_path`. Signature of `record()` grows by one optional kwarg: `phase`.
- `orchestrator.py` owns lifecycle. It constructs the tracker with `log_path`, passes phase labels through `record()` calls, and caches relay text.
- `discovery.py` owns relay concatenation; its `get_relay_context()` gains a content-hash check but keeps the same signature.

---

## Task 0: JSONL token telemetry inside CostTracker

**Why first:** Landing telemetry first lets us measure T1 and T2 directly. Without it, we ship fixes blind.

**Files:**
- Modify: `src/core/cost_tracker.py:1-143`
- Modify: `src/core/orchestrator.py:1575` (fresh construction) and `src/core/orchestrator.py:1620` (resume construction)
- Modify: `tests/test_cost_tracker.py` (append new test class)

### Steps

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cost_tracker.py` (file already exists with `TestCostTracker` class; keep the same style):

```python
import json

from src.core.cost_tracker import CostTracker


class TestCostTrackerLog:
    def test_record_without_log_path_does_not_write(self, tmp_path):
        tracker = CostTracker()  # no log_path
        tracker.record("generator", 0.12, usage={"input_tokens": 100, "output_tokens": 50})
        # No file should have been created anywhere in tmp_path
        assert list(tmp_path.iterdir()) == []

    def test_record_with_log_path_appends_jsonl(self, tmp_path):
        log = tmp_path / "token_log.jsonl"
        tracker = CostTracker(log_path=log)
        tracker.record(
            "generator", 0.12,
            usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 200,
                "cache_creation_input_tokens": 10,
            },
            duration_ms=1234,
            num_turns=5,
            phase="generator-first",
        )
        tracker.record(
            "evaluator", 0.05,
            usage={"input_tokens": 80, "output_tokens": 20},
            phase="evaluator",
        )

        lines = log.read_text().strip().split("\n")
        assert len(lines) == 2

        e1 = json.loads(lines[0])
        assert e1["agent"] == "generator"
        assert e1["phase"] == "generator-first"
        assert e1["cost_usd"] == 0.12
        assert e1["input_tokens"] == 100
        assert e1["output_tokens"] == 50
        assert e1["cache_read"] == 200
        assert e1["cache_creation"] == 10
        assert e1["duration_ms"] == 1234
        assert e1["turns"] == 5
        assert "ts" in e1

        e2 = json.loads(lines[1])
        assert e2["agent"] == "evaluator"
        assert e2["phase"] == "evaluator"
        assert e2["cache_read"] == 0
        assert e2["cache_creation"] == 0

    def test_record_phase_defaults_to_empty(self, tmp_path):
        log = tmp_path / "token_log.jsonl"
        tracker = CostTracker(log_path=log)
        tracker.record("planner", 0.30, usage={"input_tokens": 1000, "output_tokens": 200})
        entry = json.loads(log.read_text().strip())
        assert entry["phase"] == ""

    def test_from_dict_requires_log_path_reattach(self, tmp_path):
        log = tmp_path / "token_log.jsonl"
        src = CostTracker(log_path=log)
        src.record("generator", 0.1, usage={"input_tokens": 10, "output_tokens": 5})
        data = src.to_dict()

        # from_dict does NOT know about log_path — caller must re-attach
        restored = CostTracker.from_dict(data)
        assert restored.log_path is None

        # But re-attaching works and continues appending
        restored.log_path = log
        restored.record("evaluator", 0.05, usage={"input_tokens": 8, "output_tokens": 2})
        assert len(log.read_text().strip().split("\n")) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cost_tracker.py::TestCostTrackerLog -v`
Expected: FAIL with `TypeError: unexpected keyword argument 'log_path'` or similar.

- [ ] **Step 3: Add `log_path` field and `_append_log` helper**

Edit `src/core/cost_tracker.py`. At the top of the file replace:

```python
"""
Cost Tracker — Per-Agent Cost Accumulation
============================================

Tracks costs from SDK ResultMessage per agent type (planner, generator, evaluator).
"""

from dataclasses import dataclass, field
from typing import Optional
```

with:

```python
"""
Cost Tracker — Per-Agent Cost Accumulation
============================================

Tracks costs from SDK ResultMessage per agent type (planner, generator, evaluator).
Optionally appends a JSONL log of every record() call for offline token analysis.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
```

Then change the `CostTracker` dataclass definition (current lines 12-27) to add a `log_path` field. Replace:

```python
@dataclass
class CostTracker:
    """Tracks cumulative costs and token usage across all harness agents."""

    planner: float = 0.0
    generator: float = 0.0
    evaluator: float = 0.0
    input_tokens: int = 0          # uncached input only
    output_tokens: int = 0
    cache_read_tokens: int = 0     # input tokens read from cache
    cache_creation_tokens: int = 0  # input tokens written to cache
    total_duration_ms: int = 0
    total_api_ms: int = 0
    total_turns: int = 0
    _session_costs: list = field(default_factory=list)
    _feature_costs: dict = field(default_factory=dict)
```

with:

```python
@dataclass
class CostTracker:
    """Tracks cumulative costs and token usage across all harness agents."""

    planner: float = 0.0
    generator: float = 0.0
    evaluator: float = 0.0
    input_tokens: int = 0          # uncached input only
    output_tokens: int = 0
    cache_read_tokens: int = 0     # input tokens read from cache
    cache_creation_tokens: int = 0  # input tokens written to cache
    total_duration_ms: int = 0
    total_api_ms: int = 0
    total_turns: int = 0
    log_path: Optional[Path] = None  # if set, each record() appends a JSONL line here
    _session_costs: list = field(default_factory=list)
    _feature_costs: dict = field(default_factory=dict)

    def _append_log(
        self,
        agent_type: str,
        phase: str,
        cost_usd: float,
        usage: Optional[dict],
        duration_ms: int,
        num_turns: int,
    ) -> None:
        """Append one JSONL entry to log_path. Silent on I/O errors."""
        if self.log_path is None:
            return
        usage = usage or {}
        entry = {
            "ts": time.time(),
            "agent": agent_type,
            "phase": phase,
            "cost_usd": cost_usd,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read": usage.get("cache_read_input_tokens", 0),
            "cache_creation": usage.get("cache_creation_input_tokens", 0),
            "duration_ms": duration_ms,
            "turns": num_turns,
        }
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass  # telemetry must never break a run
```

- [ ] **Step 4: Extend `record()` to accept `phase` and call `_append_log`**

In the same file, replace the current `record()` method (current lines 38-63):

```python
    def record(self, agent_type: str, cost_usd: float, usage: Optional[dict] = None,
               duration_ms: int = 0, duration_api_ms: int = 0, num_turns: int = 0) -> None:
        """Record cost, tokens, and timing for an agent session."""
        if agent_type == "planner":
            self.planner += cost_usd
        elif agent_type == "generator":
            self.generator += cost_usd
        elif agent_type == "evaluator":
            self.evaluator += cost_usd

        if usage:
            self.input_tokens += usage.get("input_tokens", 0)
            self.output_tokens += usage.get("output_tokens", 0)
            self.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
            self.cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
        self.total_duration_ms += duration_ms
        self.total_api_ms += duration_api_ms
        self.total_turns += num_turns

        self._session_costs.append({
            "agent": agent_type,
            "cost_usd": cost_usd,
            "usage": usage,
            "duration_ms": duration_ms,
            "num_turns": num_turns,
        })
```

with:

```python
    def record(self, agent_type: str, cost_usd: float, usage: Optional[dict] = None,
               duration_ms: int = 0, duration_api_ms: int = 0, num_turns: int = 0,
               phase: str = "") -> None:
        """Record cost, tokens, and timing for an agent session.

        phase: optional sub-category label (e.g. "generator-retry-2", "planner-architect",
               "evaluator-resume"). Written to the JSONL log if log_path is set. Default
               empty keeps existing call sites working untouched.
        """
        if agent_type == "planner":
            self.planner += cost_usd
        elif agent_type == "generator":
            self.generator += cost_usd
        elif agent_type == "evaluator":
            self.evaluator += cost_usd

        if usage:
            self.input_tokens += usage.get("input_tokens", 0)
            self.output_tokens += usage.get("output_tokens", 0)
            self.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
            self.cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
        self.total_duration_ms += duration_ms
        self.total_api_ms += duration_api_ms
        self.total_turns += num_turns

        self._session_costs.append({
            "agent": agent_type,
            "cost_usd": cost_usd,
            "usage": usage,
            "duration_ms": duration_ms,
            "num_turns": num_turns,
            "phase": phase,
        })

        self._append_log(agent_type, phase, cost_usd, usage, duration_ms, num_turns)
```

- [ ] **Step 5: Run unit tests again**

Run: `pytest tests/test_cost_tracker.py::TestCostTrackerLog -v`
Expected: PASS for all four tests.

- [ ] **Step 6: Wire `log_path` at orchestrator construction sites**

Edit `src/core/orchestrator.py`. At the fresh-construction site (current line 1575), replace:

```python
    cb = CircuitBreaker(state_dir, config.get("circuit_breaker", {}))
    cost_tracker = CostTracker()
    prompts_dir = Path(__file__).parent.parent / "prompts"
```

with:

```python
    cb = CircuitBreaker(state_dir, config.get("circuit_breaker", {}))
    cost_tracker = CostTracker(log_path=state_dir / "token_log.jsonl")
    prompts_dir = Path(__file__).parent.parent / "prompts"
```

At the resume-construction site (current line 1620), replace:

```python
        cost_tracker = CostTracker.from_dict(state.get("cost_breakdown", {}))
```

with:

```python
        cost_tracker = CostTracker.from_dict(state.get("cost_breakdown", {}))
        cost_tracker.log_path = state_dir / "token_log.jsonl"
```

- [ ] **Step 7: Add phase labels at the 8 `record()` call sites**

Pass an appropriate `phase` kwarg at each of the existing call sites. Exact line numbers (verified against master):

- `orchestrator.py:994` — parallel pool generator first/retry call. Change:
  ```python
  cost_tracker.record("generator", gen_result.get("cost", 0), **_metrics_from(gen_result))
  ```
  to:
  ```python
  cost_tracker.record("generator", gen_result.get("cost", 0),
                      phase="generator-pool", **_metrics_from(gen_result))
  ```

- `orchestrator.py:1041` — parallel pool evaluator. Change:
  ```python
  cost_tracker.record("evaluator", qa_result.get("cost", 0), **_metrics_from(qa_result))
  ```
  to:
  ```python
  cost_tracker.record("evaluator", qa_result.get("cost", 0),
                      phase="evaluator-pool", **_metrics_from(qa_result))
  ```

- `orchestrator.py:1327` — recovery-eval of failed worktrees. Change:
  ```python
  cost_tracker.record("evaluator", eval_result.get("cost", 0), **_metrics_from(eval_result))
  ```
  to:
  ```python
  cost_tracker.record("evaluator", eval_result.get("cost", 0),
                      phase="evaluator-recovery", **_metrics_from(eval_result))
  ```

- `orchestrator.py:1492` — resumed-agent eval. Change:
  ```python
  cost_tracker.record("evaluator", eval_result.get("cost", 0), **_metrics_from(eval_result))
  ```
  to:
  ```python
  cost_tracker.record("evaluator", eval_result.get("cost", 0),
                      phase="evaluator-resume", **_metrics_from(eval_result))
  ```

- `orchestrator.py:1721` — planner main call. Change:
  ```python
  cost_tracker.record("planner", result["cost"], **_metrics_from(result))
  ```
  to:
  ```python
  cost_tracker.record("planner", result["cost"],
                      phase="planner", **_metrics_from(result))
  ```

- `orchestrator.py:1755` — planner retry/refine call. Change:
  ```python
  cost_tracker.record("planner", result2["cost"], **_metrics_from(result2))
  ```
  to:
  ```python
  cost_tracker.record("planner", result2["cost"],
                      phase="planner-retry", **_metrics_from(result2))
  ```

- `orchestrator.py:2051` — sequential-mode generator. Change:
  ```python
  cost_tracker.record("generator", gen_result["cost"], **_metrics_from(gen_result))
  ```
  to:
  ```python
  cost_tracker.record("generator", gen_result["cost"],
                      phase="generator-seq", **_metrics_from(gen_result))
  ```

- `orchestrator.py:2162` — sequential-mode evaluator. Change:
  ```python
  cost_tracker.record("evaluator", eval_result["cost"], **_metrics_from(eval_result))
  ```
  to:
  ```python
  cost_tracker.record("evaluator", eval_result["cost"],
                      phase="evaluator-seq", **_metrics_from(eval_result))
  ```

NOTE: these line numbers are the CURRENT master. After Step 6 edits land, the line numbers shift by 1. Use your editor's "go to definition" / grep for the exact match rather than trusting post-edit line numbers.

- [ ] **Step 8: Syntax + smoke check**

Run: `python -c "from src.core.cost_tracker import CostTracker; t = CostTracker(); t.record('generator', 0.1, usage={'input_tokens': 1}); print('ok')"`
Expected: prints `ok`.

Run: `python -c "import ast; ast.parse(open('src/core/orchestrator.py').read()); print('ok')"`
Expected: prints `ok`.

- [ ] **Step 9: Commit**

```bash
git add src/core/cost_tracker.py src/core/orchestrator.py tests/test_cost_tracker.py
git commit -m "feat(telemetry): append per-session token log to token_log.jsonl

CostTracker.record() now optionally appends a JSONL entry per call,
capturing cost, input/output/cache tokens, duration, and phase label.
Orchestrator wires log_path at both fresh and resume construction
sites and tags each of the 8 record() calls with a phase label so
the log can be sliced per agent type and sub-phase.

No behavior change. Log is write-only; no reader yet."
```

---

## Task 1: Remove RETRY_COUNT from evaluator system prompt (prompt-cache fix)

**Why:** `src/core/orchestrator.py:109-147` bakes `retry_count` into the evaluator *system* prompt. The comment on line 139 already acknowledges this defeats prompt caching. In sequential mode (line 2153) retry_count rises 1 → 2 → 3 on each retry, so the rendered system prompt changes and every retry pays full uncached input. The parallel pool path (line 688) currently hardcodes "1" so it is cacheable but the on-screen content is a lie — every retry's feedback.md says "attempt 1 of 3". Both sites need the same cleanup.

**Files:**
- Modify: `src/prompts/evaluator-v2.md` (lines 71 and 83)
- Modify: `src/core/orchestrator.py` (`_build_eval_replacements` at 109-147, `_qa_repl` at 688-696, three call sites at 1302, 1477, 2153)
- Create: `tests/test_eval_prompt_cache.py`

### Steps

- [ ] **Step 1: Write the failing test**

Create `tests/test_eval_prompt_cache.py` (new file — separate from `tests/test_orchestrator.py` to keep the prompt-cache concern isolated):

```python
"""Tests that the evaluator system prompt is stable across retries so the
Claude prompt cache hits on retry 2 and 3 within its TTL."""

from pathlib import Path

from src.core.orchestrator import _build_eval_replacements, load_prompt


# tests/ lives at the repo root, so parent.parent == repo root
PROMPTS_DIR = Path(__file__).parent.parent / "src" / "prompts"


class TestEvalPromptCacheStability:
    def test_system_prompt_is_stable_across_retries(self, tmp_path):
        """Rendered evaluator system prompt must be byte-identical for retry 1, 2, 3
        so the Claude prompt cache hits on retries."""
        config = {
            "evaluator": {"browser_verification": "never"},
            "generator": {"max_retries_per_feature": 3},
        }

        repl_1, eval_file, eval_fallback, _ = _build_eval_replacements(
            "feat-1", tmp_path, config, "go test ./...", retry_count=1)
        repl_2, _, _, _ = _build_eval_replacements(
            "feat-1", tmp_path, config, "go test ./...", retry_count=2)
        repl_3, _, _, _ = _build_eval_replacements(
            "feat-1", tmp_path, config, "go test ./...", retry_count=3)

        prompt_path = PROMPTS_DIR / eval_file
        if not prompt_path.exists():
            prompt_path = PROMPTS_DIR / eval_fallback

        rendered_1 = load_prompt(prompt_path, repl_1)
        rendered_2 = load_prompt(prompt_path, repl_2)
        rendered_3 = load_prompt(prompt_path, repl_3)

        assert rendered_1 == rendered_2 == rendered_3, (
            "Evaluator system prompt changed across retries — prompt cache will miss. "
            "RETRY_COUNT / MAX_RETRIES must not appear in system prompt replacements."
        )

    def test_user_message_carries_retry_context_when_retry_gt_1(self, tmp_path):
        """Retry context still needs to reach the evaluator — it just moves from
        system prompt into user message."""
        config = {
            "evaluator": {"browser_verification": "never"},
            "generator": {"max_retries_per_feature": 3},
        }

        _, _, _, msg_first = _build_eval_replacements(
            "feat-1", tmp_path, config, "go test ./...", retry_count=1)
        _, _, _, msg_retry = _build_eval_replacements(
            "feat-1", tmp_path, config, "go test ./...", retry_count=2)

        assert "attempt" not in msg_first.lower()
        assert "attempt 2 of 3" in msg_retry

    def test_template_has_no_retry_vars(self):
        """Evaluator prompt template must not reference {{RETRY_COUNT}} or {{MAX_RETRIES}}."""
        text = (PROMPTS_DIR / "evaluator-v2.md").read_text()
        assert "{{RETRY_COUNT}}" not in text
        assert "{{MAX_RETRIES}}" not in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_eval_prompt_cache.py -v`
Expected: All three tests FAIL — `test_system_prompt_is_stable_across_retries` because the rendered prompt contains the retry number; `test_template_has_no_retry_vars` because the template still contains `{{RETRY_COUNT}}`; `test_user_message_carries_retry_context_when_retry_gt_1` because the current code doesn't put retry context in the user message.

- [ ] **Step 3: Strip retry template vars from evaluator-v2.md**

Edit `src/prompts/evaluator-v2.md`. At line 71, replace:

```
### Verdict: FAIL (attempt {{RETRY_COUNT}} of {{MAX_RETRIES}})
```

with:

```
### Verdict: FAIL
```

At line 83, replace:

```
Feature <feature-id> — {{RETRY_COUNT}}/{{MAX_RETRIES}} attempts used
```

with:

```
Feature <feature-id> — FAIL
```

Rationale: the next generator already knows its own attempt count from the work_plan on disk and from the orchestrator's user message on the retry dispatch. The evaluator does not need to render this number into feedback.md.

- [ ] **Step 4: Remove RETRY_COUNT / MAX_RETRIES from `_build_eval_replacements`**

Edit `src/core/orchestrator.py`. Replace the entire `_build_eval_replacements` function (current lines 109-158):

```python
def _build_eval_replacements(
    feature_id: str,
    state_dir: Path,
    config: dict,
    test_command: str,
    work_plan=None,
    retry_count: int = 1,
) -> tuple[dict, str, str, str]:
    """Build evaluator system prompt replacements and user message.

    Returns (system_replacements, preferred_file, fallback_file, user_message).
    System replacements are wave-constant (cacheable).
    User message contains feature-specific details (dynamic).
    """
    feature_desc = feature_id
    ac_text = ""
    if work_plan:
        for ph in work_plan.data.get("phases", []):
            for ep in ph.get("epics", []):
                for st in ep.get("stories", []):
                    for t in st.get("tasks", []):
                        if t.get("id") == feature_id:
                            feature_desc = t.get("description", feature_id)
                            ac = t.get("acceptance_criteria", t.get("ac", ""))
                            if isinstance(ac, list):
                                ac_text = "\n".join(f"- {a}" for a in ac)
                            elif ac:
                                ac_text = ac

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

    # User message (dynamic, per-feature)
    user_msg_parts = [
        f"Feature ID: {feature_id}",
        f"Description: {feature_desc}",
    ]
    if ac_text:
        user_msg_parts.append(f"\nAcceptance Criteria:\n{ac_text}")
    user_message = "\n".join(user_msg_parts)

    return system_replacements, "evaluator-v2.md", "evaluator.md", user_message
```

with:

```python
def _build_eval_replacements(
    feature_id: str,
    state_dir: Path,
    config: dict,
    test_command: str,
    work_plan=None,
    retry_count: int = 1,
) -> tuple[dict, str, str, str]:
    """Build evaluator system prompt replacements and user message.

    Returns (system_replacements, preferred_file, fallback_file, user_message).
    System replacements are wave-constant and feature-independent so the rendered
    system prompt is byte-stable across features and retries — this lets the
    Claude prompt cache hit on every evaluator call within its TTL.

    retry_count is accepted (for backward compatibility with call sites) but is
    deliberately NOT written into the system prompt. It is appended to the user
    message instead so the evaluator can still reason about retry context
    without busting the cache.
    """
    feature_desc = feature_id
    ac_text = ""
    if work_plan:
        for ph in work_plan.data.get("phases", []):
            for ep in ph.get("epics", []):
                for st in ep.get("stories", []):
                    for t in st.get("tasks", []):
                        if t.get("id") == feature_id:
                            feature_desc = t.get("description", feature_id)
                            ac = t.get("acceptance_criteria", t.get("ac", ""))
                            if isinstance(ac, list):
                                ac_text = "\n".join(f"- {a}" for a in ac)
                            elif ac:
                                ac_text = ac

    # System prompt replacements — feature-independent AND retry-independent.
    # Anything dynamic goes in the user message below so the system prompt caches.
    system_replacements = {
        "STATE_DIR": str(state_dir),
        "TEST_COMMAND": test_command,
        "IF_WEB_PROJECT": config.get("evaluator", {}).get("browser_verification") != "never",
    }

    # User message (dynamic, per-feature, per-retry)
    user_msg_parts = [
        f"Feature ID: {feature_id}",
        f"Description: {feature_desc}",
    ]
    if ac_text:
        user_msg_parts.append(f"\nAcceptance Criteria:\n{ac_text}")
    if retry_count > 1:
        max_retries = config.get("generator", {}).get("max_retries_per_feature", 3)
        user_msg_parts.append(
            f"\nRetry context: this is attempt {retry_count} of {max_retries}."
        )
    user_message = "\n".join(user_msg_parts)

    return system_replacements, "evaluator-v2.md", "evaluator.md", user_message
```

- [ ] **Step 5: Remove RETRY_COUNT / MAX_RETRIES from parallel pool `_qa_repl`**

Still in `src/core/orchestrator.py`, locate `_qa_repl` (current lines 688-696):

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

- [ ] **Step 6: Verify call sites still compile**

The three call sites that pass `retry_count` as a kwarg stay unchanged — the function still accepts the parameter (for the user-message retry-context line), it just no longer places it into the system prompt. Call sites to confirm untouched:
- `orchestrator.py:1302` — no retry_count passed (defaults to 1) ✓
- `orchestrator.py:1477` — no retry_count passed (defaults to 1) ✓
- `orchestrator.py:2153` — passes `retry_count=_retry` ✓

Run: `python -c "import ast; ast.parse(open('src/core/orchestrator.py').read()); print('ok')"`
Expected: prints `ok`.

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_eval_prompt_cache.py -v`
Expected: all three tests PASS.

- [ ] **Step 8: Grep check**

Run: `rg "RETRY_COUNT|MAX_RETRIES" src/`
Expected: no matches in `src/core/` Python code. The only remaining references may be in comments or docs — if any `{{RETRY_COUNT}}` appears in `src/prompts/`, delete or rephrase it.

- [ ] **Step 9: Commit**

```bash
git add src/core/orchestrator.py src/prompts/evaluator-v2.md tests/test_eval_prompt_cache.py
git commit -m "fix(eval): preserve prompt cache on evaluator retries

_build_eval_replacements and _qa_repl no longer inject RETRY_COUNT or
MAX_RETRIES into the system prompt. Retry context (when retry>1) moves
to the user message instead. Evaluator-v2.md template drops the two
template variables from the FAIL verdict block.

Net effect: in sequential mode the evaluator system prompt is now
byte-identical across retries, so the Claude prompt cache hits on
retry 2 and 3 within the cache TTL. Parallel pool path was already
stable (hardcoded \"1\") but the template cleanup removes the
misleading \"attempt 1 of 3\" text on actual retries.

No decision logic change. Safe to resume in-flight runs."
```

---

## Task 2: Cache relay context per wave and dedupe identical briefs

**Why:** `src/core/orchestrator.py:816` calls `get_relay_context(state_dir, wave_num, include_current_wave=True)` inside every worker's startup. With a pool size of 5, that is 5 independent disk scans of the briefs directory and 5 independent concatenations of the same (up to 15KB) string, then 5 separate TASK_BRIEF.md writes embedding that same text. For a 10-feature wave with 5 workers and 2 retries each that is ~20 rebuilds of an effectively identical relay. Additionally, if two workers produce identical briefs (same files, same decisions), both strings appear verbatim in every reader's TASK_BRIEF.md — pure duplicate input tokens to the generator.

**Strategy:** Two independent pieces.
- **Piece A — per-wave cache** in `orchestrator.py`. Store `(briefs-dir-mtime, rendered-text)` keyed by wave number. Rebuild only when a new brief lands. 1 call per wave (amortized) instead of `workers × retries`.
- **Piece B — content-hash dedup** inside `get_relay_context()` in `discovery.py`. If two briefs hash to the same content bytes, concatenate only the first. Cheap, safe, catches worker restarts.

**Files:**
- Modify: `src/core/orchestrator.py` (add `_relay_cache` module state, add `_get_cached_relay` helper, change the call at current line 816)
- Modify: `src/core/discovery.py` (`get_relay_context()` hash-dedup inside the loop at current lines 182-204)
- Modify: `tests/test_discovery.py` (append `TestRelayDedup` class)
- Modify: `tests/test_orchestrator.py` (append `TestRelayCache` class)

### Steps

- [ ] **Step 1: Write the failing test for hash-dedup**

Append to `tests/test_discovery.py` (file already exists with imports for `write_brief` and `get_relay_context`):

```python
class TestRelayDedup:
    def test_identical_briefs_are_deduped(self, tmp_path):
        # write_brief() creates the briefs dir on its own; no manual mkdir needed.
        brief_a = "## Agent: a | Feature: f1\n**Status:** complete\n**Files:** src/foo.go"
        brief_b = brief_a  # byte-identical
        brief_c = "## Agent: c | Feature: f2\n**Status:** complete\n**Files:** src/bar.go"

        write_brief(brief_a, wave=1, agent_id="a", state_dir=tmp_path)
        write_brief(brief_b, wave=1, agent_id="b", state_dir=tmp_path)
        write_brief(brief_c, wave=1, agent_id="c", state_dir=tmp_path)

        relay = get_relay_context(tmp_path, current_wave=1, include_current_wave=True)

        assert relay.count("src/foo.go") == 1, "duplicate brief a/b should appear only once"
        assert relay.count("src/bar.go") == 1
        assert "Agent: a" in relay or "Agent: b" in relay  # at least one copy survives

    def test_distinct_briefs_are_all_included(self, tmp_path):
        write_brief("## Agent: a | Feature: f1\n**Files:** src/foo.go", 1, "a", tmp_path)
        write_brief("## Agent: b | Feature: f2\n**Files:** src/bar.go", 1, "b", tmp_path)

        relay = get_relay_context(tmp_path, current_wave=1, include_current_wave=True)
        assert "src/foo.go" in relay
        assert "src/bar.go" in relay
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_discovery.py::TestRelayDedup -v`
Expected: `test_identical_briefs_are_deduped` FAILS — current code includes both copies, so `count("src/foo.go") == 2`.

- [ ] **Step 3: Add hash-dedup in `get_relay_context()`**

Edit `src/core/discovery.py`. Replace the body of `get_relay_context()` (current lines 139-213) — specifically the inner concatenation loop — with a version that tracks seen content hashes. Replace:

```python
    MAX_CHARS = 15000  # ~3750 tokens — keeps TASK_BRIEF.md manageable
    parts = []
    total_size = 0

    # Same-wave briefs first — most relevant (parallel siblings)
    if same_wave:
        parts.append("### Same-wave discoveries (agents working in parallel with you)")
        for _, p in same_wave:
            try:
                content = p.read_text()
            except OSError:
                continue
            if total_size + len(content) > MAX_CHARS:
                break
            parts.append(content)
            total_size += len(content)

    # Prior-wave briefs — recent ones only
    if prior_wave and total_size < MAX_CHARS:
        parts.append("### Prior-wave discoveries")
        for _, _, p in prior_wave:
            try:
                content = p.read_text()
            except OSError:
                continue
            if total_size + len(content) > MAX_CHARS:
                break
            parts.append(content)
            total_size += len(content)
```

with:

```python
    MAX_CHARS = 15000  # ~3750 tokens — keeps TASK_BRIEF.md manageable
    parts: list[str] = []
    total_size = 0
    seen_hashes: set[str] = set()

    def _try_add(content: str) -> bool:
        """Add content if not a duplicate and within budget. Returns False when the
        budget is exhausted so the caller can break out of its loop."""
        nonlocal total_size
        import hashlib
        h = hashlib.sha1(content.encode("utf-8", errors="replace")).hexdigest()
        if h in seen_hashes:
            return True  # dup — skip but keep going
        if total_size + len(content) > MAX_CHARS:
            return False  # budget blown — caller should stop
        seen_hashes.add(h)
        parts.append(content)
        total_size += len(content)
        return True

    # Same-wave briefs first — most relevant (parallel siblings)
    if same_wave:
        parts.append("### Same-wave discoveries (agents working in parallel with you)")
        for _, p in same_wave:
            try:
                content = p.read_text()
            except OSError:
                continue
            if not _try_add(content):
                break

    # Prior-wave briefs — recent ones only
    if prior_wave and total_size < MAX_CHARS:
        parts.append("### Prior-wave discoveries")
        for _, _, p in prior_wave:
            try:
                content = p.read_text()
            except OSError:
                continue
            if not _try_add(content):
                break
```

- [ ] **Step 4: Run dedup tests to verify they pass**

Run: `pytest tests/test_discovery.py::TestRelayDedup -v`
Expected: both tests PASS.

- [ ] **Step 5: Write the failing test for per-wave cache**

Append to `tests/test_orchestrator.py`:

```python
import os

from src.core.discovery import write_brief
from src.core.fleet_session import fleet_dir
from src.core.orchestrator import _get_cached_relay, _relay_cache


class TestRelayCache:
    def test_hits_when_briefs_unchanged(self, tmp_path):
        _relay_cache.clear()
        write_brief("## Agent: a | Feature: f1\n**Files:** src/foo.go", 1, "a", tmp_path)

        r1 = _get_cached_relay(tmp_path, wave_num=1)
        r2 = _get_cached_relay(tmp_path, wave_num=1)
        assert r1 == r2
        assert r1 != ""
        assert 1 in _relay_cache  # wave cached

    def test_invalidates_when_new_brief_lands(self, tmp_path):
        _relay_cache.clear()
        write_brief("## Agent: a | Feature: f1\n**Files:** src/foo.go", 1, "a", tmp_path)

        r1 = _get_cached_relay(tmp_path, wave_num=1)
        assert "src/foo.go" in r1
        assert "src/bar.go" not in r1

        # New brief lands. To survive 1-second mtime resolution on some filesystems,
        # explicitly bump the new brief's mtime forward instead of sleeping.
        new_brief = write_brief(
            "## Agent: b | Feature: f2\n**Files:** src/bar.go", 1, "b", tmp_path)
        future = new_brief.stat().st_mtime + 5
        os.utime(new_brief, (future, future))

        r2 = _get_cached_relay(tmp_path, wave_num=1)
        assert "src/foo.go" in r2
        assert "src/bar.go" in r2

    def test_returns_empty_when_no_briefs(self, tmp_path):
        _relay_cache.clear()
        assert _get_cached_relay(tmp_path, wave_num=1) == ""
```

- [ ] **Step 6: Run test to verify it fails**

Run: `pytest tests/test_orchestrator.py::TestRelayCache -v`
Expected: collection FAILS at import time — `_get_cached_relay` and `_relay_cache` do not exist yet.

- [ ] **Step 7: Add `_relay_cache` and `_get_cached_relay` to orchestrator**

Edit `src/core/orchestrator.py`. Locate the existing `from . import fleet_session` import (currently at line 36) — no import change needed; we use `fleet_session.fleet_dir(...)` to match the rest of the file's calling convention.

Locate the `get_relay_context` import (it should already be imported via `from .discovery import ...`). If not present, add:

```python
from .discovery import get_relay_context
```

(verify with `rg "get_relay_context" src/core/orchestrator.py` first — if line 816 already calls it, the import already exists.)

Then add the cache state and helper near the top of the file, just after the existing module-level globals (a good home is right above `_build_eval_replacements` at current line 109, since both are orchestrator-internal helpers):

```python
# Per-wave relay cache — avoids re-reading + re-concatenating briefs for every
# parallel worker in the same wave. Keyed by wave number. Value is
# (newest_brief_mtime_seen_at_cache_time, rendered_text).
_relay_cache: dict[int, tuple[float, str]] = {}


def _get_cached_relay(state_dir: Path, wave_num: int) -> str:
    """Return the relay context for wave_num, building it once per wave and
    rebuilding only when a newer brief has landed. Safe to call from many
    concurrent workers — races only cause extra rebuilds, never wrong output.

    NOTE: state must be invalidated between runs (different state_dir) — the
    cache is keyed only by wave_num, so a new run with overlapping wave numbers
    must start from a fresh process. Harness already does this: each run is a
    fresh `python3 run.py` invocation.
    """
    briefs_dir = fleet_session.fleet_dir(state_dir) / "briefs"
    if not briefs_dir.exists():
        return ""
    try:
        mtimes = [p.stat().st_mtime for p in briefs_dir.glob("w*-*.md")]
    except OSError:
        mtimes = []
    newest = max(mtimes, default=0.0)

    cached = _relay_cache.get(wave_num)
    if cached is not None and cached[0] >= newest and newest > 0.0:
        return cached[1]

    text = get_relay_context(state_dir, wave_num, include_current_wave=True)
    _relay_cache[wave_num] = (newest, text)
    return text
```

- [ ] **Step 8: Swap in `_get_cached_relay` at the worker call site**

Still in `src/core/orchestrator.py`, locate the current line 816 inside `_pool_worker`:

```python
            _relay = get_relay_context(state_dir, wave_num, include_current_wave=True) if relay_enabled else ""
```

Replace with:

```python
            _relay = _get_cached_relay(state_dir, wave_num) if relay_enabled else ""
```

- [ ] **Step 9: Run cache tests to verify they pass**

Run: `pytest tests/test_orchestrator.py::TestRelayCache -v`
Expected: all three tests PASS.

- [ ] **Step 10: Full test-suite sanity**

Run: `pytest tests/ -q`
Expected: all pre-existing tests still green; new tests pass.

- [ ] **Step 11: Syntax check**

Run: `python -c "import ast; ast.parse(open('src/core/orchestrator.py').read()); ast.parse(open('src/core/discovery.py').read()); print('ok')"`
Expected: prints `ok`.

- [ ] **Step 12: Commit**

```bash
git add src/core/orchestrator.py src/core/discovery.py tests/test_discovery.py tests/test_orchestrator.py
git commit -m "perf(relay): cache relay context per wave and dedup identical briefs

- orchestrator._get_cached_relay builds the discovery relay once per
  wave and reuses it for every worker in that wave, invalidating only
  when a newer brief lands (detected via mtime).
- discovery.get_relay_context skips briefs whose SHA1 matches an
  already-included brief, so identical parallel outputs are not
  concatenated twice into every reader's TASK_BRIEF.md.

Net effect: fewer disk scans per wave, smaller TASK_BRIEF.md, and no
duplicate input tokens when workers produce identical summaries. No
decision logic change."
```

---

## Final verification

- [ ] **Step 1: Full test run**

Run: `pytest tests/ -q`
Expected: all green, including the three new files.

- [ ] **Step 2: Lint / syntax**

Run: `python -m py_compile src/core/cost_tracker.py src/core/orchestrator.py src/core/discovery.py`
Expected: no output (success).

- [ ] **Step 3: Manual smoke run (optional — cost ~$0.20)**

On a throwaway branch of a test project:

```bash
python3 -m harness run --iterations 1
```

After the run:
- `cat .harness/runs/<run-id>/token_log.jsonl | head -5` — lines exist, valid JSON, phase labels look right
- `jq -s 'group_by(.phase) | map({phase: .[0].phase, sessions: length, cache_hit_rate: ((map(.cache_read) | add) / ((map(.cache_read) | add) + (map(.cache_creation) | add) + 1))})' .harness/runs/<run-id>/token_log.jsonl` — shows per-phase stats

- [ ] **Step 4: Push branch**

```bash
git push -u origin feature/token-efficiency-t0-t2
```

No PR created automatically — open one manually when ready.

---

## Risk summary

| Task | Touches decision logic? | In-flight safe? | Rollback |
|------|------------------------|-----------------|----------|
| T0 | No | Yes | Revert commit; log file is orphan data |
| T1 | No (system prompt content only; user message preserves retry context) | Yes | Revert commit; prompt template restored |
| T2 | No (relay text only; decisions read briefs same way) | Yes | Revert commit; per-worker rebuild restored |

All three changes are pure refactors from the orchestrator's point of view. The in-flight `run-legacy-migrated` can be resumed at any point during or after this plan lands — the only user-visible difference will be (a) a new `token_log.jsonl` file in the run directory and (b) better cache hit rates from the next evaluator call onward.

## Open questions

Resolved by user before implementation:
1. **T1 retry info:** kept in the user message when `retry_count > 1`, not dropped entirely. Preserves evaluator reasoning without busting cache.
2. **Execution order:** T0 → T1 → T2 as numbered.
3. **Execution mode:** inline, not subagent-driven (three small commits, pure refactor).
