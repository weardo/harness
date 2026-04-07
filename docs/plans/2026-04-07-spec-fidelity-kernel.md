# Harness Spec Fidelity Kernel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Close the execution-phase enforcement gaps that let agents ship 10%-of-spec implementations. Make simplification drift programmatically rejectable by the orchestrator itself, not just prompt-discouraged by the LLM agents.

**Context:** An audit on 2026-04-07 found that the harness planning layer (architect → adversary → refiner → validator) is already strong — it produces rigorous work plans with behavioral AC, spec gap analysis, and structured sign-off. The weakness is the **execution seam**: once a task reaches the generator, the only gates between the agent and a 10%-of-spec implementation are (a) text prompts telling it to be thorough, (b) a single-LLM reviewer that's just as susceptible to simplification bias as the generator, and (c) the orchestrator trusting a text match of `VERDICT: PASS`. This plan adds mechanical gates that don't depend on LLM judgment.

**Why this matters economically:** The research (Addy Osmani's "80% problem", Metaswarm's enforcement layers, Traycer's drift analysis, ImpossibleBench on reward hacking) converges on one finding: **fix-up costs dominate generation costs by 10-50x**. An agent that ships a simplified feature burns tokens on detection, re-spawning generators for each gap, and cascading re-fixes. Prevention is 1x, detection is 3-5x, fix-up is 10-50x. This plan targets prevention and early detection.

**Architecture:** Seven phases, each shippable independently. Phases 1 and 2 are highest leverage and lowest risk — they close the most common drift holes with mechanical checks that cost zero LLM tokens. Phase 3 onward add role rotation, per-feature adversary re-runs, and structured handoffs. Phase 6 (immutable test directory with tester agent) is a bigger architectural change that should only be pursued if earlier phases prove insufficient.

**Tech stack:** Python 3.11+, pytest, Claude Agent SDK, Claude CLI. No new external dependencies in Phases 1–5.

**Machine constraints:** Mac mini M4, 16 GB unified memory. All phases of this plan are lightweight — no local LLM, no significant disk impact.

**Relationship to the token-efficiency plan:** This plan MUST ship before the token-efficiency plan (`docs/plans/2026-04-07-token-efficiency.md`). Making a drift-prone loop cheaper per iteration is optimizing the wrong variable — it just produces cheaper broken runs. Build trust in the loop first, then optimize it.

---

## What the harness already has (do NOT rebuild)

This plan assumes and builds on these existing features. Do NOT reimplement them.

| Feature | Where | What it does |
|---|---|---|
| Adversarial spec QA | `src/prompts/adversary.md` | Produces `spec_gaps.json` with min 9 gaps. Explicitly targets "AC that an LLM will satisfy trivially". Runs at **planning time**. |
| Gap-addressing refinement | `src/prompts/refiner.md` | "You must address every gap — do not drop gaps silently." |
| Release gate validator | `src/prompts/validator.md` | Produces `validation.json` with `sign_off`, per-requirement coverage, gap coverage, DAG validation, phase ordering. |
| Behavioral AC enforcement | `src/prompts/architect.md` + `refiner.md` | "At least 3 behavioral acceptance_criteria (not 'file exists' — 'user can do X')." |
| Hard test gate | `src/prompts/evaluator-v2.md` | "STEP 1: RUN TEST SUITE (HARD GATE)." |
| Strict evaluator framing | `src/prompts/evaluator-v2.md` | "Do NOT approve work that fails any acceptance criterion. Do NOT trust the builder's self-evaluation — verify independently." |
| Known failure modes | `src/prompts/generator-v2.md` | FM-01 through FM-10, including FM-10 (Test-Implementation Circularity — read AC before writing tests). |
| Per-feature retries | `orchestrator.py:726` | `max_retries_per_feature: 3` with worktree preservation. |
| Session resume for retries | `orchestrator.py:832` | Resume same Claude CLI session on retry, pass `feedback.md`. |
| Worktree isolation | `parallel.py` | Each task runs in its own git worktree. |
| Scope claiming | `coordination.py` | Prevents parallel tasks from overwriting shared files. |
| Circuit breaker (run-level) | `circuit_breaker.py` | Nygard pattern with stagnation detection. |
| Suspicion detection | `config.yaml` | `min_retry_rate: 0.3`, `min_avg_feature_seconds: 30`. |

**The gap:** Every item above is either (a) at planning time only, or (b) a text-prompt instruction to an LLM, or (c) a text-match check in orchestrator code. None of them programmatically catch "agent shipped code containing TODO markers for half the feature." That's what this plan adds.

---

## File Structure

**New files:**

| File | Purpose |
|------|---------|
| `src/core/drift_scanner.py` | Pure-function grep-based detection of simplification markers (TODO, FIXME, not-implemented, stubs) in task scope. No LLM, no orchestrator deps. |
| `src/core/independent_verifier.py` | Orchestrator-owned test runner. Runs `test_command` directly via subprocess after evaluator says PASS. Catches reward hacking and evaluator leniency. |
| `src/core/ac_coverage.py` | AC-ID generation from work plan, coverage map parsing from `HARNESS_VERDICT.json`, rejection logic for incomplete maps. |
| `src/core/drift_counter.py` | Instrumentation — counts drift incidents per run for A/B measurement. |
| `src/prompts/reviewer-fresh.md` | Alternate reviewer prompt used on eval-failure retries. Explicitly tells the reviewer "a previous reviewer said this passed, find what they missed." |
| `src/prompts/adversary-diff.md` | Per-feature adversary. Reviews generator's diff before the full evaluator runs, looking for simplification patterns specific to this task's AC. |
| `tests/test_drift_scanner.py` | Unit tests for each detection pattern. |
| `tests/test_independent_verifier.py` | Unit tests with mocked subprocess. |
| `tests/test_ac_coverage.py` | Unit tests for AC-ID generation + coverage map validation. |
| `tests/test_drift_counter.py` | Unit tests for counter accumulation + summary. |

**Modified files:**

| File | What changes |
|------|-------------|
| `src/core/work_plan.py` | Add `assign_ac_ids()` to generate stable AC-001, AC-002, ... IDs for each task. Preserve across refinement loops. |
| `src/core/orchestrator.py` | Wire all mechanical gates into the wave result loop and the 3 other VERDICT: FAIL sites. |
| `src/core/parallel.py` | `write_task_brief()` includes AC-IDs explicitly alongside the AC text. |
| `src/prompts/architect.md` | Emit AC as list of objects with `id` field, not just strings. |
| `src/prompts/refiner.md` | Preserve AC-IDs when refining; allocate new IDs for added criteria. |
| `src/prompts/generator-v2.md` | Require `HARNESS_VERDICT.json` with `criterion_coverage` mapping every AC-ID to an implementation artifact. |
| `src/prompts/evaluator-v2.md` | Add Step 4: "Verify `HARNESS_VERDICT.json` exists and every AC-ID is mapped to a concrete artifact." |
| `src/config.yaml` | New `spec_fidelity` config block with per-feature knobs. |

---

## Phase ordering

| Phase | Tasks | Risk | LLM cost | Leverage | Ship order |
|---|---|---|---|---|---|
| 0 | Task 0: Drift instrumentation | very low | zero | measure-only | 1st (prerequisite) |
| 1 | Tasks 1–2: Drift scanner + independent verifier | low | zero | ★★★★★ | 2nd |
| 2 | Tasks 3–5: AC-ID schema + coverage map + rejection | medium | zero at runtime | ★★★★★ | 3rd |
| 3 | Task 6: Fresh reviewer on failure | low | +1 LLM call per retry | ★★★ | 4th |
| 4 | Task 7: Per-feature adversary on diff | medium | +1 LLM call per feature | ★★★★ | 5th |
| 5 | Task 8: Structured JSON handoff | medium | zero | ★★★ | 6th |
| 6 | Tasks 9–10: Immutable test directory + tester agent | high | +1 agent role | ★★★★ | 7th (only if needed) |
| 7 | Tasks 11–12: Multi-model + living spec | high | +N× per feature | ★★ | optional |

**Recommended first shipment:** Phase 0 + Phase 1 + Phase 2. That's Tasks 0 through 5 — covers the biggest holes with mechanical gates, no new LLM calls at runtime, and produces measurable drift counts.

---

## Task 0: Drift instrumentation (Phase 0)

**Goal:** Before adding gates, add counters that measure how often drift happens. Without this we can't prove the gates are working.

**Files:**
- Create: `src/core/drift_counter.py`
- Modify: `src/core/orchestrator.py` (wire counter into wave loop)
- Test: `tests/test_drift_counter.py` (NEW)

- [ ] **Step 1: Write the failing test**

Create `tests/test_drift_counter.py`:

```python
"""Tests for drift_counter — accumulates drift incidents per run."""


def test_counter_starts_empty():
    from src.core.drift_counter import DriftCounter
    c = DriftCounter()
    summary = c.summary()
    assert summary["total"] == 0
    assert summary["by_kind"] == {}


def test_counter_records_drift_with_kind():
    from src.core.drift_counter import DriftCounter
    c = DriftCounter()
    c.record("todo_marker", feature_id="feat-1", details={"file": "a.go", "line": 42})
    c.record("todo_marker", feature_id="feat-2", details={"file": "b.go", "line": 7})
    c.record("independent_test_failure", feature_id="feat-1", details={})
    summary = c.summary()
    assert summary["total"] == 3
    assert summary["by_kind"]["todo_marker"] == 2
    assert summary["by_kind"]["independent_test_failure"] == 1


def test_counter_tracks_per_feature():
    from src.core.drift_counter import DriftCounter
    c = DriftCounter()
    c.record("todo_marker", feature_id="feat-1", details={})
    c.record("ac_uncovered", feature_id="feat-1", details={"ac_id": "AC-003"})
    per_feat = c.per_feature("feat-1")
    assert len(per_feat) == 2
    assert {e["kind"] for e in per_feat} == {"todo_marker", "ac_uncovered"}


def test_counter_full_log_preserves_order():
    from src.core.drift_counter import DriftCounter
    c = DriftCounter()
    c.record("a", feature_id="feat-1", details={})
    c.record("b", feature_id="feat-2", details={})
    c.record("c", feature_id="feat-3", details={})
    log = c.full_log()
    assert [e["kind"] for e in log] == ["a", "b", "c"]
```

- [ ] **Step 2: Run the test and confirm it fails**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_drift_counter.py -v
```

Expected: ImportError — module doesn't exist.

- [ ] **Step 3: Implement `drift_counter.py`**

Create `src/core/drift_counter.py`:

```python
"""Drift counter — per-run instrumentation for spec fidelity measurement.

Tracks every drift incident the mechanical gates catch. Used to:
  (a) prove the gates are working — counts should drop after fixes ship
  (b) identify which drift kinds dominate so we know where to invest
  (c) provide per-feature attribution so we can spot pathological tasks

Drift kinds (append to this list as gates grow):
  - 'todo_marker'              — drift_scanner found a TODO/FIXME/unimplemented
  - 'independent_test_failure' — evaluator said PASS but orchestrator's test run failed
  - 'ac_uncovered'             — HARNESS_VERDICT.json missing AC-ID mapping
  - 'ac_mapped_but_missing'    — coverage map claims artifact but file doesn't exist
  - 'reviewer_overturned'      — fresh reviewer on retry found gap the first missed
  - 'adversary_diff_flagged'   — per-feature adversary found simplification in diff
"""

from datetime import datetime, timezone
from threading import Lock


class DriftCounter:
    """Thread-safe counter for drift incidents across a run."""

    def __init__(self):
        self._lock = Lock()
        self._log: list[dict] = []

    def record(self, kind: str, feature_id: str, details: dict) -> None:
        """Record a single drift incident.

        Args:
            kind: Drift category (see module docstring).
            feature_id: Which feature exhibited the drift.
            details: Kind-specific context (file, line, ac_id, etc.).
        """
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "feature_id": feature_id,
            "details": details,
        }
        with self._lock:
            self._log.append(entry)

    def summary(self) -> dict:
        """Return aggregate counts for end-of-run reporting."""
        with self._lock:
            by_kind: dict[str, int] = {}
            by_feature: dict[str, int] = {}
            for entry in self._log:
                by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + 1
                by_feature[entry["feature_id"]] = by_feature.get(entry["feature_id"], 0) + 1
            return {
                "total": len(self._log),
                "by_kind": by_kind,
                "by_feature": by_feature,
            }

    def per_feature(self, feature_id: str) -> list[dict]:
        """Return all drift entries for a single feature."""
        with self._lock:
            return [e for e in self._log if e["feature_id"] == feature_id]

    def full_log(self) -> list[dict]:
        """Return a shallow copy of the full log in insertion order."""
        with self._lock:
            return list(self._log)

    def clear(self) -> None:
        """Drop all recorded entries (e.g., on run restart)."""
        with self._lock:
            self._log.clear()
```

- [ ] **Step 4: Run the test and confirm it passes**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_drift_counter.py -v
```

Expected: all 4 PASS.

- [ ] **Step 5: Wire counter into orchestrator**

In `src/core/orchestrator.py`, near the top with the other imports:

```python
from .drift_counter import DriftCounter
```

Inside `run_parallel_wave()` where `cost_tracker` is initialized (search for `cost_tracker = `), add immediately after:

```python
drift_counter = DriftCounter()
```

Pass it to `_pool_worker` via the closure. The counter itself is thread-safe so no extra locking needed.

At end-of-run, add:

```python
_drift_summary = drift_counter.summary()
if _drift_summary["total"] > 0:
    _write_run_log(state_dir, "drift_summary",
                   total=_drift_summary["total"],
                   by_kind=_drift_summary["by_kind"],
                   by_feature=_drift_summary["by_feature"])
    print(f"  Drift summary: {_drift_summary['total']} incidents")
    for _kind, _count in sorted(_drift_summary["by_kind"].items()):
        print(f"    {_kind}: {_count}")
```

Also write the full log to `state_dir / "logs" / "drift.jsonl"` so post-run analysis is possible:

```python
_drift_log_path = state_dir / "logs" / "drift.jsonl"
_drift_log_path.parent.mkdir(parents=True, exist_ok=True)
with open(_drift_log_path, "w") as _f:
    for _entry in drift_counter.full_log():
        import json as _json
        _f.write(_json.dumps(_entry) + "\n")
```

- [ ] **Step 6: Run the full suite**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/ -q
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/core/drift_counter.py src/core/orchestrator.py tests/test_drift_counter.py
git commit -m "feat(fidelity): add drift_counter for per-run spec drift instrumentation

Records every drift incident mechanical gates catch. Produces per-run
summary (total + by_kind + by_feature) plus a full jsonl log at
.harness/runs/<id>/logs/drift.jsonl for post-run analysis."
```

---

## Task 1: Programmatic drift scanner (Phase 1)

**Goal:** Detect simplification markers in the generator's output with a pure grep. This catches the most common drift signature: "agent shipped code with TODO markers where real implementation should be." Zero LLM cost, zero orchestrator dependencies, millisecond execution.

**Files:**
- Create: `src/core/drift_scanner.py`
- Test: `tests/test_drift_scanner.py` (NEW)

- [ ] **Step 1: Write the failing test**

Create `tests/test_drift_scanner.py`:

```python
"""Tests for drift_scanner — grep-based simplification marker detection."""

