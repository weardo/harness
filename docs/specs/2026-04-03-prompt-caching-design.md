# Prompt Caching Optimization — Design Spec

**Date:** 2026-04-03
**Status:** Approved
**Scope:** `run_agent_session_cli` + prompt templates + CostTracker

## Problem

The harness spawns many `claude -p` sessions with nearly identical prompts. Currently the entire prompt (static template + dynamic feature details) is passed as a single `-p` argument — the user message. Claude's prompt caching operates on the system prompt prefix. Since our template is in the user message, not the system prompt, no caching occurs across agents.

Additionally, token consumption is invisible: `--output-format text` discards all usage metadata. State.json shows $0.00 cost and 0 tokens across 22 iterations.

## Goals

1. **Maximize cache hits** across parallel wave agents by separating static template (system prompt) from dynamic content (user message)
2. **Track token consumption** including cache hit/miss rates
3. **Preserve project context** — CLAUDE.md, hooks, and permissions stay active
4. **General-purpose** — design works for any project with a CLAUDE.md

## Non-Goals

- Switching to `--bare` mode (agents need CLAUDE.md)
- Migrating from CLI to SDK (stays subscription-mode compatible)
- Optimizing planner pipeline prompts (single-session, low impact)
- Managing project reference docs (future work)

## Design

### 1. Function Signature Change

`run_agent_session_cli` and `run_agent_session` gain an optional `system_prompt` parameter:

```python
async def run_agent_session(
    prompt: str,                          # user message (dynamic, per-feature)
    options: dict,
    project_dir: Path,
    progress_label: str = "",
    system_prompt: Optional[str] = None,  # static template (cached)
) -> dict:

async def run_agent_session_cli(
    prompt: str,
    project_dir: Path,
    options: Optional[dict] = None,
    system_prompt: Optional[str] = None,
) -> dict:
```

CLI construction:
```python
cmd = ["claude", "-p", prompt, "--output-format", "json",
       "--model", model, "--dangerously-skip-permissions"]
if system_prompt:
    cmd += ["--system-prompt", system_prompt]
```

### 2. Prompt Split Strategy

Each prompt type splits into system (static, cacheable) and user (dynamic, small).

#### Generator

**System prompt** — generator-v2.md template with wave-constant replacements:
- `{{STATE_DIR}}` — same for all agents in a run
- `{{TEST_COMMAND}}` — same for all agents
- `{{IF_WEB_PROJECT}}` — same for all agents

**User message** — feature-specific (50-200 bytes):
```
Your assigned feature: task-063 — Write unit tests for Typesense destination.
Read TASK_BRIEF.md in this directory for full details.
```

Feature-specific placeholders (`{{FEATURE_ID}}`, `{{FEATURE_DESC}}`) move out of the template into the user message.

#### Evaluator

**System prompt** — evaluator-v2.md template with:
- `{{STATE_DIR}}`, `{{TEST_COMMAND}}`, `{{IF_WEB_PROJECT}}`
- `{{RETRY_COUNT}}`, `{{MAX_RETRIES}}` (vary per eval but not per wave)

**User message** — feature details + acceptance criteria:
```
Feature: task-063 — Write unit tests for Typesense destination.

Acceptance Criteria:
- Unit tests exist for Typesense destination connector
- Tests cover connection, indexing, and error handling
...
```

`{{ACCEPTANCE_CRITERIA}}` moves from template to user message.

#### Planner

No split — single session, caching has minimal impact. Stays as-is.

### 3. Call Site Pattern

Parallel wave (primary beneficiary):
```python
# Compute system prompt ONCE for the entire wave
wave_replacements = {
    "STATE_DIR": str(state_dir),
    "TEST_COMMAND": test_command,
    "IF_WEB_PROJECT": is_web_project,
}
gen_system = load_prompt(prompts_dir / "generator-v2.md", wave_replacements)

# Per-feature: only the user message varies
async def _run_one(agent_id, feature, wt_dir):
    user_msg = f"Your assigned feature: {feature['id']} — {feature.get('description', '')}\nRead TASK_BRIEF.md in this directory for full details."
    return await run_agent_session(user_msg, gen_options, wt_dir, system_prompt=gen_system)
```

