---
name: harness-resume
description: >-
  Resume a harness run after crash or interruption. Picks up from the last
  checkpoint — same features, same state, no lost work.
user-invocable: true
argument-hint: "[run-id]"
---

# /harness-resume — Resume Build

## Protocol

1. **Check harness is installed:**
   ```bash
   ls .harness/run.py
   ```
   If missing → "Harness not installed." STOP.

2. **Check for resumable runs:**
   Read `.harness/runs.json`. If missing or no runs → check for legacy `.harness/state/state.json` (auto-migrates on run).

   If `runs.json` exists, list runs with `status: "in_progress"`:
   ```
   Resumable runs:
     run-20260402T155600-auth-system  (started 2026-04-02, $3.24)
     run-20260401T091200-api-refactor (started 2026-04-01, $1.50)
   ```

3. **Resume:**
   - If `$ARGUMENTS` contains a run ID → `python3 .harness/run.py --resume --run-id <id>`
   - If only one resumable run → `python3 .harness/run.py --resume`
   - If multiple resumable runs and no argument → show list, ask user which one

4. **After completion:** Report final summary.
