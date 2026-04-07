# Submodule merge conflict resolution — design options

**Status**: Tier 1 (mechanical) + Tier 2 (LLM) implemented 2026-04-07
**Created**: 2026-04-07
**Context**: During the `run-legacy-migrated` run, 5 workers all touched shared admin-ui files (`api.ts`, `hooks/index.ts`, `types.ts`, `messages/*.json`) simultaneously. When their branches needed to merge into `main`, submodule conflicts cascaded — each successful merge invalidated all in-flight workers' submodule pointers.

## Problem statement

When multiple workers modify the same git submodule in parallel:

1. Worker A finishes → merges into main → main's submodule pointer advances to SHA-A
2. Worker B is still running on its branch with submodule pointer SHA-B (forked from old main)
3. Worker B finishes → tries to merge → git fails with `CONFLICT (submodule)`
4. Worker C, D, E all have the same problem

The existing `_auto_resolve_submodule_conflicts` in `src/core/parallel.py` handles the **fast-forward** and **submodule-ancestor** cases but bails on:
- **Real file conflicts inside the submodule** — two workers modified the same file (e.g., `api.ts`) differently
- **SHA reachability** edge cases (partially addressed)

The **JSON union resolver** added 2026-04-07 handles `messages/*.json` file conflicts by union of keys. This covers ~80% of real-world cases in this session but NOT TypeScript conflicts.

## Options considered

### Option 1: Extend mechanical rule-based resolvers

Write pattern-specific resolvers for common conflict types:
- TS import statement union (`import { A, B } from 'x'` → keep all imports)
- TS export-alias block union (`export type { A as X }` — keep all)
- TS interface addition union (if both sides added new interfaces, keep both)
- Python import union
- Go const/var block union

**Pros**: Deterministic, fast (<10ms), zero cost, testable.
**Cons**: Combinatorial — each language × each conflict pattern needs its own parser. Infinite yak-shaving. Fragile for edge cases.

**Verdict**: Worth doing for the highest-frequency patterns (TS imports specifically), but not as the only answer.

### Option 2: LLM conflict resolver agent (preferred long-term)

Spawn a dedicated `claude-haiku` agent per conflicted file:

```
SYSTEM: You are a git merge conflict resolver. Given a file with conflict
markers, produce the resolved file.
  - If both sides ADDED DIFFERENT content: union (keep both)
  - If both sides modified the SAME code: output <<<CANNOT_RESOLVE>>>
  - NEVER invent code beyond what's in the conflict blocks
  - Output ONLY the resolved file content
USER: <file contents with markers>
```

