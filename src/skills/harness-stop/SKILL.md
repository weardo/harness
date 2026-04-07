---
name: harness-stop
description: >-
  Stop a running harness. Three modes: --drain (default) finishes current
  generators+evaluators then exits cleanly; --now sends SIGTERM; --force
  sends SIGKILL. All modes preserve state for /harness-resume.
user-invocable: true
argument-hint: "[--drain|--now|--force]"
---

# /harness-stop — Stop Running Harness

## Protocol

1. **Find the active run directory:**
   ```bash
   # Find harness process
   pgrep -f 'run.py' || pgrep -f 'harness.*run'
   ```
   If no process found → "No harness is currently running."

   Read `.harness/runs.json` to find the active run, then read its `state.json`.
   Display: `Run <run-id> at iteration <N>, feature <id> (<done>/<total> done)`

2. **Stop based on mode:**

   ### `--drain` (default, no args)
   Writes a drain file. The orchestrator finishes current generators + evaluators,
   updates state.json, then exits cleanly. No agents are killed.
   ```bash
   # Write drain signal — orchestrator checks this between iterations
   touch <state_dir>/drain
   ```
   Confirm: `Drain requested. Harness will stop after current agents finish. Resume later with /harness-resume`

   ### `--now`
   Sends SIGTERM — the orchestrator's signal handler waits for active workers
   to finish their current tool call, then exits.
   ```bash
   kill <pid>
   ```

   ### `--force`
   Sends SIGKILL — immediate stop, workers are killed mid-execution.
   Worktree assignments are preserved for resume.
   ```bash
   kill -9 <pid>
   ```

3. **For --now and --force, verify stopped:**
   ```bash
   sleep 2 && pgrep -f 'run.py'
   ```
   If still running after `--now`: "Process didn't stop. Use `--force` to kill immediately."

4. **Confirm:**
   ```
   Harness stopped. Resume with: /harness-resume
   ```
