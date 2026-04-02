# Harness v2.1 Self-Upgrade — Reliability & Observability

Upgrade the existing AI development harness Python codebase with 6 modules that close the gaps discovered in the v2 self-build. This is a **brownfield build** — the codebase exists at `src/core/` with ~2,900 lines across 12 modules, all with tests. You are modifying and extending existing code, not building from scratch.

## Reference Documents
- Spec: harness-v2-orchestration.md (in the specs/ directory — read the full spec including Scale Architecture, FM8-FM10, Worktree Lifecycle, and behavioral acceptance criteria sections)
- Existing code: src/core/ (read ALL modules before planning — orchestrator.py, state.py, circuit_breaker.py, cost_tracker.py, control_plane.py, knowledge_client.py, client.py, progress.py, completion.py, security.py, parallel.py)
- Existing tests: tests/ (read test patterns before writing new tests)
- Existing prompts: src/prompts/ (planner.md, generator.md, evaluator.md)
- Config: src/config.yaml

## Tech Stack
- Python 3.11+
- pytest (test runner — match existing test patterns in tests/)
- PyYAML (config loading)
- sqlite3 (stdlib — for local knowledge fallback)
- subprocess (for git worktree management and Playwright checks)
- No new dependencies beyond stdlib + existing (yaml, pytest)

## Existing Architecture

```
src/
  core/
    orchestrator.py      ← 3-agent loop: Planner → Generator ↔ Evaluator
    state.py             ← Atomic JSON state (tempfile + fsync + replace)
    circuit_breaker.py   ← Nygard state machine (CLOSED/HALF_OPEN/OPEN)
    cost_tracker.py      ← Per-agent cost accumulation (planner/generator/evaluator)
    progress.py          ← Multi-source progress detection (git + status + trends)
    completion.py        ← Shared completion logic (feature list + test suite + rolling window)
    client.py            ← Claude SDK client factory (Playwright MCP wiring)
    control_plane.py     ← HTTP client for v2 control plane (fire-and-forget events)
    knowledge_client.py  ← Format knowledge chunks for planner injection
    security.py          ← Bash command allowlist + PreToolUse hook
    parallel.py          ← Parallel agent teams via worktrees (Phase 2, disabled)
  prompts/
    planner.md           ← 3-pass review: technical gaps, AI failure modes, conventions
    generator.md         ← Recovery from feedback, bearings check, feature picking
    evaluator.md         ← Strict QA with behavioral verification + dashboard theatre detection
  config.yaml            ← All defaults (models, limits, circuit breaker, security)
tests/
  test_*.py              ← One test file per module, pytest fixtures, unittest.mock
```

## What Already Works (DO NOT rebuild these)
- 3-agent orchestration loop with circuit breaker
- Atomic state management with crash recovery
- Planner knowledge injection (queries control plane KB before planning)
- Planner validation gate (requires feature_list.json + SPEC_GAPS.md)
- Browser pre-flight (installs @playwright/mcp if needed)
- Control plane HTTP client (create_run, post_event, ingest_retro, query_knowledge, get_all_chunks)
- Cost tracking per agent type (planner/generator/evaluator)
- Feature list management (get_next_feature, mark_passing, mark_blocked, increment_retries)
- Security (bash allowlist, command substitution blocking)

## What to Build (6 Features)

### Feature 1: Worktree Lifecycle Module

Create `src/core/worktree.py` with:
- `list_worktrees(repo_dir) -> list[dict]` — parse `git worktree list --porcelain`, return [{path, commit, branch}]
- `is_ancestor_of(repo_dir, commit, target="HEAD") -> bool` — `git merge-base --is-ancestor`
- `sweep_merged_worktrees(repo_dir, dry_run=False) -> list[str]` — remove all worktrees whose HEAD is at or behind master, delete their branches, run `git worktree prune`. Never remove the main worktree. Return list of removed paths.
- `should_use_worktree(task_description) -> bool` — keyword-based heuristic: read-only tasks (explore, validate, research, search, audit) return False; write tasks (implement, build, fix, add) return True; default True.
- `find_worktree_by_topic(repo_dir, topic) -> Optional[dict]` — scan existing worktrees, grep branch names and recent commit messages for topic keywords. Return matching worktree or None.

**Wire into orchestrator.py:**
- Call `sweep_merged_worktrees(project_dir)` at start of `run_harness()` (after banner, before Phase 1)
- Call `sweep_merged_worktrees(project_dir)` at end of `run_harness()` (before return)
- Print count of removed worktrees

