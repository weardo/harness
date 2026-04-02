---
name: harness-config
description: >-
  View or edit harness configuration — delegation mode, parallel settings,
  test command, cost limits, Mode B toggle. Use to tune harness behavior.
user-invocable: true
argument-hint: "[setting value]"
---

# /harness-config — Configuration

## Protocol

1. **Read config:** Read `.harness/config.yaml`. If missing → "Harness not installed." STOP.

2. **If no arguments:** Display current key settings:
   - Delegation mode: `delegation.mode`
   - Model: `model` / `planner_model`
   - Parallel: `parallel.enabled`, `parallel.max_workers`
   - Test command: `evaluator.test_suite_command`
   - Limits: `max_cost_usd`, `max_duration_minutes`, `max_iterations`
   - Mode B: `mode_b` (true/false)

3. **If arguments provided:** Parse `setting value` and update config.
   Examples:
   - `/harness-config delegation guided` → set `delegation.mode: guided`
   - `/harness-config parallel on` → set `parallel.enabled: true`
   - `/harness-config test "npm test"` → set `evaluator.test_suite_command: "npm test"`
   - `/harness-config mode-b on` → set `mode_b: true`
   - `/harness-config max-cost 50` → set `max_cost_usd: 50`

4. **Write updated config** back to `.harness/config.yaml` preserving structure.
