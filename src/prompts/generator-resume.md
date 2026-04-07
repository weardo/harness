## RECOVERY CONTEXT — YOU ARE RESUMING A PREVIOUS SESSION

A previous agent session was interrupted while working on this feature.
You are in the SAME worktree with all its committed work intact.

**Feature**: {{FEATURE_ID}} — {{FEATURE_DESC}}

**Previous commits on this branch:**
```
{{GIT_LOG}}
```

### What to do:

1. Run `git log --oneline main..HEAD` and `git diff --stat main` to see what was done
2. Run `git status` to check for uncommitted changes
3. Assess: is the feature complete, partially done, or broken?
4. If complete → verify (tests, browser) → update feature_list → commit → output STATUS block
5. If partial → continue implementation from where it left off, don't redo completed work
6. If broken → fix issues, then continue

### Overrides:

- You are assigned **{{FEATURE_ID}}** — do NOT pick a different feature
- Skip STEP 0 (feedback.md check) and STEP 4 (feature selection) from the standard prompt below
- Do NOT start the feature from scratch — build on existing commits
- All other steps (STEP 5 onwards: implement, verify, commit, status block) still apply

---

