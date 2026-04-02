---
name: harness-status
description: >-
  Show current harness run progress — features done/total, cost, wave status,
  circuit breaker state. Use to check on an active or completed run.
user-invocable: true
---

# /harness-status — Run Progress

## Protocol

1. **Check for state file:**
   Read `.harness/state/state.json`. If missing → "No active harness run."

2. **Display run status:**
   From state.json extract and display:
   - **Phase:** current phase (init/planner/generator/evaluator)
   - **Iteration:** current iteration number
   - **Cost:** total_cost_usd + cost_breakdown (planner/generator/evaluator)
   - **Started:** started_at timestamp

3. **Feature progress:**
   Read `.harness/state/feature_list.json` or `.harness/state/work_plan.json`.
   Count: total, passing/done, blocked, remaining.
   Display as: `Features: 12/20 done, 2 blocked, 6 remaining`

4. **Wave progress (if parallel):**
   Read `.harness/fleet/session.json`. If exists, display:
   - Current wave number
   - Waves completed
   - Requeued features
   - Discoveries count

5. **Circuit breaker:**
   Read `.harness/state/circuit_breaker.json`. Display state (CLOSED/HALF_OPEN/OPEN) and total opens.