from pathlib import Path


def test_scanner_detects_plain_todo(tmp_path):
    from src.core.drift_scanner import scan_for_drift
    (tmp_path / "a.go").write_text(
        "package main\n\n"
        "func Connect() error {\n"
        "    // TODO: implement connection retry logic\n"
        "    return nil\n"
        "}\n"
    )
    findings = scan_for_drift(tmp_path, scope=["a.go"])
    assert len(findings) == 1
    assert findings[0]["file"] == "a.go"
    assert findings[0]["line"] == 4
    assert findings[0]["marker"] == "TODO"


def test_scanner_detects_multiple_markers(tmp_path):
    from src.core.drift_scanner import scan_for_drift
    (tmp_path / "a.py").write_text(
        "def handler():\n"
        "    # FIXME: edge case not handled\n"
        "    pass\n"
        "\n"
        "def retry():\n"
        "    raise NotImplementedError\n"
    )
    findings = scan_for_drift(tmp_path, scope=["a.py"])
    markers = {f["marker"] for f in findings}
    assert "FIXME" in markers
    assert "NotImplementedError" in markers
    assert len(findings) == 2


def test_scanner_detects_unimplemented_panic(tmp_path):
    from src.core.drift_scanner import scan_for_drift
    (tmp_path / "a.go").write_text(
        'package main\n'
        'func doWork() { panic("unimplemented") }\n'
    )
    findings = scan_for_drift(tmp_path, scope=["a.go"])
    assert len(findings) == 1
    assert findings[0]["marker"] == "panic_unimplemented"


def test_scanner_ignores_files_outside_scope(tmp_path):
    from src.core.drift_scanner import scan_for_drift
    (tmp_path / "in_scope.go").write_text("// TODO: fix\n")
    (tmp_path / "out_of_scope.go").write_text("// TODO: fix\n")
    findings = scan_for_drift(tmp_path, scope=["in_scope.go"])
    assert len(findings) == 1
    assert findings[0]["file"] == "in_scope.go"


def test_scanner_handles_directory_scope(tmp_path):
    from src.core.drift_scanner import scan_for_drift
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.go").write_text("// TODO: a\n")
    (tmp_path / "pkg" / "b.go").write_text("// TODO: b\n")
    (tmp_path / "other.go").write_text("// TODO: out of scope\n")
    findings = scan_for_drift(tmp_path, scope=["pkg/"])
    files = {f["file"] for f in findings}
    assert files == {"pkg/a.go", "pkg/b.go"}


def test_scanner_ignores_generated_code(tmp_path):
    """Files with 'DO NOT EDIT' or 'Code generated' headers are skipped."""
    from src.core.drift_scanner import scan_for_drift
    (tmp_path / "gen.go").write_text(
        "// Code generated by protoc-gen-go. DO NOT EDIT.\n"
        "// TODO: this is in generated code, ignore\n"
    )
    findings = scan_for_drift(tmp_path, scope=["gen.go"])
    assert findings == []


def test_scanner_skips_test_files_by_default(tmp_path):
    """TODO in test files is not a simplification drift signal."""
    from src.core.drift_scanner import scan_for_drift
    (tmp_path / "foo_test.go").write_text("// TODO: add edge case test\n")
    (tmp_path / "foo.go").write_text("// TODO: bug here\n")
    findings = scan_for_drift(tmp_path, scope=["foo.go", "foo_test.go"])
    # Only the non-test file is flagged
    assert len(findings) == 1
    assert findings[0]["file"] == "foo.go"


def test_scanner_returns_empty_on_clean_code(tmp_path):
    from src.core.drift_scanner import scan_for_drift
    (tmp_path / "a.go").write_text("package main\nfunc main() {}\n")
    findings = scan_for_drift(tmp_path, scope=["a.go"])
    assert findings == []


def test_scanner_detects_rest_of_code_marker(tmp_path):
    """Sweep's infamous '// rest of code' TODO pattern."""
    from src.core.drift_scanner import scan_for_drift
    (tmp_path / "a.ts").write_text(
        "function process(items) {\n"
        "    // ... rest of code\n"
        "}\n"
    )
    findings = scan_for_drift(tmp_path, scope=["a.ts"])
    assert len(findings) == 1
    assert findings[0]["marker"] == "rest_of_code"


def test_scanner_deferred_marker(tmp_path):
    from src.core.drift_scanner import scan_for_drift
    (tmp_path / "a.py").write_text(
        "def handler():\n"
        "    return None  # deferred: will implement in next iteration\n"
    )
    findings = scan_for_drift(tmp_path, scope=["a.py"])
    assert len(findings) == 1
    assert findings[0]["marker"] == "deferred"
```

- [ ] **Step 2: Run the test and confirm it fails**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_drift_scanner.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `drift_scanner.py`**

Create `src/core/drift_scanner.py`:

```python
"""Drift scanner — grep-based detection of simplification markers.

Runs after the generator commits but before the orchestrator trusts
VERDICT: PASS. Catches the most common drift signature: code containing
markers that indicate deferred or missing implementation. Pure functions,
no LLM, no state.

Detection is conservative — false positives are OK (force a retry) but
false negatives let drift through. The patterns below were chosen because
they are strong signals in production code:

  TODO                      — universal deferred-work marker
  FIXME                     — acknowledged bug left in place
  XXX                       — code smell marker
  HACK                      — explicit short-cut acknowledgment
  NotImplementedError       — Python raise
  panic("unimplemented")    — Go idiomatic not-yet-done
  not implemented           — English marker in any comment
  rest of code              — Sweep's famous failure mode
  deferred                  — our own "I'm punting this" marker

Files that are demonstrably generated (headers like "Code generated by X.
DO NOT EDIT.") are skipped. Test files (*_test.go, test_*.py, *.test.ts)
are skipped by default since TODO in tests is often legitimate.
"""

import re
from pathlib import Path
from typing import Iterable


# (label, compiled pattern) pairs. Order matters — first match wins.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("panic_unimplemented", re.compile(r'panic\s*\(\s*["\']unimplemented["\']')),
    ("NotImplementedError", re.compile(r"\bNotImplementedError\b")),
    ("rest_of_code",       re.compile(r"\.{3,}\s*rest of (the )?code", re.IGNORECASE)),
    ("deferred",           re.compile(r"\b(deferred|will implement later|implement later)\b", re.IGNORECASE)),
    ("not_implemented",    re.compile(r"\bnot[_\s-]?implemented\b", re.IGNORECASE)),
    ("TODO",               re.compile(r"\bTODO\b")),
    ("FIXME",              re.compile(r"\bFIXME\b")),
    ("XXX",                re.compile(r"\bXXX\b")),
    ("HACK",               re.compile(r"\bHACK\b")),
]

_GENERATED_MARKERS = (
    "DO NOT EDIT",
    "Code generated by",
    "Automatically generated",
    "@generated",
    "autogenerated",
)

_TEST_FILE_PATTERNS = (
    re.compile(r".*_test\.go$"),
    re.compile(r"^test_.*\.py$"),
    re.compile(r".*\.test\.[jt]sx?$"),
    re.compile(r".*\.spec\.[jt]sx?$"),
    re.compile(r"/tests?/"),
)


def _is_test_file(rel_path: str) -> bool:
    return any(p.search(rel_path) for p in _TEST_FILE_PATTERNS)


def _is_generated(content: str) -> bool:
    head = content[:500]
    return any(marker in head for marker in _GENERATED_MARKERS)


def _iter_scope_files(worktree: Path, scope: Iterable[str]) -> Iterable[tuple[Path, str]]:
    """Yield (absolute_path, relative_path) for every file covered by scope.

    scope entries ending with '/' are treated as directories (recursive walk).
    Other entries are treated as direct file references.
    """
    for entry in scope:
        rel = entry.rstrip("/")
        p = worktree / rel
        if not p.exists():
            continue
        if p.is_file():
            yield p, rel
        elif p.is_dir():
            for sub in p.rglob("*"):
                if sub.is_file() and not _is_binary_like(sub):
                    yield sub, str(sub.relative_to(worktree))


def _is_binary_like(p: Path) -> bool:
    binary_exts = {
        ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".tar",
        ".gz", ".bz2", ".7z", ".mp3", ".mp4", ".mov", ".exe", ".dll",
        ".so", ".dylib", ".wasm",
    }
    return p.suffix.lower() in binary_exts


def scan_for_drift(
    worktree: Path,
    scope: list[str],
    include_tests: bool = False,
) -> list[dict]:
    """Scan scope files for simplification markers.

    Args:
        worktree: Root of the worktree being checked.
        scope: List of files/directories (relative to worktree).
        include_tests: If False (default), skip files matching test patterns.

    Returns a list of findings, each a dict:
        {
            "file": relative path,
            "line": 1-indexed line number,
            "marker": label from _PATTERNS,
            "snippet": up to 120 chars of the matching line
        }
    """
    findings: list[dict] = []
    for abs_path, rel_path in _iter_scope_files(worktree, scope):
        if not include_tests and _is_test_file(rel_path):
            continue
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError):
            continue
        if _is_generated(content):
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            for label, pattern in _PATTERNS:
                if pattern.search(line):
                    findings.append({
                        "file": rel_path,
                        "line": lineno,
                        "marker": label,
                        "snippet": line.strip()[:120],
                    })
                    break  # one finding per line, first match wins
    return findings
```

- [ ] **Step 4: Run the tests and confirm they pass**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_drift_scanner.py -v
```

Expected: all 10 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/core/drift_scanner.py tests/test_drift_scanner.py
git commit -m "feat(fidelity): add drift_scanner for simplification marker detection

Pure grep-based scanner that detects TODO, FIXME, NotImplementedError,
panic('unimplemented'), '... rest of code', deferred markers, etc. in
task scope files. Skips generated code and test files by default. Zero
LLM cost, millisecond execution."
```

---

## Task 2: Independent verifier — orchestrator runs tests itself (Phase 1)

**Goal:** After the reviewer says `VERDICT: PASS`, the orchestrator independently runs the configured test command via subprocess. If the test fails, the pass is overturned. This catches two failure modes:
1. Reviewer missed a real failure (leniency)
2. Agent rewrote the test to make it pass (reward hacking — ImpossibleBench documented this in Claude 3.7 Sonnet)

**Files:**
- Create: `src/core/independent_verifier.py`
- Test: `tests/test_independent_verifier.py` (NEW)

- [ ] **Step 1: Write the failing test**

Create `tests/test_independent_verifier.py`:

```python
"""Tests for independent_verifier — orchestrator-owned test execution."""

from pathlib import Path


def test_verifier_passes_when_command_exits_zero(tmp_path):
    from src.core.independent_verifier import run_independent_verification
    result = run_independent_verification(
        worktree=tmp_path,
        test_command="echo ok && exit 0",
        timeout_seconds=5,
    )
    assert result["passed"] is True
    assert result["exit_code"] == 0
    assert "ok" in result["stdout"]


def test_verifier_fails_when_command_exits_nonzero(tmp_path):
    from src.core.independent_verifier import run_independent_verification
    result = run_independent_verification(
        worktree=tmp_path,
        test_command="echo FAILURE; exit 1",
        timeout_seconds=5,
    )
    assert result["passed"] is False
    assert result["exit_code"] == 1
    assert "FAILURE" in result["stdout"]


def test_verifier_times_out_on_hang(tmp_path):
    from src.core.independent_verifier import run_independent_verification
    result = run_independent_verification(
        worktree=tmp_path,
        test_command="sleep 10",
        timeout_seconds=1,
    )
    assert result["passed"] is False
    assert result["timed_out"] is True


def test_verifier_skipped_when_command_empty(tmp_path):
    from src.core.independent_verifier import run_independent_verification
    result = run_independent_verification(
        worktree=tmp_path,
        test_command="",
        timeout_seconds=5,
    )
    assert result["passed"] is True
    assert result["skipped"] is True


def test_verifier_never_raises_on_bad_command(tmp_path):
    """Even when the shell can't launch the command, we return a dict."""
    from src.core.independent_verifier import run_independent_verification
    result = run_independent_verification(
        worktree=tmp_path,
        test_command="this_command_does_not_exist_1234567",
        timeout_seconds=5,
    )
    assert result["passed"] is False
    assert result["exit_code"] != 0


def test_overturn_verdict_when_independent_check_fails(tmp_path):
    """The helper that converts a PASS verdict to FAIL when verifier disagrees."""
    from src.core.independent_verifier import overturn_if_failed

    qa_result = {"output": "VERDICT: PASS\nAll looks good", "cost": 0.5}
    verification = {
        "passed": False, "exit_code": 1, "stdout": "2 tests failed",
        "stderr": "", "timed_out": False, "skipped": False,
    }
    overturned = overturn_if_failed(qa_result, verification)
    assert "VERDICT: FAIL" in overturned["output"]
    assert "VERDICT: PASS" not in overturned["output"]
    assert "independent verification" in overturned["output"].lower()
    assert overturned.get("overturned_by_verifier") is True


def test_overturn_noop_when_verifier_passed(tmp_path):
    from src.core.independent_verifier import overturn_if_failed

    qa_result = {"output": "VERDICT: PASS\nAll good", "cost": 0.5}
    verification = {
        "passed": True, "exit_code": 0, "stdout": "5 passed",
        "stderr": "", "timed_out": False, "skipped": False,
    }
    unchanged = overturn_if_failed(qa_result, verification)
    assert unchanged is qa_result


def test_overturn_noop_when_verifier_skipped(tmp_path):
    """If no test command is configured, verifier can't overturn."""
    from src.core.independent_verifier import overturn_if_failed

    qa_result = {"output": "VERDICT: PASS\nOK", "cost": 0.5}
    verification = {"passed": True, "skipped": True, "exit_code": 0,
                    "stdout": "", "stderr": "", "timed_out": False}
    unchanged = overturn_if_failed(qa_result, verification)
    assert unchanged is qa_result
```

- [ ] **Step 2: Run and confirm failing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_independent_verifier.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `independent_verifier.py`**

Create `src/core/independent_verifier.py`:

```python
"""Independent verifier — orchestrator-owned test execution.

After the reviewer agent emits VERDICT: PASS, the orchestrator runs the
configured test_command via subprocess directly. If the independent run
fails, the pass is overturned. This catches two drift modes:

  1. Reviewer missed a real failure (leniency / anchoring bias)
  2. Agent rewrote the test to make it pass (reward hacking — documented
     in ImpossibleBench for Claude 3.7 Sonnet)

This module is a small, self-contained subprocess wrapper. No LLM. No
orchestrator dependencies. Fully unit-testable.
"""

import subprocess
from pathlib import Path
from typing import Optional


