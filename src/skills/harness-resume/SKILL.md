---
name: harness-resume
description: >-
  Resume a harness run after crash or interruption. Picks up from the last
  checkpoint — same features, same state, no lost work.
user-invocable: true
---

# /harness-resume — Resume Build

## Protocol

1. **Check harness is installed:**
   ```bash
   ls .harness/run.py
   ```
   If missing → "Harness not installed." STOP.

2. **Check state exists:**
   Read `.harness/state/state.json`. If missing → "No previous run to resume." STOP.

3. **Resume:**
   ```bash
   python3 .harness/run.py --resume
   ```

4. **After completion:** Report final summary.
