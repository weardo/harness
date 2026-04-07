# Planner State Machine — Proper Run Tracking

**Status:** In Progress
**Date:** 2026-04-02

## Problem

Current state tracking is primitive:
- `planner_roles: {architect: true, adversary: true}` — boolean only, no timestamps, no costs, no attempt counts
- No record of validator rejections or refiner-fix loops
- Resume logic guesses from file existence (`validation.json` exists? `work_plan.json` exists?)
- No way to see how long each role took, how much it cost, or how many retries happened
- Control plane gets zero planner-level telemetry

## Solution

Replace the flat `planner_roles` dict with a structured state machine in `state.json`:

```json
{
  "planner": {
    "status": "in_progress",
    "started_at": "2026-04-02T15:56:00Z",
    "completed_at": null,
    "total_cost_usd": 3.24,
    "fix_loops_completed": 0,
    "max_fix_loops": 2,
    "roles": [
      {
        "name": "architect",
        "status": "complete",
        "started_at": "2026-04-02T15:56:00Z",
        "completed_at": "2026-04-02T16:12:00Z",
        "duration_s": 960,
        "cost_usd": 1.23,
        "model": "claude-opus-4-6",
        "artifact": "draft_work_plan.json",
        "artifact_size_bytes": 205662,
        "attempt": 1,
        "validation": {"valid": true}
      },
      {
        "name": "adversary",
        "status": "complete",
        "started_at": "2026-04-02T16:12:05Z",
        "completed_at": "2026-04-02T16:25:00Z",
        "duration_s": 775,
        "cost_usd": 0.89,
        "model": "claude-opus-4-6",
        "artifact": "spec_gaps.json",
        "artifact_size_bytes": 21424,
        "attempt": 1,
        "validation": {"valid": true, "gaps_found": 18}
      },
      {
        "name": "refiner",
        "status": "complete",
        "started_at": "2026-04-02T16:25:05Z",
        "completed_at": "2026-04-02T16:47:00Z",
        "duration_s": 1315,
        "cost_usd": 0.45,
        "model": "claude-sonnet-4-6",
        "artifact": "work_plan.json",
        "artifact_size_bytes": 210000,
        "attempt": 1,
        "validation": {"valid": true}
      },
      {
        "name": "validator",
        "status": "rejected",
        "started_at": "2026-04-02T16:47:05Z",
        "completed_at": "2026-04-02T17:01:00Z",
        "duration_s": 835,
        "cost_usd": 0.67,
        "model": "claude-opus-4-6",
        "artifact": "validation.json",
        "attempt": 1,
        "validation": {"valid": false, "sign_off": false, "issues_count": 4}
      },
      {
        "name": "refiner-fix",
        "status": "complete",
        "started_at": "2026-04-02T17:01:05Z",
        "completed_at": "2026-04-02T17:05:00Z",
        "duration_s": 235,
        "cost_usd": 0.12,
        "model": "claude-sonnet-4-6",
        "artifact": "work_plan.json",
        "attempt": 1,
        "triggered_by": "validator:attempt:1",
        "issues_fixed": 4
      },
      {
        "name": "validator",
        "status": "complete",
        "started_at": "2026-04-02T17:05:05Z",
        "completed_at": "2026-04-02T17:12:00Z",
        "duration_s": 415,
        "cost_usd": 0.55,
        "model": "claude-opus-4-6",
        "artifact": "validation.json",
        "attempt": 2,
        "validation": {"valid": true, "sign_off": true}
      }
    ]
  }
}
```

## State Transitions

```
Planner status: pending → in_progress → complete | failed

Role status: pending → running → complete | rejected | failed
  rejected → triggers refiner-fix → re-run validator

Fix loop: validator rejected → refiner-fix → validator (max 2 loops)
  After max loops: proceed anyway with warning
```

## Resume Logic

No more guessing from files. Resume reads `planner.roles[]` and:
1. Finds last role with `status != "complete"` and `status != "rejected"`
2. If last role is `rejected` (validator) and fix_loops < max → run refiner-fix
3. If role is `running` (crashed mid-run) → re-run that role
4. If all roles complete → skip planner

## What Changes