def run_independent_verification(
    worktree: Path,
    test_command: Optional[str],
    timeout_seconds: int = 900,
) -> dict:
    """Run test_command in worktree and capture the outcome.

    Returns a dict with:
        passed: bool         — True iff exit_code == 0 (or skipped)
        exit_code: int       — shell exit code
        stdout: str          — up to 4000 chars tail
        stderr: str          — up to 4000 chars tail
        timed_out: bool      — True iff the command was killed by timeout
        skipped: bool        — True iff no command was configured
    """
    if not test_command or not test_command.strip():
        return {
            "passed": True,
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "skipped": True,
        }

    try:
        proc = subprocess.run(
            test_command,
            shell=True,
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return {
            "passed": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": (proc.stdout or "")[-4000:],
            "stderr": (proc.stderr or "")[-4000:],
            "timed_out": False,
            "skipped": False,
        }
    except subprocess.TimeoutExpired:
        return {
            "passed": False,
            "exit_code": 124,
            "stdout": "",
            "stderr": f"Independent verifier timed out after {timeout_seconds}s",
            "timed_out": True,
            "skipped": False,
        }
    except Exception as e:
        return {
            "passed": False,
            "exit_code": 1,
            "stdout": "",
            "stderr": f"Independent verifier failed to launch: {e}",
            "timed_out": False,
            "skipped": False,
        }


def overturn_if_failed(qa_result: dict, verification: dict) -> dict:
    """Rewrite qa_result to VERDICT: FAIL if independent verification failed.

    Returns the original qa_result untouched if:
      - verification was skipped (no test command)
      - verification passed

    Otherwise returns a new dict with:
      - output rewritten: "VERDICT: FAIL\\n<verification context>"
      - overturned_by_verifier: True
      - original_output preserved for logging
    """
    if verification.get("skipped") or verification.get("passed"):
        return qa_result

    original = qa_result.get("output", "")
    stdout_tail = verification.get("stdout", "")[-1500:]
    stderr_tail = verification.get("stderr", "")[-1500:]
    exit_code = verification.get("exit_code", -1)

    new_output = (
        f"VERDICT: FAIL\n"
        f"Overturned by independent verification.\n"
        f"The reviewer agent emitted PASS but the orchestrator ran the test\n"
        f"command directly and it failed with exit code {exit_code}.\n\n"
        f"--- stdout (tail) ---\n{stdout_tail}\n\n"
        f"--- stderr (tail) ---\n{stderr_tail}\n\n"
        f"--- original reviewer output (preserved) ---\n{original[:2000]}\n"
    )

    new_result = dict(qa_result)
    new_result["output"] = new_output
    new_result["overturned_by_verifier"] = True
    new_result["original_output"] = original
    return new_result
```

- [ ] **Step 4: Run and confirm passing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_independent_verifier.py -v
```

Expected: all 8 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/core/independent_verifier.py tests/test_independent_verifier.py
git commit -m "feat(fidelity): add independent_verifier — orchestrator runs tests itself

Overturns a reviewer's VERDICT: PASS when the test command fails in an
independent subprocess run. Catches reviewer leniency and reward-hacking
(agent rewrites test to pass). Pure subprocess wrapper, no LLM, fully
unit-testable."
```

---

## Task 3: Wire Phase 1 gates into the orchestrator (Phase 1)

**Goal:** Plumb `drift_scanner` and `independent_verifier` into the 4 VERDICT handling sites in `orchestrator.py`. When the reviewer says PASS, we:
1. Run the drift scanner on the task's scope files
2. If any findings, record a drift incident, overturn to FAIL with drift findings as feedback
3. If scanner is clean, run independent verifier on the test command
4. If verifier fails, overturn to FAIL with verifier output as feedback
5. Only if both gates pass does the task actually merge

**Files:**
- Modify: `src/core/orchestrator.py` (4 VERDICT sites + helper)
- Modify: `src/config.yaml` (new `spec_fidelity` block)

- [ ] **Step 1: Confirm the 4 VERDICT sites**

First verify the site locations. Run:

```bash
cd /Users/prashantpandey/harness && grep -n 'VERDICT:' src/core/orchestrator.py
```

Expected output:
```
1044:        if "VERDICT: FAIL" in qa_result.get("output", ""):
1084:        # QA passed -- safe to merge      (← this is where we inject gate)
1329:            if "VERDICT: FAIL" not in eval_result.get("output", ""):
1494:            if "VERDICT: FAIL" in eval_result.get("output", ""):
2172:            if "VERDICT: FAIL" in eval_result["output"]:
2193:                tracker.feature_fail(feature_id, feature_desc, "VERDICT: FAIL")
```

The gate must go **before** the PASS handling at each site. Sites where PASS is the implicit branch:
- `1084` (wave loop, after `1044`'s FAIL branch exits early)
- `1329` (inverted check — PASS branch is inside the if)
- `1494`/`2172` (explicit FAIL branches — the PASS is the else/fall-through)

- [ ] **Step 2: Add a helper function near the top of orchestrator.py**

Add at the top of `src/core/orchestrator.py`, after the existing imports and `_metrics_from` helper (around line 82):

```python
from .drift_scanner import scan_for_drift
from .independent_verifier import run_independent_verification, overturn_if_failed
```

Then add this helper function (around line 107, next to `_log_error`):

```python
def _apply_phase1_gates(
    qa_result: dict,
    worktree: Path,
    feature: dict,
    test_command: Optional[str],
    config: dict,
    drift_counter,
    state_dir: Path,
) -> dict:
    """Apply Phase 1 mechanical gates (drift scanner + independent verifier).

    Runs ONLY when the reviewer emitted a PASS verdict. If either gate
    catches a problem, qa_result is rewritten to VERDICT: FAIL with
    detailed feedback for the next retry loop.

    Drift findings and verifier failures are written to feedback.md in
    the worktree so the next generator retry sees them.

    Returns the (possibly overturned) qa_result dict.
    """
    # Skip gating if verdict is already FAIL — nothing to overturn
    if "VERDICT: PASS" not in qa_result.get("output", ""):
        return qa_result

    _fidelity_cfg = config.get("spec_fidelity", {})
    _feature_id = feature.get("id", "unknown")
    _scope = feature.get("scope", []) or []

    # Gate 1: drift scanner
    if _fidelity_cfg.get("drift_scanner", {}).get("enabled", True) and _scope:
        findings = scan_for_drift(
            worktree=Path(worktree),
            scope=_scope,
            include_tests=_fidelity_cfg.get("drift_scanner", {}).get("include_tests", False),
        )
        if findings:
            # Record each finding in drift counter
            for f in findings:
                drift_counter.record("todo_marker", feature_id=_feature_id, details=f)
            # Build feedback for next retry
            _lines = [
                f"## Drift scanner found {len(findings)} simplification marker(s)",
                "",
                "The reviewer emitted PASS but the orchestrator's drift scanner",
                "found unfinished-work markers in your scope files. You cannot",
                "complete a task with TODO, FIXME, NotImplementedError, or similar",
                "markers in the implementation. Remove every marker below by",
                "actually implementing the work.",
                "",
            ]
            for f in findings[:30]:  # cap in feedback to 30
                _lines.append(f"- `{f['file']}:{f['line']}` [{f['marker']}] {f['snippet']}")
            if len(findings) > 30:
                _lines.append(f"... and {len(findings) - 30} more (see logs/drift.jsonl)")
            _feedback = "\n".join(_lines)

            # Write to feedback.md in worktree for next retry
            try:
                (Path(worktree) / "feedback.md").write_text(_feedback)
            except OSError as e:
                _log_error(state_dir, f"drift_feedback_write:{_feature_id}", e)

            # Overturn verdict
            _original = qa_result.get("output", "")
            _new = dict(qa_result)
            _new["output"] = (
                f"VERDICT: FAIL\n"
                f"Overturned by drift scanner ({len(findings)} markers found).\n\n"
                f"{_feedback}\n\n"
                f"--- original reviewer output (preserved) ---\n{_original[:1500]}\n"
            )
            _new["overturned_by_drift_scanner"] = True
            _new["drift_findings_count"] = len(findings)
            return _new

    # Gate 2: independent verifier (tests run by orchestrator)
    if _fidelity_cfg.get("independent_verifier", {}).get("enabled", True):
        verification = run_independent_verification(
            worktree=Path(worktree),
            test_command=test_command,
            timeout_seconds=_fidelity_cfg.get("independent_verifier", {}).get("timeout_seconds", 900),
        )
        if not verification.get("passed") and not verification.get("skipped"):
            drift_counter.record("independent_test_failure",
                                 feature_id=_feature_id,
                                 details={
                                     "exit_code": verification.get("exit_code"),
                                     "timed_out": verification.get("timed_out"),
                                     "stderr_tail": verification.get("stderr", "")[-400:],
                                 })
            overturned = overturn_if_failed(qa_result, verification)
            # Persist feedback for next retry
            try:
                (Path(worktree) / "feedback.md").write_text(overturned["output"])
            except OSError as e:
                _log_error(state_dir, f"verifier_feedback_write:{_feature_id}", e)
            return overturned

    return qa_result
```

- [ ] **Step 3: Wire the helper into the 4 VERDICT sites**

Site 1 — wave loop at `orchestrator.py:1041` area. Find:

```python
        cost_tracker.record("evaluator", qa_result.get("cost", 0), **_metrics_from(qa_result))
        cost_tracker.record_feature(feature_id, "evaluator", qa_result.get("cost", 0))

        if "VERDICT: FAIL" in qa_result.get("output", ""):
```

Insert immediately before the `if "VERDICT: FAIL"` line:

```python
        # Phase 1 mechanical gates — may overturn PASS to FAIL
        qa_result = _apply_phase1_gates(
            qa_result=qa_result,
            worktree=wt_dir,
            feature=feature,
            test_command=config.get("evaluator", {}).get("test_suite_command"),
            config=config,
            drift_counter=drift_counter,
            state_dir=state_dir,
        )
```

Sites 2, 3, 4 — the recovery/sequential paths at lines 1329, 1494, 2172. At each, find the nearest `cost_tracker.record("evaluator", ...)` line and insert the same block immediately after it. Adapt variable names:
- Sites using `eval_result` instead of `qa_result`: pass `eval_result` to the helper and assign back to `eval_result`.
- Sites using `wt_dir` vs `worktree_dir`: pass whichever is in scope.

Verify wiring with:

```bash
cd /Users/prashantpandey/harness && grep -c '_apply_phase1_gates' src/core/orchestrator.py
```

Expected: `5` (1 function definition + 4 call sites).

- [ ] **Step 4: Add config block**

Append to `src/config.yaml`:

```yaml
# Spec Fidelity Kernel — mechanical gates that catch drift without LLM cost
spec_fidelity:
  drift_scanner:
    enabled: true
    include_tests: false  # TODO in tests is often legitimate
  independent_verifier:
    enabled: true
    timeout_seconds: 900  # 15 minutes for the full test suite
```

- [ ] **Step 5: Run the full suite**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/ -q
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/core/orchestrator.py src/config.yaml
git commit -m "feat(fidelity): wire Phase 1 gates into orchestrator VERDICT sites

After reviewer emits PASS, orchestrator now runs drift_scanner + independent
verifier before merging. Overturns PASS to FAIL if either catches a drift
signal. Feedback is written to worktree/feedback.md so the next retry sees
the specific issues. All drift incidents recorded via DriftCounter."
```

---

## Task 4: AC-ID schema + assignment (Phase 2)

**Goal:** Give every acceptance criterion a stable ID (`AC-001`, `AC-002`, ...) so implementation can map artifacts back to criteria. Right now AC are unordered strings in a list — no way to say "AC-003 was satisfied by file X line Y."

**Files:**
- Create: `src/core/ac_coverage.py`
- Modify: `src/core/work_plan.py` (assign IDs when loading, preserve across refiner)
- Test: `tests/test_ac_coverage.py` (NEW)

- [ ] **Step 1: Write the failing test**

Create `tests/test_ac_coverage.py`:

```python
"""Tests for ac_coverage — AC-ID assignment and coverage map validation."""

import json
import pytest


def test_assign_ac_ids_on_plain_strings():
    from src.core.ac_coverage import assign_ac_ids
    task = {
        "id": "task-001",
        "acceptance_criteria": [
            "User can register with email",
            "Password hashed with bcrypt",
            "Email verified via token",
        ],
    }
    result = assign_ac_ids(task)
    ac = result["acceptance_criteria"]
    assert len(ac) == 3
    assert ac[0]["id"] == "task-001-AC-001"
    assert ac[0]["text"] == "User can register with email"
    assert ac[1]["id"] == "task-001-AC-002"
    assert ac[2]["id"] == "task-001-AC-003"


def test_assign_ac_ids_preserves_existing_ids():
    from src.core.ac_coverage import assign_ac_ids
    task = {
        "id": "task-001",
        "acceptance_criteria": [
            {"id": "task-001-AC-001", "text": "existing criterion"},
            "newly added criterion",
        ],
    }
    result = assign_ac_ids(task)
    ac = result["acceptance_criteria"]
    assert ac[0]["id"] == "task-001-AC-001"
    assert ac[1]["id"] == "task-001-AC-002"  # newly allocated


def test_assign_ac_ids_empty_list():
    from src.core.ac_coverage import assign_ac_ids
    task = {"id": "task-001", "acceptance_criteria": []}
    result = assign_ac_ids(task)
    assert result["acceptance_criteria"] == []


def test_assign_ac_ids_uses_next_available_number():
    """When there's a gap in existing IDs, we append sequentially."""
    from src.core.ac_coverage import assign_ac_ids
    task = {
        "id": "task-001",
        "acceptance_criteria": [
            {"id": "task-001-AC-001", "text": "one"},
            {"id": "task-001-AC-005", "text": "five"},
            "newly added without id",
        ],
    }
    result = assign_ac_ids(task)
    ac = result["acceptance_criteria"]
    # The new one should get the next number after the max existing (5)
    assert ac[2]["id"] == "task-001-AC-006"


def test_coverage_map_all_covered():
    from src.core.ac_coverage import validate_coverage_map
    ac_list = [
        {"id": "task-1-AC-001", "text": "a"},
        {"id": "task-1-AC-002", "text": "b"},
    ]
    coverage_map = {
        "task-1-AC-001": {"impl": "a.go:10", "test": "a_test.go:5"},
        "task-1-AC-002": {"impl": "b.go:20", "test": "b_test.go:8"},
    }
    result = validate_coverage_map(ac_list, coverage_map)
    assert result["complete"] is True
    assert result["missing"] == []


def test_coverage_map_missing_ac():
    from src.core.ac_coverage import validate_coverage_map
    ac_list = [
        {"id": "task-1-AC-001", "text": "a"},
        {"id": "task-1-AC-002", "text": "b"},
        {"id": "task-1-AC-003", "text": "c"},
    ]
    coverage_map = {
        "task-1-AC-001": {"impl": "a.go:10", "test": "a_test.go:5"},
    }
    result = validate_coverage_map(ac_list, coverage_map)
    assert result["complete"] is False
    assert "task-1-AC-002" in result["missing"]
    assert "task-1-AC-003" in result["missing"]


def test_coverage_map_null_impl_is_missing():
    from src.core.ac_coverage import validate_coverage_map
    ac_list = [{"id": "task-1-AC-001", "text": "a"}]
    coverage_map = {
        "task-1-AC-001": {"impl": None, "test": "a_test.go:5"},
    }
    result = validate_coverage_map(ac_list, coverage_map)
    assert result["complete"] is False
    assert "task-1-AC-001" in result["missing"]


def test_coverage_map_artifact_must_exist(tmp_path):
    """When we pass worktree_for_verification, we actually check the files."""
    from src.core.ac_coverage import validate_coverage_map
    (tmp_path / "real.go").write_text("x = 1")

    ac_list = [
        {"id": "task-1-AC-001", "text": "a"},
        {"id": "task-1-AC-002", "text": "b"},
    ]
    coverage_map = {
        "task-1-AC-001": {"impl": "real.go", "test": None},
        "task-1-AC-002": {"impl": "phantom.go", "test": None},
    }
    result = validate_coverage_map(ac_list, coverage_map, worktree=tmp_path)
    assert result["complete"] is False
    assert "task-1-AC-002" in result["missing"]
    assert "phantom.go" in str(result["phantom_artifacts"])


def test_read_verdict_json(tmp_path):
    from src.core.ac_coverage import read_harness_verdict
    (tmp_path / "HARNESS_VERDICT.json").write_text(json.dumps({
        "tests_passed": True,
        "build_ok": True,
        "confidence": "high",
        "criterion_coverage": {
            "task-1-AC-001": {"impl": "a.go:10", "test": "a_test.go:5"},
        },
    }))
    verdict = read_harness_verdict(tmp_path)
    assert verdict is not None
    assert verdict["confidence"] == "high"
    assert "task-1-AC-001" in verdict["criterion_coverage"]


def test_read_verdict_returns_none_when_missing(tmp_path):
    from src.core.ac_coverage import read_harness_verdict
    assert read_harness_verdict(tmp_path) is None


def test_read_verdict_tolerates_malformed_json(tmp_path):
    from src.core.ac_coverage import read_harness_verdict
    (tmp_path / "HARNESS_VERDICT.json").write_text("not json {{{")
    assert read_harness_verdict(tmp_path) is None
```

- [ ] **Step 2: Run and confirm failing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_ac_coverage.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `ac_coverage.py`**

Create `src/core/ac_coverage.py`:

```python
"""AC coverage — AC-ID generation and HARNESS_VERDICT.json validation.

Gives every acceptance criterion a stable ID so implementation artifacts
can be mapped back to criteria. The orchestrator uses this to reject
completion when any criterion is unmapped — no LLM judgment, pure dict
validation.

AC-IDs are scoped to tasks: `<task-id>-AC-<nnn>`. They are assigned
once (during work plan processing) and preserved across refiner loops.
"""

import json
import re
from pathlib import Path
from typing import Optional


_AC_ID_PATTERN = re.compile(r"AC-(\d+)$")


def _extract_ac_number(ac_id: str) -> int:
    """Return the numeric suffix of an AC-ID, or 0 if unparseable."""
    m = _AC_ID_PATTERN.search(ac_id)
    return int(m.group(1)) if m else 0


def assign_ac_ids(task: dict) -> dict:
    """Assign stable IDs to every acceptance_criteria entry on a task.

    Mutates and returns the task dict. Existing entries with an `id` field
    are preserved. Plain string entries are upgraded to `{id, text}` dicts.
    New IDs pick up from `max(existing) + 1` so refiner-added criteria
    never collide with prior IDs.
    """
    task_id = task.get("id", "task-unknown")
    raw = task.get("acceptance_criteria", []) or []

    # First pass: find the highest existing AC number
    max_existing = 0
    for entry in raw:
        if isinstance(entry, dict) and "id" in entry:
            max_existing = max(max_existing, _extract_ac_number(entry["id"]))

    # Second pass: upgrade strings, preserve dicts, allocate new IDs
    upgraded: list[dict] = []
    next_number = max_existing + 1
    for entry in raw:
        if isinstance(entry, dict) and "id" in entry:
            # Preserve existing
            upgraded.append({
                "id": entry["id"],
                "text": entry.get("text", ""),
                "verification": entry.get("verification", "test"),
            })
        elif isinstance(entry, str):
            upgraded.append({
                "id": f"{task_id}-AC-{next_number:03d}",
                "text": entry,
                "verification": "test",
            })
            next_number += 1
        elif isinstance(entry, dict):
            # Dict without id — allocate one, keep text
            upgraded.append({
                "id": f"{task_id}-AC-{next_number:03d}",
                "text": entry.get("text", json.dumps(entry)),
                "verification": entry.get("verification", "test"),
            })
            next_number += 1

    task["acceptance_criteria"] = upgraded
    return task


def validate_coverage_map(
    ac_list: list[dict],
    coverage_map: dict,
    worktree: Optional[Path] = None,
) -> dict:
    """Validate that every AC-ID in ac_list has a non-null entry in coverage_map.

    Args:
        ac_list: List of AC dicts (each with `id` field).
        coverage_map: Dict mapping AC-ID to `{impl, test}` or similar.
        worktree: If provided, also verify the `impl` file paths actually
                  exist on disk. Catches phantom artifact claims.

    Returns:
        {
            complete: bool,
            missing: [AC-IDs with null/absent coverage],
            phantom_artifacts: [paths claimed but not present on disk],
        }
    """
    missing: list[str] = []
    phantom: list[str] = []

    for ac in ac_list:
        ac_id = ac.get("id", "")
        if not ac_id:
            continue
        entry = coverage_map.get(ac_id)
        if not entry or not isinstance(entry, dict):
            missing.append(ac_id)
            continue
        impl = entry.get("impl")
        if impl is None or impl == "":
            missing.append(ac_id)
            continue
        # Optional file existence check
        if worktree is not None:
            # impl may be "path" or "path:line_range" — extract path
            impl_path = str(impl).split(":", 1)[0]
            if not (worktree / impl_path).exists():
                phantom.append(impl_path)
                if ac_id not in missing:
                    missing.append(ac_id)

    return {
        "complete": len(missing) == 0,
        "missing": missing,
        "phantom_artifacts": phantom,
    }


def read_harness_verdict(worktree: Path) -> Optional[dict]:
    """Read and parse HARNESS_VERDICT.json from the worktree root.

    Returns None if missing or unparseable. Never raises.
    """
    path = worktree / "HARNESS_VERDICT.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
```

- [ ] **Step 4: Run and confirm passing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_ac_coverage.py -v
```

Expected: all 11 PASS.

- [ ] **Step 5: Wire `assign_ac_ids` into work_plan.py**

In `src/core/work_plan.py`, find the `WorkPlan` class loading code (grep for `def load` or `class WorkPlan`). After tasks are loaded, call `assign_ac_ids` on each:

Add at top:
```python
from .ac_coverage import assign_ac_ids
```

In `WorkPlan.load` or wherever tasks are iterated on load, add:

```python
for phase in self.data.get("phases", []):
    for epic in phase.get("epics", []):
        for story in epic.get("stories", []):
            for task in story.get("tasks", []):
                assign_ac_ids(task)
```

Call `assign_ac_ids` ONLY at load time — once IDs are assigned, they're persisted in work_plan.json and re-loaded stably on subsequent runs.

- [ ] **Step 6: Run the full suite**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/ -q
```

Expected: all PASS. If existing tests fail because they expected AC as plain strings, update them to expect the `{id, text}` dict form.

- [ ] **Step 7: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/core/ac_coverage.py src/core/work_plan.py tests/test_ac_coverage.py
git commit -m "feat(fidelity): AC-ID system for stable criterion identification

Every acceptance criterion gets a scoped ID (task-NNN-AC-MMM) on work
plan load. Plain string criteria are upgraded to {id, text, verification}
dicts. Refiner additions get fresh IDs past the max existing number.
Adds validate_coverage_map() for checking HARNESS_VERDICT.json against
the task's AC list, including optional worktree file existence check."
```

---

## Task 5: HARNESS_VERDICT.json enforcement + generator prompt update (Phase 2)

**Goal:** Require the generator to emit `HARNESS_VERDICT.json` with `criterion_coverage` mapping every AC-ID to an implementation artifact. Orchestrator rejects completion if the map is incomplete. This is the structural gate that forces the generator to account for each criterion — no hand-waving.

**Files:**
- Modify: `src/prompts/generator-v2.md` (require HARNESS_VERDICT.json)
- Modify: `src/core/parallel.py` (include AC-IDs explicitly in TASK_BRIEF.md)
- Modify: `src/core/orchestrator.py` (extend `_apply_phase1_gates` to check coverage map)

- [ ] **Step 1: Write a failing orchestrator-level test**

Append to `tests/test_ac_coverage.py`:

```python
def test_verdict_gate_rejects_when_coverage_incomplete(tmp_path):
    """Integration: verdict file present but missing AC mapping → rejection."""
    import json as _json
    from src.core.ac_coverage import read_harness_verdict, validate_coverage_map

    (tmp_path / "HARNESS_VERDICT.json").write_text(_json.dumps({
        "tests_passed": True,
        "build_ok": True,
        "confidence": "high",
        "criterion_coverage": {
            "task-1-AC-001": {"impl": "a.go", "test": "a_test.go"},
            # AC-002 missing
        },
    }))
    (tmp_path / "a.go").write_text("package main")
    (tmp_path / "a_test.go").write_text("package main")

    verdict = read_harness_verdict(tmp_path)
    ac_list = [
        {"id": "task-1-AC-001", "text": "one"},
        {"id": "task-1-AC-002", "text": "two"},
    ]
    result = validate_coverage_map(
        ac_list,
        verdict["criterion_coverage"],
        worktree=tmp_path,
    )
    assert result["complete"] is False
    assert "task-1-AC-002" in result["missing"]


def test_verdict_gate_rejects_when_no_verdict_file(tmp_path):
    """No HARNESS_VERDICT.json at all → treated as missing."""
    from src.core.ac_coverage import read_harness_verdict
    assert read_harness_verdict(tmp_path) is None
```

- [ ] **Step 2: Run and confirm failing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_ac_coverage.py -v
```

Expected: these new tests PASS immediately (we already implemented the functions in Task 4). This step is a sanity check that the Task 4 implementation handles the orchestrator-integration case cleanly.

- [ ] **Step 3: Update the generator prompt**

Edit `src/prompts/generator-v2.md`. Find the section before the `---HARNESS_STATUS---` block (around STEP 5b). Insert a new STEP 5a-prime:

Find:

```markdown
### 5a. Commit
```bash
git add .
git commit -m "feat: <feature-id> — <feature description>"
```
```

Replace with:

```markdown
### 5a. Write HARNESS_VERDICT.json (MANDATORY)

Before committing, write a file `HARNESS_VERDICT.json` in the worktree root.
This file MAPS every acceptance criterion in your task brief to the exact
file(s) and test(s) that satisfy it. The orchestrator will reject completion
if any AC-ID from your task brief is missing from `criterion_coverage`.

Schema:

```json
{
  "tests_passed": true,
  "build_ok": true,
  "confidence": "high",
  "criterion_coverage": {
    "<task-id>-AC-001": {
      "impl": "path/to/file.go:10-42",
      "test": "path/to/file_test.go:TestSomething"
    },
    "<task-id>-AC-002": {
      "impl": "path/to/other.go:5",
      "test": "path/to/other_test.go:TestOther"
    }
  },
  "notes": []
}
```

**Rules:**
- You MUST include an entry for EVERY AC-ID listed in your task brief.
- `impl` is the file (optionally `file:line_range`) where the criterion is satisfied.
- `test` is the file (optionally `file:test_name`) that verifies it.
- `confidence: "high"` means you personally ran the test command and saw it pass AND every AC-ID is genuinely satisfied.
- `confidence: "low"` means you deferred, partial, or uncertain — the orchestrator will route through full LLM review.
- If you cannot map an AC-ID to a real file, you have NOT completed the task. Do NOT write `null` — go back and implement the criterion.

If `HARNESS_VERDICT.json` is missing or incomplete, the orchestrator will
reject your work AND write feedback.md with the specific gaps. Be honest.

### 5b. Commit

```bash
git add .
git commit -m "feat: <feature-id> — <feature description>"
```
```

- [ ] **Step 4: Update `parallel.py:write_task_brief` to include AC-IDs**

In `src/core/parallel.py`, find the block that writes the AC section (around line 192):

```python
    if ac:
        lines += ["## Acceptance Criteria", "", ac if isinstance(ac, str) else "\n".join(f"- {a}" for a in ac), ""]
```

Replace with:

```python
    if ac:
        lines += ["## Acceptance Criteria (each MUST be mapped in HARNESS_VERDICT.json)", ""]
        if isinstance(ac, str):
            # Legacy plain-string AC — no IDs available
            lines.append(ac)
        else:
            for a in ac:
                if isinstance(a, dict):
                    _id = a.get("id", "AC-???")
                    _text = a.get("text", "")
                    lines.append(f"- **{_id}**: {_text}")
                else:
                    lines.append(f"- {a}")
        lines.append("")
        lines.append("Every AC-ID above MUST appear as a key in HARNESS_VERDICT.json's")
        lines.append("`criterion_coverage` object with non-null `impl` and `test` fields.")
        lines.append("")
```

- [ ] **Step 5: Extend `_apply_phase1_gates` with the coverage check**

In `src/core/orchestrator.py`, add to the imports near the top:

```python
from .ac_coverage import read_harness_verdict, validate_coverage_map
```

Then in the `_apply_phase1_gates` function added in Task 3, insert a new gate section BEFORE the existing drift-scanner gate:

```python
    # Gate 0: HARNESS_VERDICT.json must exist and cover every AC-ID
    if _fidelity_cfg.get("verdict_gate", {}).get("enabled", True):
        verdict = read_harness_verdict(Path(worktree))
        ac_list = feature.get("acceptance_criteria", []) or []
        # Only run gate if we have dict-form AC (legacy plain strings opt out)
        structured_ac = [a for a in ac_list if isinstance(a, dict) and "id" in a]
        if structured_ac:
            if verdict is None:
                drift_counter.record("ac_uncovered",
                                     feature_id=_feature_id,
                                     details={"reason": "no HARNESS_VERDICT.json"})
                _feedback = (
                    "## HARNESS_VERDICT.json is missing\n\n"
                    "You must write `HARNESS_VERDICT.json` in the worktree root\n"
                    "before committing. It must map every AC-ID from your task\n"
                    "brief to a real implementation file and test.\n\n"
                    "See your TASK_BRIEF.md for the list of AC-IDs that must be mapped.\n"
                )
                try:
                    (Path(worktree) / "feedback.md").write_text(_feedback)
                except OSError:
                    pass
                _new = dict(qa_result)
                _new["output"] = (
                    f"VERDICT: FAIL\nOverturned: HARNESS_VERDICT.json missing.\n\n{_feedback}"
                )
                _new["overturned_by_verdict_gate"] = True
                return _new

            coverage = verdict.get("criterion_coverage", {})
            validation = validate_coverage_map(
                structured_ac,
                coverage,
                worktree=Path(worktree),
            )
            if not validation["complete"]:
                for ac_id in validation["missing"]:
                    drift_counter.record("ac_uncovered",
                                         feature_id=_feature_id,
                                         details={"ac_id": ac_id})
                for phantom in validation["phantom_artifacts"]:
                    drift_counter.record("ac_mapped_but_missing",
                                         feature_id=_feature_id,
                                         details={"path": phantom})
                _lines = [
                    "## HARNESS_VERDICT.json is incomplete",
                    "",
                    f"Missing or null coverage for {len(validation['missing'])} AC-ID(s):",
                    "",
                ]
                for ac_id in validation["missing"]:
                    _text = next((a["text"] for a in structured_ac if a["id"] == ac_id), "")
                    _lines.append(f"- **{ac_id}**: {_text}")
                if validation["phantom_artifacts"]:
                    _lines.append("")
                    _lines.append("These paths are claimed in the map but don't exist:")
                    for p in validation["phantom_artifacts"]:
                        _lines.append(f"- `{p}`")
                _lines.append("")
                _lines.append("Implement each missing criterion and update HARNESS_VERDICT.json.")
                _feedback = "\n".join(_lines)
                try:
                    (Path(worktree) / "feedback.md").write_text(_feedback)
                except OSError:
                    pass
                _new = dict(qa_result)
                _new["output"] = (
                    f"VERDICT: FAIL\nOverturned by AC coverage gate.\n\n{_feedback}\n\n"
                    f"--- original reviewer output ---\n{qa_result.get('output', '')[:1500]}"
                )
                _new["overturned_by_verdict_gate"] = True
                _new["missing_ac_ids"] = validation["missing"]
                return _new
```

- [ ] **Step 6: Update config with `verdict_gate` knob**

In `src/config.yaml`, extend the `spec_fidelity` block:

```yaml
spec_fidelity:
  verdict_gate:
    enabled: true
    require_file_existence: true  # Phantom artifact check
  drift_scanner:
    enabled: true
    include_tests: false
  independent_verifier:
    enabled: true
    timeout_seconds: 900
```

- [ ] **Step 7: Run the full suite**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/ -q
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/prompts/generator-v2.md src/core/parallel.py src/core/orchestrator.py src/config.yaml tests/test_ac_coverage.py
git commit -m "feat(fidelity): require HARNESS_VERDICT.json with per-AC coverage map

Generator must now emit HARNESS_VERDICT.json mapping every AC-ID from
the task brief to a real impl file and test. Orchestrator rejects
completion if any AC is unmapped or points to a phantom file. Feedback
is written to worktree/feedback.md listing the specific missing AC-IDs.

This is the structural gate that forces the generator to account for
every criterion — no more 'implemented the easy 2 of 5 criteria' drift."
```

---

## Task 6: Update planner prompts to preserve AC-IDs (Phase 2)

**Goal:** The architect and refiner currently emit AC as plain strings. With Task 4's `assign_ac_ids()` running at load time, strings get IDs automatically. But if the refiner DROPS or REORDERS criteria during refinement, the IDs get reshuffled. We need planner prompts to preserve explicit IDs when they refine.

**Files:**
- Modify: `src/prompts/architect.md` (hint but don't require — architect produces draft)
- Modify: `src/prompts/refiner.md` (MUST preserve IDs on existing AC)
- Modify: `src/prompts/validator.md` (check ID stability)

- [ ] **Step 1: Update architect.md**

In `src/prompts/architect.md`, find the example JSON block that shows `acceptance_criteria: ["..."]`. Replace with:

```json
                  "acceptance_criteria": [
                    {"id": "task-001-AC-001", "text": "package.json exists"},
                    {"id": "task-001-AC-002", "text": "npm install succeeds without errors"},
                    {"id": "task-001-AC-003", "text": "All required packages are listed"}
                  ],
```

And in the rules section, add:

```markdown
### AC-ID format
- Every acceptance_criteria entry is an object with `id` and `text` fields.
- ID format: `<task-id>-AC-<nnn>` where nnn is zero-padded sequential (001, 002, ...).
- If you emit AC as plain strings the loader will assign IDs automatically,
  but emitting them explicitly is preferred.
```

- [ ] **Step 2: Update refiner.md**

In `src/prompts/refiner.md`, find the "Task Quality Requirements" section. Add a new bullet:

```markdown
- **AC-ID stability**: If the architect's draft already has `id` fields on acceptance_criteria entries, you MUST preserve those exact IDs on the corresponding refined entries. When you add NEW criteria (e.g., to address a gap), allocate new IDs using `<task-id>-AC-<nnn>` where nnn is one greater than the highest existing AC number for that task. Never renumber existing AC-IDs — downstream tooling depends on stability.
```

Also in the "Address Every Gap" section, add:

```markdown
When tightening existing AC to address a gap, keep the original AC-ID and extend the `text` field. Example:
- Before: `{"id": "task-001-AC-002", "text": "User can register"}`
- After:  `{"id": "task-001-AC-002", "text": "User can register with email verification and rate limiting"}`

Do NOT drop the AC and add a new one — tighten the text in place.
```

- [ ] **Step 3: Update validator.md**

In `src/prompts/validator.md`, add a new check under "You MUST set `sign_off: false` if ANY of these are true":

```markdown
8. Any AC-ID from the architect's draft is missing from the refined work_plan.json (the refiner is not allowed to drop criteria silently).
9. Any two acceptance_criteria entries share the same `id` field (IDs must be unique within the work plan).
```

And extend the `validation.json` schema example with:

```json
  "ac_id_stability": {
    "valid": true,
    "dropped_ids": [],
    "duplicate_ids": []
  },
```

- [ ] **Step 4: No tests for this step — prompts are not unit-tested**

Prompt changes are validated by actually running a planning pipeline in a test project (Task 11 covers integration testing).

- [ ] **Step 5: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/prompts/architect.md src/prompts/refiner.md src/prompts/validator.md
git commit -m "feat(fidelity): planner prompts preserve AC-IDs across refinement

Architect emits AC as {id, text} objects. Refiner MUST keep IDs stable
on preserved criteria and allocate new IDs for additions (never renumber).
Validator adds AC-ID stability check to sign-off rules."
```

---

## Phase 1 + Phase 2 shipment checkpoint

After Tasks 0 through 6 land, the harness has:
- A drift counter logging every incident
- Programmatic TODO/unimplemented scanner
- Orchestrator-owned test runner (defeats reward hacking)
- AC-IDs with stable mapping
- HARNESS_VERDICT.json required with per-AC coverage
- Planner prompts preserving IDs

**Measure before continuing.** Run the harness on a real feature. Read `.harness/runs/<run-id>/logs/drift.jsonl` and the end-of-run drift summary. If the `by_kind` counts are dropping significantly compared to pre-fidelity runs, Phase 3 may not be necessary. If specific kinds are still dominant, use that data to decide which Phase 3+ item to build next.

---

## Task 7: Fresh reviewer on eval failure (Phase 3)

**Goal:** Break the anchoring-bias loop where the same reviewer that missed a gap on attempt 1 tends to miss it again on attempt 2. On eval-fail retry, spawn a reviewer with a different prompt variant explicitly told: "a previous reviewer said this passed. It didn't. Find what they missed."

**Files:**
- Create: `src/prompts/reviewer-fresh.md` (alternate reviewer prompt for retries)
- Modify: `src/core/orchestrator.py` (use alternate prompt on retry)
- Modify: `src/config.yaml` (new `spec_fidelity.fresh_reviewer.enabled` knob)

- [ ] **Step 1: Write the alternate reviewer prompt**

Create `src/prompts/reviewer-fresh.md`:

```markdown
## YOUR ROLE — SECOND-LOOK REVIEWER

You are a second-pass reviewer reviewing work that a PREVIOUS reviewer
already approved. **The previous reviewer missed something.** Your job
is to find it.

This is NOT a fresh review. Start from the assumption that there is a
gap, a simplification, a deferred criterion, or a reward hack somewhere
in this work. Your job is to FIND IT.

## CRITICAL RULES

- A previous reviewer said PASS. Something was missed. Do NOT assume they were right.
- Look specifically for: simplified implementations, TODO markers, test files that were rewritten to pass, criteria that were paraphrased away.
- Read `feedback.md` if it exists — it describes what mechanical gates caught.
- Be skeptical. Be specific. Be hostile.

## THE FEATURE UNDER REVIEW

The feature ID, description, and acceptance criteria are in the user message.

## STEP 1: RE-RUN TESTS YOURSELF

```bash
{{TEST_COMMAND}}
```

Do NOT trust any assertion you see. Actually run it. If it fails, the
previous reviewer was wrong to approve.

## STEP 2: INSPECT FOR SIMPLIFICATION

For each acceptance criterion in the user message:

1. Find the `impl` file claimed to satisfy it in `HARNESS_VERDICT.json`.
2. Read that specific section of the file.
3. Ask: "Does this ACTUALLY do what the criterion describes, or is it a stub/facade/partial?"
4. If the criterion says "X with retries and backoff" and the code has a single try with no backoff, the criterion is NOT satisfied.
5. Search for: `TODO`, `FIXME`, `NotImplementedError`, `panic("unimplemented")`, `// rest of code`, `pass  # later`, `return nil  # stub`.

## STEP 3: INSPECT THE TEST FILES

Did the previous generator edit test files?

```bash
git log --oneline -5 -- '*_test.go' 'test_*.py' '*.test.ts' '*.spec.ts'
```

If tests were modified alongside implementation, READ those test modifications
specifically. Are they:
- Legitimate (testing new behavior)?
- Or are they test deletions / assertion weakenings / `assert True` garbage?

Reward hacking is common — agents sometimes edit tests to make them pass
rather than fix the code. Your job is to catch this.

## STEP 4: OUTPUT VERDICT

### If you STILL find no issues after genuine skepticism:

```
VERDICT: PASS
Second-look review confirms — no gaps found despite targeted search.
```

### If you find ANYTHING that the previous reviewer missed:

Write detailed feedback to `./feedback.md` with:
- What the previous reviewer missed
- Why it's a real gap (not cosmetic)
- The exact file:line location
- Specific fix instructions

And output:

```
VERDICT: FAIL
Second-look review caught <N> missed issues. See feedback.md.
```

## WHAT YOU MUST NOT DO

- Do NOT rubber-stamp. The previous reviewer already said PASS. Your job is to second-guess.
- Do NOT be charitable to ambiguity. If the code is unclear, assume the worst.
- Do NOT accept "mostly works" — the criterion is either satisfied or it isn't.
```

- [ ] **Step 2: Wire the alternate prompt into the retry path**

In `src/core/orchestrator.py`, find the area around the eval-fail retry loop at line 1067 (inside the wave loop). The retry currently uses the same reviewer system prompt. We need to use `reviewer-fresh.md` on attempts 2+.

Add near where the wave-level `qa_system_prompt` is loaded (around line 695):

```python
    # Fresh-reviewer system prompt for anti-anchoring on retries
    fresh_reviewer_enabled = config.get("spec_fidelity", {}).get("fresh_reviewer", {}).get("enabled", True)
    if fresh_reviewer_enabled and (prompts_dir / "reviewer-fresh.md").exists():
        _fresh_repl = dict(_qa_repl)
        fresh_qa_system_prompt = load_prompt(prompts_dir / "reviewer-fresh.md", _fresh_repl)
    else:
        fresh_qa_system_prompt = qa_system_prompt
```

Then inside `_pool_worker`, when running the reviewer, check attempt count and pick the appropriate system prompt:

Find the reviewer spawn (inside the gate-wrapper else branch from Task 5 in the token-efficiency plan, OR at line 912 if Phase 5 of the token-efficiency plan hasn't landed):

```python
                qa_options = create_client_options(wt_dir, config)
                try:
                    qa_result = await asyncio.wait_for(
                        run_agent_session(qa_user_msg, qa_options, wt_dir, system_prompt=qa_system_prompt),
                        timeout=qa_timeout,
                    )
```

Replace the `system_prompt=qa_system_prompt` with an attempt-aware choice:

```python
                qa_options = create_client_options(wt_dir, config)
                _attempt_count = _get_attempt_count(feature_id)
                _chosen_sp = fresh_qa_system_prompt if _attempt_count >= 1 else qa_system_prompt
                if _attempt_count >= 1 and fresh_reviewer_enabled:
                    print(f"  {feature_id}: reviewer attempt {_attempt_count + 1} — using second-look prompt")
                try:
                    qa_result = await asyncio.wait_for(
                        run_agent_session(qa_user_msg, qa_options, wt_dir, system_prompt=_chosen_sp),
                        timeout=qa_timeout,
                    )
```

Do the same at sites 1327, 1492, 2162 (the recovery and sequential paths) — all need the attempt-count-aware system prompt selection.

Verify wiring:

```bash
cd /Users/prashantpandey/harness && grep -c 'fresh_qa_system_prompt' src/core/orchestrator.py
```

Expected: at least 5 (1 definition + 4 call sites).

- [ ] **Step 3: Add config knob**

In `src/config.yaml`, extend `spec_fidelity`:

```yaml
spec_fidelity:
  verdict_gate:
    enabled: true
    require_file_existence: true
  drift_scanner:
    enabled: true
    include_tests: false
  independent_verifier:
    enabled: true
    timeout_seconds: 900
  fresh_reviewer:
    enabled: true  # Use second-look prompt on retry attempts
```

- [ ] **Step 4: Run the full suite**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/ -q
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/prompts/reviewer-fresh.md src/core/orchestrator.py src/config.yaml
git commit -m "feat(fidelity): fresh reviewer on retry breaks anchoring bias

On retry attempts, reviewer uses reviewer-fresh.md which explicitly
frames the work as 'a previous reviewer said PASS, find what they
missed'. Mitigates anchoring bias where the same reviewer that missed
a gap on attempt 1 tends to miss it again on attempt 2."
```

---

## Task 8: Per-feature adversary on the diff (Phase 4)

**Goal:** After the generator commits but before the reviewer runs, spawn a small adversary agent that reviews ONLY the generator's diff against the task's specific AC. The planning-time adversary already knows how to find "AC that an LLM will satisfy trivially" — we reuse that skill for post-implementation checking.

**Files:**
- Create: `src/prompts/adversary-diff.md`
- Modify: `src/core/orchestrator.py` (spawn adversary before reviewer)
- Modify: `src/config.yaml` (new `spec_fidelity.adversary_diff.enabled` knob)

- [ ] **Step 1: Write the per-feature adversary prompt**

Create `src/prompts/adversary-diff.md`:

```markdown
## YOUR ROLE — DIFF ADVERSARY

You review a generator agent's output and find simplifications, deferrals,
or stubs BEFORE the main reviewer gets involved. You are fast and hostile.

## YOUR INPUTS

- The task's acceptance criteria (in the user message, with AC-IDs)
- The generator's diff since the last commit on the base branch

Read them both. That's all you need.

## STEP 1: GET THE DIFF

```bash
git log --oneline -5
git diff HEAD~1 HEAD --stat
git diff HEAD~1 HEAD
```

## STEP 2: FOR EACH AC-ID, ASK:

For every AC-ID in the user message, answer these questions:

1. Which files in the diff implement this criterion?
2. Is the implementation complete, or does it have any of:
   - TODO/FIXME markers
   - A single case when multiple are required
   - A stub return value (`return nil`, `return None`, `pass`)
   - A test that asserts `True == True` or `assert 1 == 1`
   - A deferred comment ("will handle later", "next iteration")
3. If the AC says "with retries and backoff" or similar, is ALL of it implemented?

## STEP 3: OUTPUT REPORT

Write your findings to `./adversary_report.json`:

```json
{
  "verdict": "clean" | "simplified",
  "findings": [
    {
      "ac_id": "task-001-AC-002",
      "ac_text": "User can register with email verification and rate limiting",
      "what_is_missing": "Email verification is implemented but rate limiting is not present in the diff",
      "evidence": "src/routes/register.go — no rate limit middleware applied",
      "severity": "high"
    }
  ],
  "summary": "1 finding, 1 high-severity — rate limiting was deferred"
}
```

Rules:
- `verdict: "clean"` ONLY if every AC-ID is fully addressed.
- `verdict: "simplified"` if ANY AC-ID shows deferral, stubs, or partial implementation.
- Every finding MUST reference a specific AC-ID and file:line.
- Do NOT be charitable. If you're unsure, flag it.

## WHAT YOU MUST NOT DO

- Do NOT run tests (that's the reviewer's job; you're pre-checking)
- Do NOT modify any files
- Do NOT make suggestions — only report findings
- Do NOT produce any file other than `adversary_report.json`
```

- [ ] **Step 2: Wire the adversary into the orchestrator**

In `src/core/orchestrator.py`, near the other prompt loads (around line 695), add:

```python
    adversary_diff_enabled = config.get("spec_fidelity", {}).get("adversary_diff", {}).get("enabled", True)
    adversary_diff_system_prompt = None
    if adversary_diff_enabled and (prompts_dir / "adversary-diff.md").exists():
        adversary_diff_system_prompt = load_prompt(prompts_dir / "adversary-diff.md", wave_replacements)
```

Then inside `_pool_worker`, after the generator commits but BEFORE the reviewer runs, add:

```python
            # Per-feature adversary on the diff (before reviewer)
            adversary_report = None
            if (adversary_diff_enabled and adversary_diff_system_prompt
                    and gen_status == "ok" and branch_has_commits(project_dir, branch)):
                _adv_user_msg = _build_qa_user_msg(feature_id, work_plan)
                _adv_options = create_client_options(wt_dir, config)
                try:
                    _adv_result = await asyncio.wait_for(
                        run_agent_session(_adv_user_msg, _adv_options, wt_dir,
                                          system_prompt=adversary_diff_system_prompt),
                        timeout=300,  # 5 minutes — focused task
                    )
                    # Read the adversary's report from the worktree
                    _adv_report_path = Path(wt_dir) / "adversary_report.json"
                    if _adv_report_path.exists():
                        try:
                            adversary_report = json_mod.loads(_adv_report_path.read_text())
                        except Exception:
                            pass
                except asyncio.TimeoutError:
                    print(f"  Adversary-diff {feature_id}: timed out")

            # If adversary flagged simplification, treat as eval fail immediately
            if adversary_report and adversary_report.get("verdict") == "simplified":
                _findings = adversary_report.get("findings", [])
                for f in _findings:
                    drift_counter.record("adversary_diff_flagged",
                                         feature_id=feature_id,
                                         details=f)
                _feedback_lines = [
                    "## Adversary-diff caught simplifications",
                    "",
                    f"{len(_findings)} finding(s) — the generator did not fully implement every AC:",
                    "",
                ]
                for f in _findings:
                    _feedback_lines.append(f"### {f.get('ac_id', 'AC-???')}: {f.get('ac_text', '')}")
                    _feedback_lines.append(f"**Missing:** {f.get('what_is_missing', '')}")
                    _feedback_lines.append(f"**Evidence:** {f.get('evidence', '')}")
                    _feedback_lines.append(f"**Severity:** {f.get('severity', 'unknown')}")
                    _feedback_lines.append("")
                _feedback_lines.append("Fully implement each criterion above and re-run.")
                _feedback_text = "\n".join(_feedback_lines)
                (Path(wt_dir) / "feedback.md").write_text(_feedback_text)

                # Synthesize a FAIL qa_result so the retry loop kicks in
                qa_result = {
                    "output": f"VERDICT: FAIL\nOverturned by adversary-diff ({len(_findings)} findings).\n\n{_feedback_text}",
                    "cost": 0,
                    "usage": {},
                    "overturned_by_adversary_diff": True,
                    "adversary_report": adversary_report,
                }
                # Skip the main reviewer — adversary already caught it
            else:
                # Main reviewer runs as normal
                # ... existing reviewer call ...
                pass  # (keep the existing reviewer logic below)
```

This is an ADDITION before the existing reviewer call. The existing reviewer logic stays; we only skip it when the adversary has already caught something.

- [ ] **Step 3: Add config knob**

In `src/config.yaml`, extend `spec_fidelity`:

```yaml
spec_fidelity:
  # ... existing knobs ...
  adversary_diff:
    enabled: true
    timeout_seconds: 300
```

- [ ] **Step 4: Run the full suite**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/ -q
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/prompts/adversary-diff.md src/core/orchestrator.py src/config.yaml
git commit -m "feat(fidelity): per-feature adversary checks diff before reviewer

After the generator commits, a focused adversary agent reviews the diff
against each AC-ID looking for simplifications, stubs, and deferrals.
If the adversary flags findings, the reviewer is skipped and the task
goes straight to retry with specific feedback. Catches drift one LLM
call earlier than the reviewer would."
```

---

## Task 9: Structured JSON handoff (Phase 5)

**Goal:** Replace the markdown `---HARNESS_STATUS---` and `---HANDOFF---` blocks with a strict JSON schema validated by `jsonschema`. The generator's completion signal becomes a contract, not a regex match. Downstream code can't parse garbage and silently proceed.

**Files:**
- Create: `src/core/handoff_schema.py`
- Modify: `src/core/orchestrator.py` (parse structured handoff instead of markdown blocks)
- Modify: `src/prompts/generator-v2.md` (emit `HARNESS_HANDOFF.json` as the single source of truth)
- Test: `tests/test_handoff_schema.py` (NEW)

- [ ] **Step 1: Write the failing test**

Create `tests/test_handoff_schema.py`:

```python
"""Tests for the JSON handoff schema."""

import json
import pytest


def test_schema_accepts_minimal_valid_handoff():
    from src.core.handoff_schema import validate_handoff
    handoff = {
        "feature_id": "task-001",
        "status": "complete",
        "files_modified": ["src/a.go"],
        "tests_passed": True,
        "build_ok": True,
        "confidence": "high",
        "criterion_coverage": {},
        "decisions": [],
        "discoveries": [],
        "failures": [],
    }
    result = validate_handoff(handoff)
    assert result["valid"] is True


def test_schema_rejects_missing_required_field():
    from src.core.handoff_schema import validate_handoff
    handoff = {
        "feature_id": "task-001",
        # missing "status"
        "files_modified": [],
        "tests_passed": True,
        "build_ok": True,
        "confidence": "high",
        "criterion_coverage": {},
    }
    result = validate_handoff(handoff)
    assert result["valid"] is False
    assert any("status" in e.lower() for e in result["errors"])


def test_schema_rejects_invalid_confidence_value():
    from src.core.handoff_schema import validate_handoff
    handoff = {
        "feature_id": "task-001",
        "status": "complete",
        "files_modified": [],
        "tests_passed": True,
        "build_ok": True,
        "confidence": "medium",  # not in enum
        "criterion_coverage": {},
    }
    result = validate_handoff(handoff)
    assert result["valid"] is False


def test_schema_rejects_non_boolean_tests_passed():
    from src.core.handoff_schema import validate_handoff
    handoff = {
        "feature_id": "task-001",
        "status": "complete",
        "files_modified": [],
        "tests_passed": "yes",  # string instead of bool
        "build_ok": True,
        "confidence": "high",
        "criterion_coverage": {},
    }
    result = validate_handoff(handoff)
    assert result["valid"] is False


def test_read_handoff_returns_none_when_missing(tmp_path):
    from src.core.handoff_schema import read_handoff
    assert read_handoff(tmp_path) is None


def test_read_handoff_parses_valid_file(tmp_path):
    from src.core.handoff_schema import read_handoff
    (tmp_path / "HARNESS_HANDOFF.json").write_text(json.dumps({
        "feature_id": "task-001",
        "status": "complete",
        "files_modified": ["a.go"],
        "tests_passed": True,
        "build_ok": True,
        "confidence": "high",
        "criterion_coverage": {},
        "decisions": [],
        "discoveries": [],
        "failures": [],
    }))
    handoff = read_handoff(tmp_path)
    assert handoff is not None
    assert handoff["feature_id"] == "task-001"


def test_read_handoff_tolerates_malformed_json(tmp_path):
    from src.core.handoff_schema import read_handoff
    (tmp_path / "HARNESS_HANDOFF.json").write_text("not json {{{")
    assert read_handoff(tmp_path) is None
```

- [ ] **Step 2: Run and confirm failing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_handoff_schema.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `handoff_schema.py`**

Create `src/core/handoff_schema.py`:

```python
"""Structured handoff schema — replaces regex-parsed markdown blocks.

The generator emits HARNESS_HANDOFF.json in the worktree root with a
strict schema. The orchestrator validates against the schema and rejects
completion if the handoff is missing or malformed. No more silent
regex failures on "---HARNESS_STATUS---" drift.
"""

import json
from pathlib import Path
from typing import Optional


HANDOFF_SCHEMA = {
    "type": "object",
    "required": [
        "feature_id", "status", "files_modified", "tests_passed",
        "build_ok", "confidence", "criterion_coverage",
    ],
    "properties": {
        "feature_id": {"type": "string"},
        "status": {"type": "string", "enum": ["complete", "partial", "blocked"]},
        "files_modified": {
            "type": "array",
            "items": {"type": "string"},
        },
        "tests_passed": {"type": "boolean"},
        "build_ok": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["high", "low"]},
        "criterion_coverage": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "impl": {"type": ["string", "null"]},
                    "test": {"type": ["string", "null"]},
                },
            },
        },
        "decisions": {"type": "array", "items": {"type": "string"}},
        "discoveries": {"type": "array", "items": {"type": "string"}},
        "failures": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,  # Allow extras for forward-compat
}