**Acceptance criteria:**
- `sweep_merged_worktrees` on a repo with 3 worktrees (2 at master HEAD, 1 ahead) removes exactly the 2 merged ones and returns their paths
- `should_use_worktree("explore the codebase")` returns False
- `should_use_worktree("implement auth module")` returns True
- `find_worktree_by_topic(repo, "auth")` returns a worktree whose branch contains "auth" if one exists, None otherwise
- Main worktree is NEVER removed even if its commit matches HEAD
- `git worktree prune` runs after every sweep
- Tests use real temporary git repos (subprocess git init), not mocks

### Feature 2: Per-Feature Cost Tracking

Extend `src/core/cost_tracker.py`:
- Add `record_feature(feature_id, agent_type, cost_usd)` — tracks cost per feature_id
- Add `feature_costs -> dict[str, float]` property — returns {feature_id: total_cost}
- Add `most_expensive_features(n=5) -> list[tuple[str, float]]` — sorted descending
- Include per-feature breakdown in `to_dict()` and `from_dict()`
- Include per-feature summary in `format_summary()`

**Wire into orchestrator.py:**
- In the generator loop, after `cost_tracker.record("generator", ...)`, also call `cost_tracker.record_feature(feature_id, "generator", result["cost"])`
- Same for evaluator cost
- Print top 5 most expensive features in final summary

**Acceptance criteria:**
- `record_feature("001", "generator", 1.50)` twice + `record_feature("001", "evaluator", 0.50)` → `feature_costs["001"]` == 2.50 (note: the generator calls may not always be exactly for this feature, so track by what feature is current)
- `most_expensive_features(3)` returns sorted list of (id, cost) tuples
- `to_dict()` includes `"feature_costs": {"001": 2.50, ...}`
- `from_dict(data)` restores feature costs on resume
- `format_summary()` shows per-feature costs when > 0

### Feature 3: Suspicion Heuristic

