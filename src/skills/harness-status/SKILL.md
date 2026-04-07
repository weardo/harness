---
name: harness-status
description: >-
  Show current harness run progress — features done/total, cost, wave status,
  active agents, worktree health. Use to check on an active or completed run.
user-invocable: true
argument-hint: "[run-id]"
---

# /harness-status — Run Progress

Run the status script (single command, gathers everything):

```bash
python3 .claude/skills/harness-status/status.py --pretty --project "$(pwd)" $ARGUMENTS
```

For terminal watch mode (outside Claude): `python3 .claude/skills/harness-status/status.py -w`

If `$ARGUMENTS` contains a run ID, pass it as `--run <run-id>`.

## Output Rules

**ALWAYS display the FULL script output verbatim in a code block first.** Never summarize,
truncate, paraphrase, or omit any section. The script output is pre-formatted — show every
line exactly as printed.

After showing the full output, you MAY:
- Add observations, insights, or warnings based on what you see (e.g., a stuck worker, high failure rate, cost concerns)
- Investigate further if the user asks (e.g., read logs, check specific task state, dig into failures)
- Suggest actions (e.g., restart a stuck task, increase pool size, drain the run)

But the full output comes first, every time.

## CRITICAL: READ-ONLY — NEVER TAKE ACTION

**This skill is STRICTLY read-only. You MUST NOT:**
- Kill any process (no `kill`, `pkill`, or any signal)
- Modify any harness state file (no writes to session.json, worker_assignments.json, etc.)
- Remove worktrees or branches
- Touch coordination files (claims, instances)
- Run any harness commands (drain, stop, resume)

**Agents routinely take 15-30 minutes per task.** This is NORMAL. A generator or evaluator
running for 20+ minutes is NOT stuck — it is working. Do NOT kill agents based on elapsed time.

Only the user can authorize destructive actions. If you suspect something is genuinely stuck,
**report it and ask the user** — never act on your own.