def validate_handoff(data: dict) -> dict:
    """Validate a handoff dict against the schema.

    Returns {valid: bool, errors: list[str]}. Does not raise.

    Uses jsonschema if available; falls back to manual validation
    for environments without it (harness is allowed to require it,
    but we're defensive).
    """
    errors: list[str] = []
    try:
        import jsonschema
        try:
            jsonschema.validate(instance=data, schema=HANDOFF_SCHEMA)
            return {"valid": True, "errors": []}
        except jsonschema.ValidationError as e:
            errors.append(f"{e.json_path}: {e.message}")
            return {"valid": False, "errors": errors}
    except ImportError:
        # Manual fallback
        for field in HANDOFF_SCHEMA["required"]:
            if field not in data:
                errors.append(f"missing required field: {field}")
        if "confidence" in data and data["confidence"] not in ("high", "low"):
            errors.append(f"confidence must be 'high' or 'low', got {data['confidence']!r}")
        if "status" in data and data["status"] not in ("complete", "partial", "blocked"):
            errors.append(f"status must be complete/partial/blocked, got {data['status']!r}")
        if "tests_passed" in data and not isinstance(data["tests_passed"], bool):
            errors.append("tests_passed must be boolean")
        if "build_ok" in data and not isinstance(data["build_ok"], bool):
            errors.append("build_ok must be boolean")
        return {"valid": len(errors) == 0, "errors": errors}