Add `check_suspicion(state_mgr, config) -> dict` to `src/core/completion.py`:
- After all features pass, check for suspiciously clean results:
  - 100% pass rate with zero retries AND zero circuit breaker opens
  - Total evaluator sessions < total features (evaluator didn't run for every feature)
  - Features completed faster than 30s average (likely superficial)
- Return `{suspicious: bool, reasons: list[str]}`
- If suspicious, orchestrator prints a warning but does NOT block (informational only for v2.1)

**Wire into orchestrator.py:**
- Call after the completion check in the generator loop finds all features done
- Print suspicion report in final summary if suspicious

**Acceptance criteria:**
- 10 features, all pass on first attempt, 0 CB opens → suspicious=True, reasons includes "100% first-attempt pass rate"
- 10 features, 2 retries, 1 CB open → suspicious=False
- Evaluator ran 5 times for 10 features → suspicious=True, reasons includes "evaluator sessions < features"
- Function reads from state_mgr (not global state) and config (thresholds configurable)

### Feature 4: Feature Descriptions in Control Plane Events

Currently `orchestrator.py` posts `feature_id` to control plane events but not `feature_desc`. The dashboard's run-detail page needs descriptions.

**Modify orchestrator.py:**
- When posting `feature_pass` or `feature_fail` events, include `feature_desc=next_feature.get("description", "")`
- When posting `feature_start` (if added), include description

**Modify control_plane.py:**
- `post_event` already accepts `**kwargs` — no change needed to the client

**Acceptance criteria:**
- `cp.post_event("feature_pass", feature_id="001", feature_desc="Create project skeleton")` — the event body sent to the API contains both `feature_id` and `feature_desc`
- `cp.post_event("feature_fail", feature_id="002", feature_desc="Implement auth", error="test failed")` — body contains all three fields
- Existing tests still pass (post_event signature unchanged)

### Feature 5: Evaluator Browser Use Verification

After the evaluator runs on a UI feature, check if the evaluator's output mentions using browser tools. If the feature description mentions "UI", "dashboard", "page", "click", "render", "display", or "navigate" but the evaluator output contains no Playwright tool use indicators, log a warning.

**Add `check_evaluator_used_browser(feature_desc, evaluator_output) -> dict` to orchestrator.py:**
- Determine if feature is UI-related (keyword match on description)
- If UI-related, check evaluator output for browser tool indicators: "browser_navigate", "browser_snapshot", "browser_click", "screenshot", "Playwright"
- Return `{ui_feature: bool, browser_used: bool, warning: str|None}`

**Wire into orchestrator.py:**
- Call after evaluator PASS verdict on each feature
- Print warning if UI feature passed without browser verification

**Acceptance criteria:**
- Feature desc "Add dashboard overview page" + evaluator output contains "browser_snapshot" → `{ui_feature: True, browser_used: True, warning: None}`
- Feature desc "Add dashboard overview page" + evaluator output has no browser mentions → `{ui_feature: True, browser_used: False, warning: "UI feature passed without browser verification"}`
- Feature desc "Create database schema" + any output → `{ui_feature: False, browser_used: False, warning: None}`
- Warning is printed but does NOT fail the feature (informational for v2.1)

### Feature 6: Config Section Updates + Integration Test

**Update src/config.yaml:**
Add these sections:
```yaml
# Knowledge (past learnings injection)
knowledge:
  inject_into_planner: true
  max_chunks: 20
  chunk_types:
    - failure_mode
    - pattern
    - convention

# Worktree lifecycle
worktree:
  sweep_on_start: true
  sweep_on_end: true
  read_only_guard: true

# Suspicion heuristic
suspicion:
  enabled: true
  min_retry_rate: 0.05
  min_avg_feature_seconds: 30
```

**Create `tests/test_integration.py`:**
- Integration test that exercises the full harness state lifecycle:
  1. Create a temp state_dir
  2. Initialize StateManager
  3. Create a CostTracker, record some costs including per-feature
  4. Create feature_list.json with 3 features, mark 2 passing
  5. Check completion (should be incomplete)
  6. Mark remaining feature passing
  7. Check completion (should be complete)
  8. Run suspicion check (should detect clean run)
  9. Validate planner output (should fail — no SPEC_GAPS.md)
  10. Create SPEC_GAPS.md, re-validate (should pass)
  11. Format cost summary, assert per-feature costs shown
  12. Assert all state files are valid JSON after the run

**Acceptance criteria:**
- The integration test passes with `pytest tests/test_integration.py -v`
- It exercises StateManager, CostTracker, completion detection, suspicion heuristic, and planner validation
- No mocks — real file I/O with temp directories
- All 192+ existing tests still pass

## Critical Constraints — Read Before Writing Any Code

1. **Brownfield rules** — read every file you modify before changing it. Match existing code style. Do not restructure existing modules.
2. **No new dependencies** — everything uses stdlib + existing deps (yaml, pytest). No pip installs.
3. **Test-first** — write failing test, then implement, then verify pass. Match existing test patterns (pytest fixtures, unittest.mock for HTTP, real temp dirs for state).
4. **Atomic state** — any new state files must use the `atomic_write`/`atomic_read` pattern from state.py.
5. **Git operations are subprocess-based** — use `subprocess.run(["git", ...])` not gitpython. All git calls must have `timeout=` and `capture_output=True`.
6. **Control plane is optional** — all new features must work when `HARNESS_CONTROL_PLANE_URL` is not set. Never crash if the control plane is down.
7. **Config is optional** — all new config keys must have sensible defaults. If config section is missing, feature still works.
8. **No print changes in test mode** — new print statements in orchestrator are fine but must not break any test that captures stdout.

## Known Failure Modes (from v2 self-build — THE PLANNER MUST GUARD AGAINST ALL OF THESE)

- **FM-08: Dashboard theatre** — evaluator checks code compiles and endpoints return 200, never checks if UI actually renders data or has click handlers. Guard: behavioral verification rules in evaluator prompt + browser use verification (Feature 5)
- **FM-09: Browse-requires-search** — list pages show empty until user searches. Guard: every browse page must call its list endpoint on mount
- **FM-10: Worktree accumulation** — 46 orphaned worktrees after 2 runs. Guard: worktree lifecycle module (Feature 1)
- **FM-11: Planner skip** — planner skips 3-pass review if not validated. Guard: validation gate already patched, but features must have behavioral acceptance criteria not just structural
- **FM-12: Cost blindness** — can't tell which features are expensive. Guard: per-feature cost tracking (Feature 2)
- **FM-13: Silent evaluator leniency** — evaluator passes UI features without browser checks. Guard: browser use verification (Feature 5)
- **FM-14: Clean run false confidence** — 100% pass rate with 0 retries signals "done" but may mean evaluator is too lenient. Guard: suspicion heuristic (Feature 3)

## What "Done" Looks Like

- `python3 -m pytest tests/ -v` → all tests pass (existing 192 + new tests for features 1-6)
- `worktree.py` can sweep a repo with mixed merged/unmerged worktrees correctly
- `cost_tracker.format_summary()` shows per-feature costs
- `check_suspicion()` flags clean runs for extra scrutiny
- Control plane events include `feature_desc` alongside `feature_id`
- `check_evaluator_used_browser()` detects when UI features pass without browser verification
- Integration test exercises the full state lifecycle without mocks
- All existing 192 tests still pass — zero regressions
