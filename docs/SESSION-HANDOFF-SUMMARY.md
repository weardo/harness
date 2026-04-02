# Harness v2 Session Handoff Summary
> Generated 2026-03-28 — carry this into CLI Claude for continuity

---

## What We Built This Session

### 1. R&D Phase (completed, committed to vault)
- **Multi-agent team orchestration R&D** → `vault/industry/multi-agent-team-orchestration-2026.md` (commit `2a9e2ae`)
  - Analyzed AutoGen, CrewAI, LangGraph, Devin patterns
  - Extracted: Epic→Story→Task hierarchy, DAG scheduling, blackboard state, contract-first Phase 0, service-scoped sessions
- **AI/AGI industry transformation analysis** → `vault/industry/ai-agi-software-industry-transformation-2026.md` (commit `10a02f6`)
- **R&D gap identified**: MSG 764 (distributed multi-project harness, K8s, federated knowledge) was only partially absorbed into Tier 1-4 deployment tiers in spec — deferred to v3

### 2. Harness v2 Control Plane — Spec + Plan + Build (completed)
- **Spec**: `harness-dev/specs/harness-v2-orchestration.md` — includes Scale Architecture section (Epic→Story→Task, DAG, blackboard, service isolation)
- **Plan**: `docs/plans/2026-03-28-harness-v2-control-plane-plan.md` (in worktree `jolly-edison`) — 13 phases, 59 features
- **Build**: `harness-dev/test-projects/harness-v2-control-plane/` — **48/48 features passing**, 165 tests all green
- **Scope decision**: v2 = Tier 1 only (single machine, control plane + visibility). Execution engine upgrade (Epic→Story→Task) = v3

### 3. Orchestrator Bug Fixes (5 bugs, all fixed in `harness-dev/src/core/orchestrator.py`)
1. Phase never set to "complete" — added `state_mgr.update_state(phase="complete")` before exit
2. `feature_list.json` wrong location — auto-copies from project root to `.harness/state/`
3. `last_updated` stays null — now written at top of every iteration loop
4. Phase stays "init" after planner — transitions to "generator" after planner completes
5. Cost stays $0 — `run_agent_session_cli` now estimates cost via `output_chars // 4 / 1_000_000 × model_rate`

### 4. Control Plane Server (running)
- **Port**: `localhost:7842` (configured via `HARNESS_PORT` env var, NOT 3001)
- **Stack**: Node.js/Express, SQLite, SSE streaming, static dashboard
- **Seeded data**: 2 projects, 2 completed runs (LLM Obs 50/50 + v2 Control Plane 48/48), 98 feature events, 10 knowledge chunks
- **Process**: Running from `harness-dev/test-projects/harness-v2-control-plane/dist/main.js`

---

## Current Problem: Dashboard Is Unusable

The dashboard at `localhost:7842` renders but is basically empty/useless:

### What the user sees
- Stat cards show numbers (98 passes, 10 KB chunks) but nothing else useful
- No project details — can't see what any project IS
- No feature descriptions — just pass/fail, no idea what each feature does
- No phase hierarchy — can't see the planning phases, nested/repeat cycles
- No run detail — can't drill into a run to see its timeline
- No learnings/retrospective view — knowledge chunks exist in DB but aren't displayed
- No harness orchestrator KB — 10 chunks seeded but UI doesn't load them
- Overview was showing "No active runs" because it queried `?status=running` (all runs are complete)

### Partial fix applied (by previous task)
- `overview.js` rewritten to show stats + recent runs table (not filtered to running)
- `public/js/runs.js` created with full runs table
- Nav updated with Runs link
- BUT: still missing project detail, feature descriptions, phase breakdown, knowledge viewer, retrospective

### Audit in progress
- Task `local_2fb3e0af` ("Full dashboard audit + rebuild") is reading ALL source files and querying SQLite
- Will produce `harness-dev/docs/dashboard-audit-report.md` with full gap analysis
- Was at 51 turns when session ended, still running

---

## Key File Locations

| What | Path |
|------|------|
| Harness orchestrator | `harness-dev/src/core/orchestrator.py` |
| Harness v2 spec | `harness-dev/specs/harness-v2-orchestration.md` |
| v2 plan (worktree) | `docs/plans/2026-03-28-harness-v2-control-plane-plan.md` |
| Built control plane | `harness-dev/test-projects/harness-v2-control-plane/` |
| Control plane source | `harness-dev/test-projects/harness-v2-control-plane/src/` |
| Dashboard frontend | `harness-dev/test-projects/harness-v2-control-plane/public/` |
| API routes | `harness-dev/test-projects/harness-v2-control-plane/src/routes/` |
| SQLite DB | `harness-dev/test-projects/harness-v2-control-plane/data/control-plane.db` |
| State files | `harness-dev/test-projects/harness-v2-control-plane/.harness/state/` |
| Feature list | `.harness/state/feature_list.json` (48 features) |
| Multi-agent R&D | `vault/industry/multi-agent-team-orchestration-2026.md` |
| Industry R&D | `vault/industry/ai-agi-software-industry-transformation-2026.md` |

---

## Worktree Situation

Multiple git worktrees were created during harness runs. Key branches NOT yet merged to master:
- `jolly-edison` — has the v2 plan
- `unruffled-ride` — may have other artifacts
- The built control plane project is in a worktree under `harness-dev/test-projects/`

A merge task was attempted but may not have completed fully. Check `git branch` and `git worktree list`.

---

## Discovered Gotchas

1. **Worktree isolation**: Code tasks run in git worktrees, not main `~/brain`. State files and outputs end up scattered. Use absolute paths (`/Users/prashantpandey/brain/...`) in all commands.
2. **`python` vs `python3`**: Host doesn't have `python` alias — always use `python3`
3. **Brownfield-guard loop**: Claude Code's brownfield-guard discovery can loop before writing files. May need bypassing.
4. **`last_run_at` on projects stays null**: Small bug — events flow doesn't call `projectQueries.updateLastRun`

---

## What's Next (Priority Order)

1. **Wait for / read the dashboard audit report** at `harness-dev/docs/dashboard-audit-report.md`
2. **Fix the dashboard** based on audit — add project detail, feature descriptions, phase hierarchy, knowledge viewer, retrospective view
3. **Merge worktree branches to master** — plan file + artifacts still only in worktrees
4. **v3: Execution engine upgrade** — Epic→Story→Task hierarchical model, DAG, blackboard state, service-scoped sessions (deferred)
5. **Observability integration** — harness instrumenting itself via LLM Observatory SDK (confirmed by user, not yet spec'd)
