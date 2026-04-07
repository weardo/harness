# Harness: Graceful Agent Interruption & Worktree-Aware Resume

> **Status:** Draft
> **Date:** 2026-04-03
> **Scope:** `.harness/core/` — orchestrator, parallel, fleet_session, coordination, agent_runner

---

## 1. Goal

Enable the harness to preserve in-progress worktree work across kills and resume interrupted agent sessions into their existing worktrees, so uncommitted or unmerged code is never lost.

## 2. Background

Today, killing the orchestrator (Ctrl+C, SIGTERM, or process crash) destroys all in-flight work:

1. `sweep_stale_instances()` on resume removes coordination files for dead agents
2. `cleanup_worktree()` does `git worktree remove --force` AND `git branch -D` — destroying the branch and any commits on it
3. New waves create fresh worktrees from `main`, agents start from scratch
4. Any code committed to a worker branch but not yet merged to main is permanently lost

**Cost**: Each interrupted agent wastes 15-30 minutes of compute. With 3 parallel workers, a single kill-and-resume cycle burns ~1 hour of wasted work. Over a 141-task run with multiple restarts, this compounds to hours of lost progress.

**Root cause**: There is no durable mapping between worktrees and task assignments that survives process death. The coordination system (instances/claims) is designed for conflict prevention during a live wave, not for crash recovery.

---

## 3. Architecture

### Overview

Four components work together:

```
Signal Handler ──→ Orchestrator Loop ──→ Worker Assignments File
       │                  │                        │
       │           (checks flag)              (persists on disk)
       │                  │                        │
  soft/hard stop    skip new batches          Resume Reader
                    wait or kill workers           │
                                            re-dispatch agents
                                            with recovery prompt
```

### Component 1: Worker Assignments (`fleet/worker_assignments.json`)

A durable file in the run's `state_dir` that maps worktrees to task assignments. Unlike coordination files (which are swept), this file persists across kills and is the source of truth for resume.

**Schema:**

```json
{
  "assignments": {
    "worker-5679": {
      "feature_id": "task-058",
      "branch": "harness/worker-5679",
      "worktree_dir": "/abs/path/to/project/.worktrees/worker-5679",
      "agent_id": "agent-37f60642",
      "wave": 1,
      "scope": ["go-worker/internal/connector/"],
      "assigned_at": "2026-04-03T16:52:00Z",
      "status": "running"
    }
  }
}
```

**Status values:** `running` → `completed` | `interrupted` | `failed`

**Lifecycle:**

| Event | Action |
|-------|--------|
| Worktree created + agent dispatched | Write assignment with `status: "running"` |
| Agent completes normally | Update `status: "completed"` |
| Merge succeeds + cleanup | Remove assignment entry |
| Merge conflict / agent error | Update `status: "failed"`, keep worktree |
| Hard stop (second Ctrl+C) | Update all running → `status: "interrupted"` |
| Resume finds stale assignment | Re-dispatch or clean based on branch state |

**File location:** `{state_dir}/fleet/worker_assignments.json` — same directory as `session.json`. Written via `atomic_write()` for crash safety.

### Component 2: Signal Handling (Graceful Shutdown)

Two-phase shutdown replaces the current bare `KeyboardInterrupt` catch.

**Phase 1 — Soft stop** (first SIGINT/SIGTERM):
- Set `_shutdown_requested = True` on the orchestrator
- Print: `"Shutdown requested. Waiting for active workers to finish (Ctrl+C again to force)..."`
- Current wave's workers continue running to natural completion
- No new batches or waves are started
- After workers finish: merge results, update assignments, save state, exit cleanly

**Phase 2 — Hard stop** (second SIGINT):
- Send SIGTERM to all active worker subprocesses
- Update `worker_assignments.json`: all `running` → `interrupted`
- Persist `session.json` and `state.json` with current progress
- Raise `KeyboardInterrupt` to trigger existing cleanup in `run.py`

**Implementation location:** `run_harness()` in `orchestrator.py`. Signal handlers are registered at function entry, deregistered at exit.

**Worker process tracking:** The orchestrator must hold references to active `asyncio.subprocess.Process` objects (or their PIDs) so hard-stop can kill them. Currently `run_agent_session_cli` creates the subprocess but doesn't expose the process handle. This needs a small change to return/track the PID.

### Component 3: Resume with Existing Worktrees

New function `resume_interrupted_workers()` in `orchestrator.py`, called at the start of the parallel execution path (before `run_parallel_wave` creates new worktrees).

**Decision matrix for each stale assignment:**

| Worktree exists? | Branch has commits ahead of main? | Action |
|:-:|:-:|---|
| Yes | Yes | **Re-dispatch** agent with recovery prompt |
| Yes | No | Clean up worktree + remove assignment (nothing to preserve) |
| No | Yes (branch exists) | Recreate worktree from branch, re-dispatch |
| No | No | Remove assignment, task returns to pending pool |

**Re-dispatch flow:**
1. Read assignment from `worker_assignments.json`
2. Verify worktree/branch state (commits ahead of main?)
3. Claim scope for the feature (same coordination flow as normal dispatch)
4. Register a new agent instance
5. Build recovery prompt with git log context
6. Launch `run_agent_session` in the existing worktree
7. On completion: merge → evaluate → mark done (normal flow)

**Integration point:** Called after `sweep_stale_instances()` but before `group_by_dependency()` / `run_parallel_wave()`. Resumed tasks are excluded from the pending pool so they aren't double-dispatched.

### Component 4: Recovery Prompt (`prompts/generator-resume.md`)

A preamble prepended to the standard generator prompt when an agent is re-dispatched to an existing worktree.

**Template:**