def read_handoff(worktree: Path) -> Optional[dict]:
    """Read and validate HARNESS_HANDOFF.json from the worktree root.

    Returns the parsed dict on success, None on any failure (missing file,
    malformed JSON, schema violation).
    """
    path = worktree / "HARNESS_HANDOFF.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    validation = validate_handoff(data)
    if not validation["valid"]:
        return None
    return data
```

- [ ] **Step 4: Add jsonschema to requirements**

In `src/requirements.txt`, add:

```
jsonschema>=4.0
```

And install:

```bash
cd /Users/prashantpandey/harness && pip install jsonschema
```

- [ ] **Step 5: Run and confirm passing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_handoff_schema.py -v
```

Expected: all 7 PASS.

- [ ] **Step 6: Update generator prompt to emit HARNESS_HANDOFF.json**

In `src/prompts/generator-v2.md`, find the `---HARNESS_STATUS---` and `---HANDOFF---` block instructions (STEP 5b and 5d). Replace BOTH blocks with:

```markdown
### 5c. Write HARNESS_HANDOFF.json (MANDATORY)

Write a single file `HARNESS_HANDOFF.json` in the worktree root. This
replaces the old `---HARNESS_STATUS---` and `---HANDOFF---` markdown blocks.

```json
{
  "feature_id": "<your task id>",
  "status": "complete",
  "files_modified": ["src/a.go", "src/b.go"],
  "tests_passed": true,
  "build_ok": true,
  "confidence": "high",
  "criterion_coverage": {
    "<task-id>-AC-001": {"impl": "src/a.go:10", "test": "src/a_test.go:5"},
    "<task-id>-AC-002": {"impl": "src/b.go:20", "test": "src/b_test.go:8"}
  },
  "decisions": ["Chose exponential backoff over linear"],
  "discoveries": ["Existing retry helper at src/retry/helper.go"],
  "failures": [],
  "notes": []
}
```

This supersedes `HARNESS_VERDICT.json` from Task 5 — it's the same data
plus the handoff/discovery fields. Do NOT write both.

Valid values:
- `status`: `"complete"`, `"partial"`, or `"blocked"`
- `confidence`: `"high"` or `"low"` (no other values accepted)
- `tests_passed`, `build_ok`: must be boolean

The orchestrator will validate this JSON against a strict schema. If the
file is missing, malformed, or violates the schema, the orchestrator will
reject your work with the specific validation error in `feedback.md`.
```