Resume path and sequential loop follow the same pattern.

### 4. Template Changes

**generator-v2.md:**
- Remove `{{FEATURE_ID}}` and `{{FEATURE_DESC}}` references from template body
- Step 5a (mark feature passing) and 5b (commit message) currently have feature ID hardcoded via `{{FEATURE_ID}}`. Replace with generic instructions that read from TASK_BRIEF.md:
  ```
  Read your feature ID from TASK_BRIEF.md. Use it in the python3 command to mark the feature passing,
  and in the git commit message.
  ```
- The user message also states the feature ID, so the agent has two sources (user message + TASK_BRIEF.md). This redundancy is intentional — the agent sees the ID immediately without file reads.

**evaluator-v2.md:**
- Remove `{{FEATURE_ID}}`, `{{FEATURE_DESC}}`, `{{ACCEPTANCE_CRITERIA}}` from template
- Add instruction: "The feature details and acceptance criteria are provided in the user message below."

### 5. Token Usage + Cache Tracking

#### Already done (this session):
- `--output-format json` replaces `--output-format text`
- JSONL parsing extracts `cost_usd`, `usage`, `duration_ms`, `num_turns`

#### New — cache metrics in CostTracker:

Add fields:
```python
@dataclass
class CostTracker:
    # ... existing fields ...
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
```

Extract from usage:
```python
usage = msg.get("usage", {})
# Standard tokens
input_tokens = usage.get("input_tokens", 0)
output_tokens = usage.get("output_tokens", 0)
# Cache tokens
cache_read = usage.get("cache_read_input_tokens", 0)
cache_creation = usage.get("cache_creation_input_tokens", 0)
```

Display in `format_summary()`:
```
Tokens: 2,450,000 (1,800,000 in / 650,000 out)
Cache: 1,200,000 read / 600,000 created (67% hit rate)
```

Persist in `to_dict()` / `from_dict()` for state.json.

### 6. Cache Behavior

Claude's prompt cache evaluates: tools → system → messages.

With `--system-prompt`, our template becomes part of the system context alongside CLAUDE.md. The cache key is the prefix hash.

**Parallel wave (3 agents):**
1. Agent 1: cache MISS → `cache_creation_input_tokens` ≈ 4K
2. Agent 2 (seconds later): cache HIT → `cache_read_input_tokens` ≈ 4K (90% savings)
3. Agent 3: cache HIT → same

**Sequential agents (>5min apart):**
- Cache expires (5-min TTL) — each gets a cache MISS
- Still benefits from smaller user message

**Evaluator after generator (same wave):**
- Different system prompt (evaluator-v2.md vs generator-v2.md) — separate cache entry
- But if multiple features evaluated in sequence within 5min, evaluator cache hits

### 7. Files Modified

| File | Change |
|------|--------|
| `src/core/orchestrator.py` | `run_agent_session` + `run_agent_session_cli` signature; all call sites split prompt |
| `src/prompts/generator-v2.md` | Remove feature-specific placeholders; reference TASK_BRIEF.md for IDs |
| `src/prompts/evaluator-v2.md` | Remove feature-specific placeholders; reference user message |
| `src/core/cost_tracker.py` | Add `cache_read_tokens`, `cache_creation_tokens`; update `to_dict`/`from_dict`/`format_summary` |
| `src/skills/harness-status/SKILL.md` | Display token + cache metrics |

### 8. Risks

- **`--system-prompt` arg length limits:** Shell argument length limit is ~256KB on macOS. Our templates are 2-4KB — no risk.
- **Cache not guaranteed:** Claude may not cache prompts below minimum threshold (2,048 tokens for Sonnet). CLAUDE.md + our template should comfortably exceed this.
- **Subscription mode cache behavior:** Cache metrics may not be reported in usage on Max subscription. If `cache_read_input_tokens` is absent, we still get `input_tokens`/`output_tokens` for basic tracking.

### 9. Success Criteria

- state.json shows non-zero `input_tokens` and `output_tokens` after a wave completes
- `cache_read_input_tokens > 0` for agents 2+ in a parallel wave (if reported)
- No regression in agent behavior — generators still find TASK_BRIEF.md, evaluators still get acceptance criteria
- harness-status displays token consumption
