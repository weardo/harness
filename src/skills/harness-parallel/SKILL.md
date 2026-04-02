---
name: harness-parallel
description: >-
  Start an autonomous build with parallel fleet mode forced on. Independent features
  run simultaneously in isolated git worktrees with discovery relay between waves.
user-invocable: true
argument-hint: "<prompt> [--max-cost N]"
---

# /harness-parallel — Parallel Fleet Build

## Protocol

1. **Check harness is installed:**
   ```bash
   ls .harness/run.py
   ```
   If missing → "Harness not installed." STOP.

2. **Run with parallel forced on:**
   ```bash
   HARNESS_PARALLEL=true python3 .harness/run.py --prompt "$ARGUMENTS"
   ```

3. **After completion:** Report summary including wave count, conflicts, and discovery relay stats.
