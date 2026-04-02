# Planner State Machine — Proper Run Tracking

**Status:** Draft
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

## Test Plan

| Test | Expected |
|---|---|
| Fresh run produces structured planner state | All roles have timestamps, costs, status |
| Resume from crashed refiner | Re-runs refiner, skips architect/adversary |
| Resume after validator rejection | Runs refiner-fix first, then validator |
| Validator rejects twice | Fix loop runs twice, then proceeds with warning |
| Old state format migrates | `planner_roles: {architect: true}` → structured format |
| `/harness-status` reads new format | Shows per-role timing and costs |