- [ ] **Step 7: Wire structured handoff into orchestrator**

In `src/core/orchestrator.py`, add:

```python
from .handoff_schema import read_handoff, validate_handoff
```

In `_apply_phase1_gates`, replace the `read_harness_verdict` call with `read_handoff`. Update the coverage extraction to use the structured handoff format:

Find in `_apply_phase1_gates`:

```python
        verdict = read_harness_verdict(Path(worktree))
```

Replace with:

```python
        verdict = read_handoff(Path(worktree)) or read_harness_verdict(Path(worktree))
```

This is a graceful transition — accept the new `HARNESS_HANDOFF.json` when present, fall back to the older `HARNESS_VERDICT.json` for runs in progress during the upgrade.

After the transition period (once all agents emit HANDOFF.json), delete the `read_harness_verdict` fallback.

Also update the HANDOFF block parsing in `discovery.py` to prefer the structured form:

In `src/core/discovery.py`, at the top of `parse_handoff()` (around line 14), add a check for the structured file first:

```python
def parse_handoff(output: str, worktree: Optional[Path] = None) -> dict:
    """Parse handoff data from generator output or structured JSON file.

    Prefers HARNESS_HANDOFF.json if worktree is provided and the file exists.
    Falls back to legacy ---HANDOFF--- block parsing.
    """
    if worktree is not None:
        handoff_path = Path(worktree) / "HARNESS_HANDOFF.json"
        if handoff_path.exists():
            try:
                import json as _json
                data = _json.loads(handoff_path.read_text())
                items = []
                items.extend(data.get("decisions", []))
                items.extend(data.get("discoveries", []))
                if data.get("failures"):
                    items.append(f"FAILURES: {'; '.join(data['failures'])}")
                return {"found": True, "items": items, "raw": _json.dumps(data, indent=2)}
            except (OSError, _json.JSONDecodeError):
                pass  # fall through to regex parse

    # Legacy markdown parsing
    pattern = r"---\s*HANDOFF\s*---\s*\n([\s\S]*?)(?:\n---|$)"
    match = re.search(pattern, output, re.IGNORECASE)
    # ... existing logic ...
```

