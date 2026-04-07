---
name: harness-cost
description: >-
  Show detailed cost breakdown for the current or last harness run. Per-agent,
  per-feature, and total USD. Use to track spend.
user-invocable: true
---

# /harness-cost — Cost Report

## Protocol

1. **Read state:** Read `.harness/runs.json` to find the latest run, then read `<run-dir>/state.json`. If no runs → "No runs yet."

2. **Display cost breakdown:**
   - **Total:** `total_cost_usd`
   - **Per agent:** planner, generator, evaluator from `cost_breakdown`
   - **Per feature (top 5):** If `cost_breakdown` contains feature-level data, show top 5 most expensive

3. **Format as table:**
   ```
   Agent       | Cost
   ------------|--------
   Planner     | $1.23
   Generator   | $4.56
   Evaluator   | $2.34
   ------------|--------
   Total       | $8.13
   ```