**Validation layer** after agent response:
1. No remaining conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`)
2. File parses as the expected type (json.loads, tsc --noEmit, etc.)
3. If validation fails → revert, fall through to next tier

**Pros**: Handles any language/pattern, semantic awareness, small cost (~$0.01-0.05 per file with Haiku), writes-once-works-forever.
**Cons**: Non-deterministic, requires API call, slower (3-10 sec), must have robust fallback.

**Cost math**: 5 conflict files × $0.05 = $0.25 per stuck worker. Compared to re-running generator+evaluator at $10-30 per retry, that's 40-120x cheaper.

### Option 3: Sequential merge with rebase

Instead of merging all 5 workers in parallel:
1. Merge worker A → main
2. Before merging B, rebase B's branch onto main's new HEAD (submodule pointers fast-forward automatically)
3. If rebase hits conflicts, resolve them in the worker's worktree (agent can help)
4. Merge B → main
5. Repeat for C, D, E

**Pros**: Eliminates the cascade structurally. Natural serialization matches git's model.
**Cons**: Loses parallelism for workers touching shared code (which is the whole point of parallel mode). Rebase conflicts still need resolution (same as merge conflicts). Worse UX for rebase failures (more complex than merge failures).

**Verdict**: Could be a mode flag — `parallel.conflict_strategy: merge-cascade | rebase-sequential`. Default to cascade (current), opt-in to rebase for high-overlap runs.

### Option 4: Delegate to recovery/generator agent

When a merge conflict is detected, send the worker's generator agent a new prompt:
> "Your branch's merge into main failed with the following conflicts: [files]. Please rebase onto main's current HEAD and resolve the conflicts, then commit."

**Pros**: Reuses existing generator session (continues with full context), no new agent infrastructure. Agent already knows the codebase.
**Cons**: Generator is expensive (Sonnet, $0.20-2.00 per invocation). Loses time as agent re-explores to understand the conflict. Mixes conflict resolution with feature work — confusing for the agent.

**Verdict**: Lower-priority alternative. Only useful if Option 2 (dedicated haiku resolver) proves insufficient.

### Option 5: Tighter scope declarations (root cause fix)

The real reason all 5 workers needed to touch the same files is that they share the admin-ui shared layer (`data-management/api.ts`, `hooks/index.ts`, etc.). The planner declared each worker's scope too loosely (`[create/]`, `[]`, `[page.tsx]`) — the scope-overlap detector didn't trip.

If each worker declared its ACTUAL write scope (`admin-ui/apps/isomorphic-intl/src/app/shared/data-management/*`), the scope-overlap detector would serialize them automatically.

**Pros**: Prevents the problem at its source. No merge conflicts to resolve because workers never touch shared code in parallel. Zero cost.
**Cons**: Requires planner to produce tighter scope declarations. Tasks 108/109/113/118/123 would be forced into sequential mode — losing some parallelism but getting predictable merges.

**Verdict**: This is the RIGHT fix. The other options are workarounds for scope laxity. Worth a separate plan: "tighten planner scope declarations for shared-layer modifications."

## Recommended architecture (long-term)

**Tiered conflict resolution** in `_try_auto_resolve_inner_conflicts`:

```
Tier 0: root-cause prevention (planner gives tight scope) → no conflict
Tier 1: mechanical rules (JSON union, TS import union)   → fast, free, safe
Tier 2: LLM conflict resolver agent (Haiku)              → semantic, bounded cost
Tier 3: delegate to worker's generator (Sonnet)          → last resort, expensive
Tier 4: abort merge, mark worker blocked                 → human intervention
```

Each tier is tried in order. If a tier fails, fall through to the next. Config knobs:

```yaml
parallel:
  auto_resolver:
    enabled: true
    mechanical_rules: true     # Tier 1
    llm_agent: true            # Tier 2
    llm_agent_model: haiku-4-5
    delegate_to_generator: false  # Tier 3 (off by default)
```

## Current state (as of 2026-04-07)

- ❌ Tier 0: not implemented (requires planner changes — separate plan)
- ✅ Tier 1: mechanical JSON union for `messages/*.json` — implemented + tested
- 🟡 Tier 1: TS import union — not implemented (LLM resolver covers this case)
- ✅ Tier 2: LLM resolver agent (Haiku) — implemented in `src/core/conflict_resolver.py`,
             wired into `_try_auto_resolve_inner_conflicts` via `parallel.py`, 27 tests passing.
             Config: `parallel.auto_resolver.llm_agent` (default true), `.llm_agent_model` (default `claude-haiku-4-5`).
             Validation: marker-free, JSON parse for `.json`, balanced-bracket check for TS/JS.
             Falls back to Tier 4 on invalid output or `<<<CANNOT_RESOLVE>>>` sentinel.
- ❌ Tier 3: delegate to generator — not implemented
- ✅ Tier 4: abort + manual intervention — current behavior

## Immediate workaround (what worked manually)

When Tier 2+ are missing, the human operator can:

1. For each stuck worker:
   ```bash
   cd .worktrees/worker-XXXX/admin-ui
   git fetch ../../.. admin-ui:refs/heads/main-target
   git merge main-target  # or from a temp branch pointing at main's SHA
   # resolve conflicts (JSON union script, manual for TS)
   git commit
   cd ..
   git add admin-ui
   git commit -m "fix(submodule): rebase on main pre-merge"
   ```
2. Resume harness — the pre-advanced submodule pointer makes the parent merge a fast-forward.

This is what happened twice in the 2026-04-07 session. It's labor-intensive but reliable.

## Metrics to track when implementing

- % of merges that reach Tier 2 (mechanical rules insufficient)
- Tier 2 success rate (LLM agent resolves cleanly)
- Tier 2 validation rejection rate (agent output failed parse/compile check)
- Avg cost per Tier 2 invocation
- Time-to-merge distribution (before/after)

## Related files

- `src/core/parallel.py` — `merge_worktree`, `_auto_resolve_submodule_conflicts`, `_try_resolve_json_union` (implemented), `_try_auto_resolve_inner_conflicts` (implemented)
- `src/core/worker_assignments.py` — `live_state` tracking (so observers can see merge-conflict state)
- `src/skills/harness-status/status.py` — displays merge state via `MRG✗`, `CONF` tags