Update the call site in `orchestrator.py` (around line 998) to pass `worktree`:

```python
        handoff_data = parse_handoff(gen_result.get("output", ""), worktree=wt_dir)
```

- [ ] **Step 8: Run the full suite**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/ -q
```

Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/core/handoff_schema.py src/core/orchestrator.py src/core/discovery.py src/prompts/generator-v2.md src/requirements.txt tests/test_handoff_schema.py
git commit -m "feat(fidelity): structured HARNESS_HANDOFF.json replaces markdown blocks

Generator now emits a single HARNESS_HANDOFF.json validated against a
strict schema (enum values for status/confidence, boolean types for
tests_passed/build_ok, etc). Orchestrator reads it via read_handoff()
with a temporary fallback to the legacy HARNESS_VERDICT.json path.

Discovery relay also prefers the structured file, falling back to the
legacy ---HANDOFF--- block for in-flight runs."
```

---

## Task 10: Tester agent + immutable test directory (Phase 6 — optional)

**Goal:** Defeat the reward-hacking failure mode at structural level. A dedicated "tester" agent writes acceptance tests from the AC list BEFORE the generator runs. The tests are placed in a read-only directory that the generator cannot modify. When the generator is done, the tests (which it never saw) must pass.

**Only build this if Phases 1–4 don't catch drift well enough on their own.** It adds a new agent role and meaningfully changes the execution pipeline.

**Files:**
- Create: `src/prompts/tester.md`
- Create: `src/core/test_vault.py` (read-only enforcement helper)
- Modify: `src/core/orchestrator.py` (spawn tester before generator)
- Modify: `src/prompts/generator-v2.md` (instructions about test vault)
- Modify: `src/config.yaml` (new `spec_fidelity.tester_agent.enabled` knob)
- Test: `tests/test_test_vault.py` (NEW)

- [ ] **Step 1: Write the tester prompt**

Create `src/prompts/tester.md`:

```markdown
## YOUR ROLE — TESTER AGENT

You write acceptance tests for a feature BEFORE the generator implements it.
Your tests will be placed in a read-only directory. The generator agent
cannot see or modify them. When the generator is done, your tests must pass.

## YOUR INPUT

The task's feature ID, description, and acceptance criteria with IDs are in
the user message.

## YOUR DELIVERABLES

Write tests to `./test_vault/<feature-id>/` — one test file per AC-ID or
grouped sensibly:

- `./test_vault/<feature-id>/test_<ac_id>.<ext>`
- Or `./test_vault/<feature-id>/test_<group>.<ext>` covering multiple related AC

## RULES

- Each AC-ID in the user message MUST be covered by at least one test assertion.
- Tests must be RUNNABLE by the project's configured test runner (check `package.json`, `go.mod`, `pyproject.toml`, etc.)
- Tests must be MEANINGFUL — no `assert True` or `assert 1 == 1`.
- Prefer behavioral tests ("call the endpoint, check the response") over structural tests ("the file contains this string").
- If the AC says "with retries and backoff", your test must actually verify the retry count and backoff delay — not just that the code compiles.
- Do NOT implement the feature. Do NOT write production code outside `test_vault/`.
- At the end, write `./test_vault/<feature-id>/coverage_map.json` mapping AC-IDs to test functions:

```json
{
  "task-001-AC-001": ["test_vault/task-001/test_register.go:TestUserCanRegister"],
  "task-001-AC-002": ["test_vault/task-001/test_register.go:TestEmailVerification", "test_vault/task-001/test_register.go:TestRateLimit"]
}
```

## WHAT YOU MUST NOT DO

- Do NOT write implementation code.
- Do NOT write tests that can be satisfied by a stub (return 200 for everything).
- Do NOT pass an AC with a superficial test — if the AC needs N behaviors verified, write N assertions.
```

- [ ] **Step 2: Write the test_vault module test**

Create `tests/test_test_vault.py`:

```python
"""Tests for test_vault enforcement."""

from pathlib import Path
import os
import pytest


def test_vault_freeze_makes_files_readonly(tmp_path):
    from src.core.test_vault import freeze_vault
    vault = tmp_path / "test_vault" / "feat-1"
    vault.mkdir(parents=True)
    (vault / "test_a.go").write_text("// test")
    (vault / "test_b.go").write_text("// test")

    freeze_vault(vault)

    # Files should not be writable by owner
    for f in (vault / "test_a.go", vault / "test_b.go"):
        mode = oct(f.stat().st_mode)[-3:]
        # 444 or 544 (readonly) expected, not 644 or 744
        assert mode[0] in ("4", "5")


def test_vault_thaw_restores_writability(tmp_path):
    from src.core.test_vault import freeze_vault, thaw_vault
    vault = tmp_path / "test_vault" / "feat-1"
    vault.mkdir(parents=True)
    (vault / "test_a.go").write_text("// test")

    freeze_vault(vault)
    thaw_vault(vault)

    (vault / "test_a.go").write_text("// modified")  # should not raise


def test_vault_verify_integrity_detects_modification(tmp_path):
    from src.core.test_vault import snapshot_vault, verify_vault_unchanged
    vault = tmp_path / "test_vault" / "feat-1"
    vault.mkdir(parents=True)
    (vault / "test_a.go").write_text("// original")

    snapshot = snapshot_vault(vault)
    result = verify_vault_unchanged(vault, snapshot)
    assert result["unchanged"] is True

    # Simulate tampering
    (vault / "test_a.go").chmod(0o644)
    (vault / "test_a.go").write_text("// tampered")

    result2 = verify_vault_unchanged(vault, snapshot)
    assert result2["unchanged"] is False
    assert len(result2["modified"]) == 1
```

- [ ] **Step 3: Run and confirm failing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_test_vault.py -v
```

- [ ] **Step 4: Implement `test_vault.py`**

Create `src/core/test_vault.py`:

```python
"""Test vault — read-only enforcement for tester-agent-written tests.

The tester agent writes acceptance tests to a vault directory. After the
vault is frozen, the generator agent cannot modify the tests (chmod 444
prevents writes; integrity verification catches tampering).

This is the structural defense against reward hacking: the agent cannot
rewrite a test file to make it pass because the orchestrator holds the
ground truth of what tests existed before the generator ran.
"""

import hashlib
import os
import stat
from pathlib import Path


def freeze_vault(vault_dir: Path) -> None:
    """Make every file in vault_dir read-only (owner-readable only)."""
    if not vault_dir.exists():
        return
    for root, dirs, files in os.walk(vault_dir):
        for name in files:
            p = Path(root) / name
            try:
                p.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            except OSError:
                pass


def thaw_vault(vault_dir: Path) -> None:
    """Restore write permissions so the orchestrator can clean up."""
    if not vault_dir.exists():
        return
    for root, dirs, files in os.walk(vault_dir):
        for name in files:
            p = Path(root) / name
            try:
                p.chmod(0o644)
            except OSError:
                pass


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def snapshot_vault(vault_dir: Path) -> dict:
    """Compute a content hash for every file in the vault.

    Returns {relative_path: sha256_hex} for later verification.
    """
    snapshot: dict = {}
    if not vault_dir.exists():
        return snapshot
    for root, dirs, files in os.walk(vault_dir):
        for name in files:
            p = Path(root) / name
            rel = str(p.relative_to(vault_dir))
            snapshot[rel] = _hash_file(p)
    return snapshot


def verify_vault_unchanged(vault_dir: Path, snapshot: dict) -> dict:
    """Verify the vault matches a prior snapshot.

    Returns:
        {
            unchanged: bool,
            modified: [relative paths that changed],
            deleted: [paths in snapshot but missing from vault],
            added: [paths in vault but not in snapshot],
        }
    """
    current = snapshot_vault(vault_dir)
    modified: list[str] = []
    deleted: list[str] = []
    added: list[str] = []
    for path, sha in snapshot.items():
        if path not in current:
            deleted.append(path)
        elif current[path] != sha:
            modified.append(path)
    for path in current:
        if path not in snapshot:
            added.append(path)
    return {
        "unchanged": len(modified) == 0 and len(deleted) == 0 and len(added) == 0,
        "modified": modified,
        "deleted": deleted,
        "added": added,
    }
```

- [ ] **Step 5: Run and confirm passing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_test_vault.py -v
```

- [ ] **Step 6: Wire tester agent into orchestrator**

This is a significant addition to `_pool_worker`. The new flow becomes:

```
1. Spawn tester agent → writes tests to ./test_vault/<feature-id>/
2. freeze_vault() + snapshot_vault() → capture ground truth
3. Spawn generator → implements feature (cannot touch test_vault/)
4. verify_vault_unchanged() → detect any tampering
5. independent_verifier runs tests from test_vault/ directly
6. Reviewer / drift scanner / etc. (as before)
```

In `src/core/orchestrator.py`, add imports:

```python
from .test_vault import freeze_vault, thaw_vault, snapshot_vault, verify_vault_unchanged
```

Load the tester prompt (around line 695):

```python
    tester_enabled = config.get("spec_fidelity", {}).get("tester_agent", {}).get("enabled", False)
    tester_system_prompt = None
    if tester_enabled and (prompts_dir / "tester.md").exists():
        tester_system_prompt = load_prompt(prompts_dir / "tester.md", wave_replacements)
```

In `_pool_worker`, BEFORE the generator runs, add:

```python
            vault_snapshot: dict = {}
            if tester_enabled and tester_system_prompt and not _existing:
                # Only run tester on FRESH worktrees — not on retries where
                # the vault already exists from the first pass.
                _tester_user_msg = _build_qa_user_msg(feature_id, work_plan)
                _tester_options = create_client_options(wt_dir, config)
                try:
                    await asyncio.wait_for(
                        run_agent_session(_tester_user_msg, _tester_options, wt_dir,
                                          system_prompt=tester_system_prompt),
                        timeout=600,  # 10 min for test writing
                    )
                except asyncio.TimeoutError:
                    print(f"  Tester {feature_id}: timed out")

                _vault_path = Path(wt_dir) / "test_vault" / feature_id
                if _vault_path.exists():
                    vault_snapshot = snapshot_vault(_vault_path)
                    freeze_vault(_vault_path)
                    print(f"  {feature_id}: test vault frozen ({len(vault_snapshot)} files)")
```

After the generator runs, BEFORE the reviewer, add integrity verification:

```python
            if tester_enabled and vault_snapshot:
                _vault_path = Path(wt_dir) / "test_vault" / feature_id
                integrity = verify_vault_unchanged(_vault_path, vault_snapshot)
                if not integrity["unchanged"]:
                    drift_counter.record("test_vault_tampered",
                                         feature_id=feature_id,
                                         details=integrity)
                    _tamper_report = (
                        "## Test vault integrity violated\n\n"
                        "The generator agent modified files in the read-only test vault.\n"
                        "This is a reward-hacking attempt. The task is rejected.\n\n"
                        f"Modified: {integrity['modified']}\n"
                        f"Deleted: {integrity['deleted']}\n"
                        f"Added: {integrity['added']}\n\n"
                        "The vault must remain unchanged. Implement the feature against "
                        "the tests as written — do NOT modify the tests themselves."
                    )
                    (Path(wt_dir) / "feedback.md").write_text(_tamper_report)
                    # Synthesize FAIL
                    qa_result = {
                        "output": f"VERDICT: FAIL\n{_tamper_report}",
                        "cost": 0, "usage": {},
                        "overturned_by_vault_integrity": True,
                    }
                    # Skip the reviewer
                    # ... fall through to failure handling
```

- [ ] **Step 7: Update generator prompt about the vault**

In `src/prompts/generator-v2.md`, add a new section after STEP 0b:

```markdown
## STEP 0c: CHECK FOR TEST VAULT

If `./test_vault/<your-feature-id>/` exists, a tester agent has already
written acceptance tests for your task. **You MUST make those tests pass
without modifying them.**

- The test_vault directory is read-only. Any attempt to modify it will
  be caught by the orchestrator and your work will be rejected.
- Read the test files to understand what behavior is expected.
- Implement the feature so the tests pass.
- Do NOT implement tests of your own that duplicate the vault's tests.

If test_vault does NOT exist, proceed with your normal TDD workflow
(write your own tests first, then implement).
```

- [ ] **Step 8: Add config knob**

In `src/config.yaml`, extend `spec_fidelity`:

```yaml
spec_fidelity:
  # ... existing knobs ...
  tester_agent:
    enabled: false  # Opt-in — big architectural change, measure Phase 1-4 first
    timeout_seconds: 600
```

- [ ] **Step 9: Run the full suite**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/ -q
```

Expected: all PASS.

- [ ] **Step 10: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/prompts/tester.md src/prompts/generator-v2.md src/core/test_vault.py src/core/orchestrator.py src/config.yaml tests/test_test_vault.py
git commit -m "feat(fidelity): tester agent + read-only test vault (optional)

Adds a new agent role that writes acceptance tests BEFORE the generator
runs. Tests are placed in ./test_vault/<feature-id>/ and frozen read-only.
Generator cannot modify them — the orchestrator verifies integrity after
generation via sha256 snapshots. Defeats reward hacking at structural
level. Opt-in via spec_fidelity.tester_agent.enabled (default: false)."
```

---

## Task 11: Multi-model candidate generation (Phase 7 — optional)