```markdown
## RECOVERY CONTEXT — YOU ARE RESUMING A PREVIOUS SESSION

A previous agent session was interrupted while working on this feature.
You are in the SAME worktree with all its committed work intact.

**Feature**: {{FEATURE_ID}} — {{FEATURE_DESC}}
**Previous commits on this branch:**
{{GIT_LOG}}

### Instructions:
1. Run `git log --oneline main..HEAD` and `git diff --stat main` to see what was done
2. Run `git status` to check for uncommitted changes
3. Assess: is the feature complete, partially done, or broken?
4. If complete → verify (tests, browser) → update feature_list → commit → output STATUS block
5. If partial → continue implementation, don't redo completed work
6. If broken → fix, then continue

### Overrides:
- You are assigned **{{FEATURE_ID}}** — do NOT pick a different feature
- Skip STEP 0 (feedback.md check) and STEP 4 (feature selection) from the standard prompt
- Do NOT start the feature from scratch — build on existing commits
```

**Substitution variables:**
- `{{FEATURE_ID}}`: from assignment
- `{{FEATURE_DESC}}`: looked up from work_plan or feature_list
- `{{GIT_LOG}}`: output of `git log --oneline main..HEAD` run in the worktree

---

## 4. Modified Existing Code

### `sweep_stale_instances()` — coordination.py

Must check `worker_assignments.json` before destroying worktrees:

- If a stale instance has a matching assignment → clean coordination files (instances/claims) only, preserve worktree and branch
- If no matching assignment → full cleanup (current behavior)

### `cleanup_worktree()` — parallel.py

Add a guard: refuse to destroy a worktree if its branch has commits ahead of main, unless `force=True` is passed. Normal completion passes `force=True` after successful merge. Sweep does not.

### `run_parallel_wave()` — orchestrator.py

- Write assignments before launching workers
- Clear assignments after successful merge + cleanup
- Check `_shutdown_requested` before each new batch
- Track worker process handles for hard-stop

### `run_harness()` — orchestrator.py

- Register signal handlers at entry
- Call `resume_interrupted_workers()` before new wave dispatch
- Deregister signal handlers at exit

### `run_agent_session_cli()` — agent_runner.py

- Return or expose the subprocess PID so orchestrator can track it for hard-stop

---

## 5. New Files

| File | Type | Purpose |
|------|------|---------|
| `core/worker_assignments.py` | Module | CRUD for `fleet/worker_assignments.json` — load, save, add, remove, update status |
| `prompts/generator-resume.md` | Template | Recovery preamble for resumed agents |

---

## 6. State File Summary

| File | Survives kill? | Resume behavior |
|------|:-:|---|
| `state.json` | Yes | Determines phase, iteration; `current_feature_id` reset to None |
| `work_plan.json` | Yes | Source of truth for task status; pre-seeds session |
| `feature_list.json` | Yes | Agent-written signal; read by generators |
| `fleet/session.json` | Yes | Re-initialized from work_plan on resume |
| `fleet/worker_assignments.json` | **Yes (NEW)** | Drives worktree re-dispatch on resume |
| `coordination/instances/*.json` | Swept | Stale entries cleaned; assignments take over for recovery |
| `coordination/claims/*.json` | Swept | Re-claimed on re-dispatch |

---

## 7. Edge Cases

### Agent committed but evaluator never ran
Re-dispatched agent verifies the committed work (tests, browser check). If it passes, agent marks feature as done and outputs STATUS block. Normal evaluator flow runs after merge.

### Worktree has uncommitted changes (no commits)
`git status` in the worktree shows modifications but no commits. The recovery prompt tells the agent to check `git status`. Agent can commit the changes and continue, or discard and redo.

### Two assignments for the same feature (double-dispatch race)
Cannot happen: `worker_assignments.json` is keyed by worker ID, and `claim_scope()` prevents overlapping scope claims. A feature can only be assigned to one worker at a time.

### Worktree deleted manually by user
Assignment exists but worktree dir is gone. Branch may or may not exist. Decision matrix in Section 3 handles all four cases.

### Soft stop but worker hangs forever
Workers have a timeout (default 3600s). Soft stop waits for natural completion OR timeout. If user is impatient, second Ctrl+C triggers hard stop.

---

## 8. Out of Scope

- **Mid-session checkpointing within the agent**: Agents don't write incremental state during their session. Recovery relies on git commits as natural checkpoints.
- **Partial output recovery**: If an agent wrote code but didn't commit before kill, that code is lost. This is acceptable — git commits are the checkpoint boundary.
- **Multi-run coordination**: This design is per-run. Different runs don't share worktrees or assignments.
- **Automatic evaluator resume**: If the evaluator was interrupted mid-verdict, the feature is re-evaluated from scratch on resume. Evaluator sessions are short enough that this is acceptable.

---

## 9. Test Plan

### Unit Tests
- [ ] `worker_assignments.py`: add/remove/update/load assignments, atomic write safety
- [ ] `cleanup_worktree()` with guard: refuses to delete branch with commits when `force=False`
- [ ] `sweep_stale_instances()`: preserves worktrees with matching assignments
- [ ] Signal handler state machine: soft stop sets flag, hard stop kills processes
- [ ] `resume_interrupted_workers()`: all four cases in decision matrix

### Integration Tests
- [ ] Full cycle: create worktree → assign → simulate kill → resume → re-dispatch → merge
- [ ] Soft stop: start wave → send SIGINT → workers finish → clean exit
- [ ] Hard stop: start wave → send SIGINT twice → workers killed → assignments preserved
- [ ] Recovery prompt: verify substitution variables are correctly populated from git state

### Manual Verification
- [ ] Run harness with parallel=true, kill mid-wave, resume — verify worktrees preserved and agents continue
- [ ] Verify feature_list counts stay consistent across kill/resume cycles
- [ ] Verify no duplicate task assignments after resume
