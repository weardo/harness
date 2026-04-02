---
name: harness-run
description: >-
  Start an autonomous harness build. Runs the 3-agent Planner→Generator↔Evaluator
  loop with circuit breaker. Use for long-running autonomous builds from a prompt,
  spec, or plan.
user-invocable: true
argument-hint: "<prompt> [--spec path] [--plan path] [--max-cost N] [--delegation mode]"
---

# /harness-run — Autonomous Build

## Protocol

1. **Check harness is installed:**
   ```bash
   ls .harness/run.py
   ```
   If missing → "Harness not installed. Run `bash ~/harness/install.sh` first." STOP.

2. **Parse arguments from `$ARGUMENTS`:**
   - If starts with `--spec` → extract path, use `--spec` flag
   - If starts with `--plan` → extract path, use `--plan` flag
   - If contains `--max-cost` → extract value
   - If contains `--delegation` → extract mode (full-plan, guided, outcome-only)
   - Otherwise → treat entire argument as the prompt

3. **Run the harness:**
   ```bash
   python3 .harness/run.py --prompt "$PROMPT" [--max-cost N] [--delegation mode]
   ```
   This is a long-running command. Let it run — it will output progress as it goes.

4. **After completion:** Read `.harness/state/state.json` and report final summary:
   - Features: done/total/blocked
   - Cost: total USD
   - Duration
   - Circuit breaker opens