| File | Change |
|---|---|
| `src/core/state.py` | Add `PlannerState` helpers: `get_planner_state()`, `update_role_state()`, `record_role_complete()` |
| `src/core/planner_pipeline.py` | Replace `planner_roles_done` dict with structured role tracking. Record timestamps, costs, attempts. Fix loop logic driven by state, not file existence. |
| `src/core/orchestrator.py` | Resume reads `planner.status` instead of `planner_complete` boolean. Backward compat: if old `planner_roles` dict exists, migrate to new format. |
| `src/core/event_tracker.py` | Post per-role events with duration, cost, attempt number |

## Backward Compatibility

If `state.json` has old format (`planner_roles: {architect: true}`), migrate:
- Each `true` entry → `{name, status: "complete", attempt: 1}`
- `planner_complete: true` → `planner.status: "complete"`

## Context Optimization (from brainstorm)

While refactoring the pipeline, also implement:
- **P0: Tool-based artifact access** — stop embedding 200K chars in prompts. Tell the agent "Read STATE_DIR/draft_work_plan.json" instead of pasting it inline. Cuts prompt tokens by 60%.
- **P1: Patch mode for Refiner-fix** — output only diffs, not full rewrite.

## Run Isolation

### Problem

Currently `.harness/state/` is a single mutable directory — one run overwrites the previous.
No way to look back at artifacts from past feature runs.

### Design

No singleton state tracker. Every run is an isolated entity.

```
.harness/
  runs.json                              # index of all runs
  runs/
    run-2026-04-02T155600-auth-system/
      state.json
      draft_work_plan.json
      work_plan.json
      spec_gaps.json
      validation.json
    run-2026-04-03T091200-api-refactor/
      ...
```

Two objects, clear responsibilities:

| Object | Scope | Job |
|--------|-------|-----|
| `RunRegistry` | `.harness/` | Create runs, list runs, find runs by status/id, manage `runs.json` index |
| `StateManager` | `.harness/runs/<run_id>/` | Manage state within one run (same API as today, scoped to a run dir) |

### RunRegistry API

```python
registry = RunRegistry(project_dir / ".harness")

# Create a new run — returns run_id
run_id = registry.create_run(prompt="build auth system")

# Get the directory for a run
run_dir = registry.run_dir(run_id)  # → Path

# List all runs
runs = registry.list_runs()  # → list[dict]

# Find latest incomplete run for --resume
run_id = registry.find_resumable()  # → Optional[str]

# Update run status in index
registry.update_run(run_id, status="complete", total_cost_usd=3.24)
```

### runs.json Schema

```json
{
  "runs": [
    {
      "run_id": "run-2026-04-02T155600-auth-system",
      "prompt": "build auth system",
      "status": "complete",
      "started_at": "2026-04-02T15:56:00Z",
      "completed_at": "2026-04-02T18:30:00Z",
      "total_cost_usd": 3.24,
      "features_total": 12,
      "features_passing": 12
    }
  ]
}
```

### Resume Logic

- `--resume` without run ID → `registry.find_resumable()` → latest run with `status != "complete"`
- `--resume --run-id <id>` → target a specific run
- No guessing from file existence

### Backward Compatibility

If `.harness/state/` exists (old layout) and `.harness/runs/` does not:
1. Create `.harness/runs/legacy-migrated/`
2. Move contents of `state/` into it
3. Create `runs.json` index entry
4. Remove `state/` directory

### What Changes (additional)

| File | Change |
|---|---|
| `src/core/state.py` | Add `RunRegistry` class. `StateManager` unchanged (already takes a dir path). |
| `src/core/orchestrator.py` | `run_harness()` creates run via registry. Resume uses `find_resumable()`. Pass `run_dir` to `StateManager`. |

## Test Plan

| Test | Expected |
|---|---|
| Fresh run produces structured planner state | All roles have timestamps, costs, status |
| Resume from crashed refiner | Re-runs refiner, skips architect/adversary |
| Resume after validator rejection | Runs refiner-fix first, then validator |
| Validator rejects twice | Fix loop runs twice, then proceeds with warning |
| Old state format migrates | `planner_roles: {architect: true}` → structured format |
| `/harness-status` reads new format | Shows per-role timing and costs |
| Create run produces isolated directory | `runs/<run_id>/` exists with state.json |
| List runs returns all entries | `runs.json` has correct count and fields |
| Find resumable returns latest incomplete | Skips complete runs, returns most recent in_progress |
| Find resumable returns None when all complete | No false positives |
| Two concurrent runs don't collide | Different run dirs, independent state |
| Old state/ layout migrates to runs/ | Legacy directory moved, index created |
| Resume with explicit run ID works | Targets the specific run, not latest |