**Goal:** Generate N candidate implementations in parallel, rank by AC coverage completeness, pick the best. Catches single-model shortcutting. Expensive — only enable for critical features. Each candidate multiplies generator cost by N.

**Only build this after measuring Phase 1–6 effectiveness.** For subscription users this is likely too expensive except on high-value features.

**Files:**
- Modify: `src/core/orchestrator.py` (N-candidate generation + ranking)
- Modify: `src/config.yaml` (new `spec_fidelity.multi_model.candidates` knob)
- Test: covered by integration run, not unit tests

- [ ] **Step 1: Add candidate generation to `_pool_worker`**

In `src/core/orchestrator.py`, inside `_pool_worker`, wrap the generator spawn in a candidate loop. High-level sketch:

```python
            num_candidates = config.get("spec_fidelity", {}).get("multi_model", {}).get("candidates", 1)
            # Only use multi-candidate if:
            # 1. Config enables it AND
            # 2. This is a fresh attempt (not a retry) AND
            # 3. The task has structured AC for coverage ranking
            use_multi = (num_candidates > 1 and not _existing and
                         any(isinstance(a, dict) and "id" in a
                             for a in feature.get("acceptance_criteria", [])))

            if use_multi:
                # Spawn N generators in parallel worktrees, rank results
                candidates = await _generate_candidates(
                    num_candidates, feature, wt_dir, branch, wid,
                    gen_options, gen_system_prompt, gen_timeout, config, state_dir,
                )
                # Rank by (a) coverage completeness, (b) build+test pass, (c) fewest fix markers
                best = _rank_candidates(candidates, feature)
                gen_result = best["gen_result"]
                # Merge the winning candidate's changes into the main worktree
                _merge_candidate_into(best, wt_dir, branch)
            else:
                # Existing single-candidate path (unchanged)
                gen_result = await asyncio.wait_for(
                    run_agent_session(user_msg, gen_options, wt_dir, system_prompt=gen_system_prompt),
                    timeout=gen_timeout,
                )
```

This is a sketch because the full implementation requires additional helper functions (`_generate_candidates`, `_rank_candidates`, `_merge_candidate_into`) that are too large to fully spec without knowing how you want to handle the parallel worktree directories.

**Recommended approach if you build this:** start by implementing it as a separate Python file `src/core/multi_candidate.py` with pure functions that take a list of candidate results and return the ranked winner. Wire it into `_pool_worker` as a single call site. Don't try to fit it inline.

- [ ] **Step 2: Config knob**

In `src/config.yaml`, extend `spec_fidelity`:

```yaml
spec_fidelity:
  # ... existing knobs ...
  multi_model:
    candidates: 1  # Set to 3 for ensemble, 1 to disable
    ranking: "coverage_then_tests"  # or "tests_only", "coverage_only"
```

- [ ] **Step 3: Commit (if built)**

```bash
cd /Users/prashantpandey/harness
git add src/core/multi_candidate.py src/core/orchestrator.py src/config.yaml
git commit -m "feat(fidelity): multi-model candidate generation + coverage ranking"
```

---

## Task 12: Living spec — write decisions back (Phase 7 — optional)

**Goal:** When a feature makes an implementation decision (e.g., "exponential backoff, base=100ms, max 5 retries"), write that decision back to `spec.md` so subsequent features inherit it rather than re-interpreting from scratch.

**Files:**
- Create: `src/core/living_spec.py`
- Modify: `src/core/orchestrator.py` (call `append_decisions_to_spec` after successful merge)
- Test: `tests/test_living_spec.py` (NEW)

- [ ] **Step 1: Write the failing test**

Create `tests/test_living_spec.py`:

```python
"""Tests for living_spec — append decisions to spec.md."""

from pathlib import Path


def test_append_decisions_to_fresh_spec(tmp_path):
    from src.core.living_spec import append_decisions_to_spec
    spec = tmp_path / "spec.md"
    spec.write_text("# Overview\nBuild a thing.\n")

    append_decisions_to_spec(
        spec_path=spec,
        feature_id="task-001",
        decisions=["Chose exponential backoff", "Retry max 5 attempts"],
    )

    content = spec.read_text()
    assert "## Implementation Decisions" in content
    assert "task-001" in content
    assert "exponential backoff" in content
    assert "Retry max 5 attempts" in content


def test_append_decisions_preserves_existing_decisions(tmp_path):
    from src.core.living_spec import append_decisions_to_spec
    spec = tmp_path / "spec.md"
    spec.write_text("# Overview\n\n## Implementation Decisions\n\n### task-001\n- Used JWT\n")

    append_decisions_to_spec(
        spec_path=spec,
        feature_id="task-002",
        decisions=["Used Redis for session store"],
    )

    content = spec.read_text()
    assert "task-001" in content  # preserved
    assert "Used JWT" in content
    assert "task-002" in content  # appended
    assert "Redis" in content


def test_append_decisions_idempotent(tmp_path):
    """Appending the same decisions twice doesn't duplicate."""
    from src.core.living_spec import append_decisions_to_spec
    spec = tmp_path / "spec.md"
    spec.write_text("# Overview\n")

    append_decisions_to_spec(
        spec_path=spec, feature_id="task-001",
        decisions=["Used JWT"],
    )
    append_decisions_to_spec(
        spec_path=spec, feature_id="task-001",
        decisions=["Used JWT"],
    )

    content = spec.read_text()
    assert content.count("Used JWT") == 1
```

- [ ] **Step 2: Implement `living_spec.py`**

Create `src/core/living_spec.py`:

```python
"""Living spec — write implementation decisions back to spec.md.

When a feature completes, the orchestrator appends the generator's
reported decisions to a "Implementation Decisions" section in spec.md.
Subsequent features can then inherit those decisions rather than
re-interpreting ambiguous criteria from scratch.

Idempotent — appending the same decision twice is a no-op.
"""

from pathlib import Path


_DECISIONS_HEADING = "## Implementation Decisions"


def append_decisions_to_spec(
    spec_path: Path,
    feature_id: str,
    decisions: list[str],
) -> None:
    """Append a decisions block for feature_id to spec.md.

    If spec.md already has an Implementation Decisions section, we
    add or update the subsection for this feature. If the decisions
    list is empty, no-op.
    """
    if not decisions:
        return
    if not spec_path.exists():
        return

    content = spec_path.read_text(encoding="utf-8")

    # Ensure decisions section exists
    if _DECISIONS_HEADING not in content:
        content = content.rstrip() + f"\n\n{_DECISIONS_HEADING}\n\n"

    # Build this feature's block
    existing_section = f"### {feature_id}"
    if existing_section in content:
        # Find the subsection and append idempotently
        lines = content.split("\n")
        in_feature_section = False
        out_lines: list[str] = []
        existing_decisions: set = set()
        for line in lines:
            if line.startswith(f"### {feature_id}"):
                in_feature_section = True
                out_lines.append(line)
                continue
            if in_feature_section and line.startswith("### "):
                # Hit the next feature — emit any missing decisions first
                for d in decisions:
                    if d not in existing_decisions:
                        out_lines.append(f"- {d}")
                in_feature_section = False
            if in_feature_section and line.startswith("- "):
                existing_decisions.add(line[2:])
            out_lines.append(line)
        if in_feature_section:
            # Section was at EOF
            for d in decisions:
                if d not in existing_decisions:
                    out_lines.append(f"- {d}")
        content = "\n".join(out_lines)
    else:
        # Append new subsection
        block_lines = [f"### {feature_id}"]
        for d in decisions:
            block_lines.append(f"- {d}")
        block_lines.append("")
        content = content.rstrip() + "\n\n" + "\n".join(block_lines) + "\n"

    spec_path.write_text(content, encoding="utf-8")
```

- [ ] **Step 3: Run and confirm passing**

```bash
cd /Users/prashantpandey/harness && python -m pytest tests/test_living_spec.py -v
```

Expected: all 3 PASS.

- [ ] **Step 4: Wire into orchestrator after successful merge**

In `src/core/orchestrator.py`, after the successful merge path (around line 1114 where `completed.append(feature_id)`), add:

```python
        # Living spec update — append decisions from handoff
        if config.get("spec_fidelity", {}).get("living_spec", {}).get("enabled", False):
            try:
                from .living_spec import append_decisions_to_spec
                _spec_path = state_dir / "spec.md"
                _decisions = (handoff_data.get("items") if handoff_data else []) or []
                # Filter to short, decision-like lines
                _decisions = [d for d in _decisions if len(d) < 200 and ":" in d]
                if _decisions and _spec_path.exists():
                    append_decisions_to_spec(_spec_path, feature_id, _decisions[:5])
            except Exception as e:
                _log_error(state_dir, f"living_spec:{feature_id}", e)
```

- [ ] **Step 5: Config knob**

In `src/config.yaml`, extend `spec_fidelity`:

```yaml
spec_fidelity:
  # ... existing knobs ...
  living_spec:
    enabled: false  # Opt-in
```

- [ ] **Step 6: Commit**

```bash
cd /Users/prashantpandey/harness
git add src/core/living_spec.py src/core/orchestrator.py src/config.yaml tests/test_living_spec.py
git commit -m "feat(fidelity): living spec — append decisions to spec.md after merge

Feature handoff decisions are appended to an Implementation Decisions
section in spec.md. Subsequent features see prior decisions rather than
re-interpreting from scratch. Idempotent — duplicate decisions are no-op.
Opt-in via spec_fidelity.living_spec.enabled."
```

---

## Summary of expected drift reduction

| Phase | Ships | Catches |
|---|---|---|
| Phase 0 (Task 0) | Drift counter | Nothing — measurement only, but enables A/B for later phases |
| Phase 1 (Tasks 1–3) | Drift scanner + independent verifier | TODO/FIXME/unimplemented markers, reward-hacked tests, reviewer leniency. Estimated 40-60% of drift caught here with zero LLM cost. |
| Phase 2 (Tasks 4–6) | AC-IDs + HARNESS_VERDICT.json coverage gate | "Agent implemented 2 of 5 AC" drift. Forces structural accountability for every criterion. Estimated 20-30% more drift caught. |
| Phase 3 (Task 7) | Fresh reviewer on retry | Anchoring bias — reviewer that missed a gap once tends to miss it again. Estimated 5-10% more. |
| Phase 4 (Task 8) | Per-feature adversary on diff | Catches drift one LLM call earlier than the reviewer. Estimated 5-10% more. |
| Phase 5 (Task 9) | Structured JSON handoff | Eliminates regex-parsing silent failures. Quality-of-life but hard-to-quantify drift prevention. |
| Phase 6 (Task 10) | Tester agent + read-only vault | Defeats reward hacking at structural level. Estimated residual 5-10% of drift after Phases 1-5. |
| Phase 7 (Tasks 11–12) | Multi-model + living spec | Marginal improvements, high cost. |

**Realistic target:** Phases 0–4 together should catch 80-90% of the simplification drift that currently leaks through. Phase 6 adds structural defense for the remaining ~10%. Phase 7 is optional polish.

**Recommended first shipment (1-2 days of work):** Tasks 0, 1, 2, 3, 4, 5. That's Phase 0 through the end of Phase 2. It's the highest-leverage bundle that closes the biggest drift holes without introducing new LLM calls at runtime. After it ships and you've measured drift counts on a real run, decide whether Phase 3+ are worth building.

---

## Self-review notes

**Spec coverage:** All 10 drift-prevention patterns from the audit are covered by tasks. Task 0 adds measurement. Tasks 1–3 close the biggest holes mechanically. Tasks 4–6 add AC-ID enforcement. Tasks 7–8 add role rotation and per-feature adversary. Task 9 hardens the handoff layer. Task 10 is the big structural change (reward-hacking defense). Tasks 11–12 are optional polish.

**Placeholder scan:** Tasks 0 through 9 have exact file paths, concrete code, and complete test cases. Task 10 (tester agent) is spec'd in detail for the orchestrator wiring but the prompt work is lighter. Task 11 (multi-model) intentionally leaves the parallel candidate implementation as a sketch because it's optional and the full details depend on pattern decisions (worktree layout, merge strategy) that deserve their own mini-brainstorm.

**Type consistency:** Function names are stable across tasks: `DriftCounter`, `scan_for_drift`, `run_independent_verification`, `overturn_if_failed`, `assign_ac_ids`, `validate_coverage_map`, `read_harness_verdict`, `read_handoff`, `validate_handoff`, `freeze_vault`, `snapshot_vault`, `verify_vault_unchanged`, `append_decisions_to_spec`.

**Scope check:** Single codebase (`~/harness/src`). No new external services. Phases can each be implemented, tested, and shipped independently with measurable impact.

**Risk notes:**

- **Task 4 (AC-ID)** requires changes in `work_plan.py`'s loading path. Existing work plans without `id` fields will auto-upgrade via `assign_ac_ids`, but existing state files with the old AC format will need re-loading. Mitigate by running Task 4 against a fresh run rather than resuming an in-progress one.

- **Task 5 (HARNESS_VERDICT gate)** rejects completion if the generator doesn't emit the file. On runs that started before this lands, the generator won't know to emit it — so either (a) run Task 5 against fresh runs only, or (b) make the gate opt-in via config for the first few runs while agents get used to the new requirement.

- **Task 6 (planner prompts)** changes the expected output format. Runs already past the planning phase won't be affected, but if you resume a partially-planned run, the refiner may not recognize its own previous output.

- **Task 9 (JSON handoff)** introduces `jsonschema` as a Python dependency. Make sure the harness's install path picks it up. The `try: import jsonschema` fallback in `validate_handoff` handles the transition gracefully.

- **Task 10 (tester agent)** is the biggest architectural addition. Adding a new agent role means every feature now has 3 LLM calls (tester, generator, reviewer) + possibly adversary-diff = up to 4. This is expensive. Do not enable this by default — measure Phase 1–5 effectiveness first.

**What this plan does NOT solve:**

- Planning-phase drift (architect producing weak AC in the first place). The existing adversary/refiner/validator pipeline already addresses this — we're not touching it.
- Cross-feature semantic drift (feature N and feature N+2 collectively implement something weaker than the spec intended). Task 12 (living spec) is a partial mitigation; full solution requires something like a continuous spec checker that audits the whole codebase against the spec after every Nth merge.
- Generator hallucinations of APIs/libraries that don't exist. The drift scanner won't catch this — a compile/build check will (already done by `independent_verifier`).
- Agent refusing to implement part of a feature because it "doesn't feel safe" or "violates best practices". This is a prompt engineering problem, outside scope.

**Relationship to token-efficiency plan:** After this plan lands, the token-efficiency plan (`docs/plans/2026-04-07-token-efficiency.md`) becomes safer to ship. Specifically:
- Warm session reuse is safer because the fidelity gates catch cross-feature contamination
- Deterministic gate can be stacked with the fidelity gates — they're complementary
- Cache stability work is unaffected by fidelity changes
- Local LLM offload still makes sense for grunt work

Ship this plan FIRST, measure the drift reduction, then ship the token-efficiency plan.

---

