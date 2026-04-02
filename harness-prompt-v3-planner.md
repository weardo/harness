# Harness v3 — Multi-Phase Planner + Epic/Story/Task + Full Observability

Three upgrades in one deliverable:

1. **Multi-phase planner** — replace the monolithic single-session planner with a 4-role pipeline (Architect → Adversary → Refiner → Validator), each as a separate `claude -p` session with focused context, typed artifacts, and schema+quality validation between roles.

2. **Epic/Story/Task hierarchy** — replace the flat `feature_list.json` with a 3-tier work model. Epics contain Stories, Stories contain Tasks. Tasks are the atomic generator unit. Stories are the evaluator unit. Epics are the integration unit. The orchestrator enforces dependency ordering and phase gates.

3. **Full lifecycle observability** — every state transition in the harness posts an event to the control plane. The user sees the ENTIRE run from start to finish in the dashboard: worktree sweep → knowledge query → planner roles → epic/story/task execution → evaluator verdicts → completion → retrospective. Nothing is invisible.

This is a **brownfield build** — the codebase exists at `src/core/` with ~2,700 lines across 13 modules, all with 268 passing tests.

## Design Principle: EVERYTHING IS AN EVENT

Every `print()` in the harness MUST have a corresponding `cp.post_event()`. If the terminal sees it, the dashboard sees it. Events use the existing `feature_events` table with `feature_id` prefixed by phase:

```
feature_id format:
  "setup-{step}"     → pre-flight events (worktree sweep, browser check, knowledge query)
  "planner-{role}"   → planner role events (architect, adversary, refiner, validator)
  "{NNN}"            → generator/evaluator feature events (001, 002, ... — unchanged)
  "completion-{step}" → completion events (suspicion check, retrospective, knowledge ingest)
```

The dashboard can group/filter by prefix to show phases separately.

## Reference Documents
- Design spec: Read `docs/superpowers/specs/2026-03-30-harness-v3-multi-phase-planner-design.md` — this is the approved design. Follow it exactly.
- Existing code: `src/core/` — read ALL modules before planning, especially `orchestrator.py`, `state.py`, `client.py`, `knowledge_client.py`
- Existing prompts: `src/prompts/planner.md` (the monolithic prompt being replaced)
- Existing tests: `tests/` — read test patterns before writing new tests
- Config: `src/config.yaml`

## Tech Stack
- Python 3.11+
- pytest (match existing test patterns)
- PyYAML (already used for config)
- No new dependencies

## Existing Architecture

```
src/core/
  orchestrator.py      ← 3-agent loop (Planner → Generator ↔ Evaluator)
  state.py             ← Atomic JSON state management
  circuit_breaker.py   ← Nygard state machine
  cost_tracker.py      ← Per-agent + per-feature cost tracking
  progress.py          ← Multi-source progress detection
  completion.py        ← Completion + suspicion heuristic
  client.py            ← Claude SDK client factory (model selection, MCP, security)
  control_plane.py     ← HTTP client for control plane
  knowledge_client.py  ← Format knowledge chunks for planner
  worktree.py          ← Worktree lifecycle management
  security.py          ← Bash command allowlist
  parallel.py          ← Parallel teams (disabled)
src/prompts/
  planner.md           ← BEING REPLACED (keep as fallback)
  generator.md         ← Unchanged
  evaluator.md         ← Unchanged
src/config.yaml        ← Add strategies section
```

## What Already Works (DO NOT rebuild)
- Generator ↔ evaluator loop with circuit breaker
- Cost tracking (per-agent + per-feature)
- Knowledge injection into planner context
- Planner validation gate (feature_list.json + SPEC_GAPS.md)
- Worktree lifecycle, suspicion heuristic, browser pre-flight
- Control plane integration, state persistence, resume support
- `run_agent_session()` and `run_agent_session_cli()` — reuse these for role sessions
- `load_prompt()` and `process_conditionals()` — reuse for role prompt loading
- `create_client_options()` — reuse with `model_override` for per-role model selection

## What to Build (7 Features)

### Feature 1: Planner Pipeline Module — Validation Functions

Create `src/core/planner_pipeline.py` with two validation functions:

```python
def validate_json_array(path: Path, min_items: int, required_fields: list[str]) -> dict:
    """Returns {valid: bool, reason: str}"""

def validate_markdown(path: Path, min_length: int, required_headings: list[str], min_gaps: int = 0) -> dict:
    """Returns {valid: bool, reason: str}"""
```

**Acceptance criteria:**
- `validate_json_array` on a valid file with 10 items and required fields → `{valid: True}`
- `validate_json_array` on a file with 2 items when min_items=5 → `{valid: False, reason: "...2 items...min 5..."}`
- `validate_json_array` on items missing required field "acceptance_criteria" → `{valid: False, reason: "...missing field..."}`
- `validate_json_array` on non-existent file → `{valid: False, reason: "...not found..."}`
- `validate_json_array` on invalid JSON → `{valid: False, reason: "...invalid JSON..."}`
- `validate_markdown` on file with 500 chars and all required headings → `{valid: True}`
- `validate_markdown` on file with 50 chars when min_length=200 → `{valid: False, reason: "...too short..."}`
- `validate_markdown` on file missing "AI Failure Modes" heading → `{valid: False, reason: "...missing heading..."}`
- `validate_markdown` with min_gaps=3 on a file with only 1 gap entry → `{valid: False, reason: "...only 1 gap..."}`
- Gap counting: count lines starting with `### GAP` or `### Gap` or numbered patterns like `1.` under gap headings
- Tests use real temp files, not mocks

### Feature 2: Planner Pipeline Module — Role Execution

Add `run_planner_pipeline()` to `src/core/planner_pipeline.py`:

```python
async def run_planner_pipeline(
    strategy_config: dict,
    prompts_dir: Path,
    project_dir: Path,
    state_dir: Path,
    state_mgr: StateManager,
    cost_tracker: CostTracker,
    knowledge_section: str,
    input_section: str,
) -> dict:
```

This function loops through `strategy_config["planner_roles"]`, running each role as a separate agent session via the existing `run_agent_session()`.

**Acceptance criteria:**
- Calls `run_agent_session()` once per role (4 times for feature strategy)
- Each role's prompt is loaded from `prompts_dir/{role.prompt}` via existing `load_prompt()`
- Architect prompt gets `knowledge_section` + `input_section` appended
- Adversary prompt gets contents of `spec.md` + `draft_features.json` appended
- Refiner prompt gets contents of `spec.md` + `draft_features.json` + `SPEC_GAPS.md` appended
- Validator prompt gets contents of `spec.md` + `feature_list.json` + `SPEC_GAPS.md` appended
- Model resolved per role: `"opus"` → `config["planner_model"]`, `"sonnet"` → `config["model"]`
- `create_client_options()` called with `model_override` set to resolved model
- Cost recorded via `cost_tracker.record("planner", cost)` after each role
- Returns `{success: bool, roles_completed: list[str], total_cost: float}`
- Prints status after each role: `"  {name}: complete. Cost: ${cost:.2f}. Artifact: {artifact}"`

### Feature 3: Planner Pipeline — Validation Gate Between Roles

After each role session, validate the artifact using the role's `validation` config.

**Acceptance criteria:**
- After Architect: validate `draft_features.json` as json_array with min_items and required_fields
- After Adversary: validate `SPEC_GAPS.md` as markdown with min_length, required_headings, min_gaps
- After Refiner: validate `feature_list.json` as json_array with full required_fields
- After Validator: validate `VALIDATION.md` as markdown with required_headings
- If validation fails: build retry prompt = original prompt + "VALIDATION FAILED: {reason}" + failed artifact content
- Re-run the role session (1 retry max)
- If retry also fails: print warning, proceed to next role
- If retry succeeds: print success message with retry cost

### Feature 4: Planner Pipeline — Resume Support

Check `state_mgr` before each role — skip roles already marked complete.

**Acceptance criteria:**
- `planner_roles` dict in state.json tracks completion per role name
- If `planner_roles.architect == true`, skip Architect and load its artifacts from disk
- If all roles complete, `planner_complete` set to `true`
- `current_planner_role` updated in state before each role starts
- Crash after Adversary + resume → starts at Refiner (reads Architect + Adversary artifacts from disk)
- Old state.json without `planner_roles` field + `planner_complete=false` → runs all roles
- Old state.json without `planner_roles` field + `planner_complete=true` → skips pipeline entirely
- Tests verify resume by pre-populating state.json and artifact files

### Feature 5: Role Prompt Files

Create 4 prompt files in `src/prompts/`:

**`architect.md`** (~50 lines):
- Role: Product architect designing a comprehensive application specification
- Reads knowledge chunks + input spec/prompt
- Produces: `spec.md` (Overview, Architecture, Features, Data Model, Security, Success Criteria) + `draft_features.json` (JSON array with id, priority, category, depends_on, description, acceptance_criteria, steps, passes, blocked, retries)
- Rules: behavioral acceptance criteria, scale 20-200 features, setup first then features then integration then testing
- Does NOT: review its own work, write SPEC_GAPS.md, write init.sh

**`adversary.md`** (~45 lines):
- Role: Hostile QA engineer. "You did NOT write this spec. Your job is to FIND PROBLEMS."
- Reads: spec.md + draft_features.json from state_dir
- Produces: `SPEC_GAPS.md` with 3 sections: "## Technical Gaps", "## AI Failure Modes", "## Community Alignment"
- Each gap: `### GAP-{N}: {title}` with Risk (HIGH/MEDIUM/LOW), What breaks, Fix
- Rules: must find at least 3 gaps, interpret ambiguity strictly, think about what the generator will get wrong

**`refiner.md`** (~40 lines):
- Role: Senior engineer turning a reviewed spec into a bulletproof build plan
- Reads: spec.md + draft_features.json + SPEC_GAPS.md from state_dir
- Produces: `feature_list.json` (final, revised)
- Rules: address every gap (new feature, tightened criteria, or explicit rejection with reason), preserve Architect's IDs, every feature must have `passes: false, blocked: false, retries: 0`

**`validator.md`** (~45 lines):
- Role: Release gate. Verify and sign off.
- Reads: spec.md + feature_list.json + SPEC_GAPS.md from state_dir
- Produces: `VALIDATION.md` (Coverage Matrix mapping spec requirements to feature IDs, Uncovered Requirements list, Feature Ordering check, Sign-off statement) + `init.sh` (idempotent setup script)
- Rules: reject if coverage gaps found (list them), check feature ordering (setup < features < integration < testing), init.sh must be idempotent

**Acceptance criteria:**
- Each prompt file exists in `src/prompts/`
- Each prompt uses `{{STATE_DIR}}` placeholder for state directory path (loaded via existing `load_prompt()`)
- Architect prompt does NOT reference SPEC_GAPS.md or init.sh
- Adversary prompt explicitly says "You did NOT write this spec"
- Refiner prompt explicitly says "address every gap — do not drop gaps silently"
- Validator prompt includes Coverage Matrix format

### Feature 6: Orchestrator Integration

Replace the ~50-line planner section in `orchestrator.py` with a call to `run_planner_pipeline()`.

**Acceptance criteria:**
- `from .planner_pipeline import run_planner_pipeline` at top of orchestrator.py
- Reads `default_strategy` and `strategies` from config
- If strategy config exists: calls `run_planner_pipeline()` with strategy_config, prompts_dir, project_dir, state_dir, state_mgr, cost_tracker, knowledge_section, input_section
- If strategy config missing: falls back to legacy single-session planner (existing `planner.md` + `validate_planner_output()`)
- After pipeline completes: sets `planner_complete=true`, posts `run_start` event with feature count
- Knowledge injection uses existing `_query_knowledge_for_planner()`
- Input section built from `--prompt`, `--spec`, or `--plan` (same as current `build_planner_prompt()`)
- `build_planner_prompt()` function kept for legacy fallback — NOT deleted
- All existing orchestrator tests still pass

### Feature 7: Config + Tests + Integration

**Update `src/config.yaml`:**
```yaml
strategies:
  feature:
    planner_roles:
      - name: architect
        model: opus
        prompt: architect.md
        artifact: draft_features.json
        also_produces: [spec.md]
        validation:
          type: json_array
          min_items: 5
          required_fields: [id, description, acceptance_criteria]
      - name: adversary
        model: opus
        prompt: adversary.md
        artifact: SPEC_GAPS.md
        validation:
          type: markdown
          min_length: 200
          required_headings: ["Technical Gaps", "AI Failure Modes", "Community Alignment"]
          min_gaps: 3
      - name: refiner
        model: sonnet
        prompt: refiner.md
        artifact: feature_list.json
        validation:
          type: json_array
          min_items: 5
          required_fields: [id, description, acceptance_criteria, steps, passes, blocked]
      - name: validator
        model: opus
        prompt: validator.md
        artifact: VALIDATION.md
        also_produces: [init.sh]
        validation:
          type: markdown
          min_length: 100
          required_headings: ["Coverage Matrix", "Sign-off"]

default_strategy: feature
```

**Create `tests/test_planner_pipeline.py`:**
- `TestValidateJsonArray` — valid file, too few items, missing fields, non-existent, invalid JSON
- `TestValidateMarkdown` — valid file, too short, missing headings, min_gaps check
- `TestRunPlannerPipeline` — mock `run_agent_session`, verify 4 calls with correct prompts and models
- `TestPipelineResume` — pre-populate state with partial completion, verify skipped roles
- `TestPipelineRetry` — mock validation failure then success, verify retry with feedback prompt
- `TestPipelineLegacyFallback` — no strategy config → legacy planner runs
- `TestBackwardCompat` — old state.json format still works

**Update `tests/test_orchestrator.py`:**
- Verify pipeline is called when strategy config exists
- Verify legacy fallback when strategy config absent

**Acceptance criteria:**
- `python3 -m pytest tests/ -v` passes (all existing 268 + new tests)
- Config loads correctly with new strategies section
- Harness run produces all 4 artifacts in state_dir: spec.md, draft_features.json (or overwritten by feature_list.json), SPEC_GAPS.md, VALIDATION.md, init.sh
- Legacy runs (no strategy config) still work identically

### Feature 8: Full Lifecycle Observability

Every state transition in the harness posts an event to the control plane. The user sees the ENTIRE run from start to finish in the dashboard. Nothing is invisible.

**Create:** `src/core/event_tracker.py` — thin wrapper around `ControlPlaneClient` that provides named methods for each lifecycle event. All methods are fire-and-forget (try/except, never crash the harness). Works when control plane is not running.

```python
class EventTracker:
    """Posts lifecycle events to control plane. Fire-and-forget, never crashes."""

    def __init__(self, cp: ControlPlaneClient):
        self.cp = cp

    # Setup phase
    def run_started(self, features_planned: int = 0): ...
    def worktree_sweep(self, phase: str, removed_count: int): ...
    def browser_preflight(self, result: str): ...
    def knowledge_query(self, chunks_found: int): ...

    # Planner phase
    def planner_role_start(self, role_name: str, desc: str): ...
    def planner_role_pass(self, role_name: str, artifact: str, duration_ms: int): ...
    def planner_role_fail(self, role_name: str, reason: str): ...
    def planner_role_retry(self, role_name: str): ...
    def planner_validation_summary(self, roles_completed: list, total_cost: float): ...

    # Generator/Evaluator phase (upgrade existing cp.post_event calls)
    def feature_start(self, feature_id: str, desc: str): ...
    def feature_pass(self, feature_id: str, desc: str, duration_ms: int): ...
    def feature_fail(self, feature_id: str, desc: str, error: str): ...
    def feature_retry(self, feature_id: str, desc: str, attempt: int): ...
    def cb_open(self, reason: str): ...
    def cb_close(self): ...

    # Completion phase
    def suspicion_warning(self, reasons: list): ...
    def run_complete(self, features_done: int, cost_usd: float): ...
    def run_failed(self): ...
    def retro_generated(self): ...
    def knowledge_ingested(self, chunks: int): ...
```

Each method internally calls `cp.post_event()` with the appropriate `type`, `feature_id` (prefixed by phase), and `feature_desc`.

**Event → feature_id mapping (for dashboard display):**

| Event | feature_id | feature_desc |
|-------|-----------|-------------|
| worktree_sweep | `setup-worktree-sweep` | "Setup: Worktree sweep — removed {N}" |
| browser_preflight | `setup-browser-preflight` | "Setup: Browser pre-flight — {result}" |
| knowledge_query | `setup-knowledge-query` | "Setup: Knowledge query — {N} chunks loaded" |
| planner_role_start | `planner-{role}` | "Planning: {Role} — {description}" |
| feature_start | `{NNN}` | unchanged (existing behavior) |
| suspicion_warning | `completion-suspicion` | "Completion: Suspicion check — {N} warnings" |
| retro_generated | `completion-retro` | "Completion: Retrospective generated" |
| knowledge_ingested | `completion-knowledge` | "Completion: Knowledge ingested — {N} chunks" |

All events use existing `feature_start`/`feature_pass`/`feature_fail` event types so they work with the current control plane without TypeScript changes.

**Wire into orchestrator.py:**
- Create `EventTracker(cp)` at the start of `run_harness()`
- Replace all existing `cp.post_event()` calls with `tracker.{method}()` calls
- Add new tracker calls at: worktree sweep, browser pre-flight, knowledge query, suspicion check, retro generation, knowledge ingestion

**Wire into planner_pipeline.py:**
- Accept `EventTracker` as parameter to `run_planner_pipeline()`
- Call `tracker.planner_role_start/pass/fail/retry()` around each role

**Acceptance criteria:**
- `EventTracker` class exists with all methods listed above
- Every `print()` in orchestrator.py and planner_pipeline.py has a corresponding tracker call
- All tracker methods are fire-and-forget (wrapped in try/except)
- Works when control plane is not running (events silently dropped)
- Dashboard run-detail shows setup events → planner events → feature events → completion events in order
- Events use `feature_id` prefix convention: `setup-*`, `planner-*`, `{NNN}`, `completion-*`
- Existing orchestrator tests still pass (tracker calls don't affect control flow)
- New tests verify EventTracker calls the correct cp.post_event with correct arguments

### Feature 9: Epic/Story/Task Data Model

Replace the flat `feature_list.json` array with a hierarchical `work_plan.json`. This is the new output of the Refiner role (replaces `feature_list.json`).

**Create:** `src/core/work_plan.py` — data model, loader, query helpers

```python
# work_plan.json structure
{
  "phases": [
    {
      "id": "phase-0",
      "name": "Contracts & Setup",
      "epics": [
        {
          "id": "epic-001",
          "name": "Project Scaffolding",
          "stories": [
            {
              "id": "story-001",
              "name": "Initialize project structure",
              "tasks": [
                {
                  "id": "task-001",
                  "description": "Create package.json with dependencies",
                  "acceptance_criteria": ["package.json exists", "npm install succeeds"],
                  "steps": ["Write package.json", "Run npm install"],
                  "depends_on": [],
                  "status": "pending",
                  "attempts": 0,
                  "blocked_reason": null
                }
              ]
            }
          ]
        }
      ]
    }
  ]
}
```

**WorkPlan class interface:**

```python
class WorkPlan:
    """Manages the hierarchical work plan."""

    @classmethod
    def load(cls, path: Path) -> "WorkPlan": ...

    def save(self, path: Path): ...  # uses atomic_write

    # Query
    def get_next_task(self) -> Optional[dict]: ...  # first pending task with all deps satisfied
    def get_task(self, task_id: str) -> Optional[dict]: ...
    def get_story(self, story_id: str) -> Optional[dict]: ...
    def get_epic(self, epic_id: str) -> Optional[dict]: ...

    # Status
    def mark_task_done(self, task_id: str): ...
    def mark_task_blocked(self, task_id: str, reason: str): ...
    def increment_task_attempts(self, task_id: str) -> int: ...

    # Counts
    def count_tasks(self) -> dict: ...  # {total, done, blocked, pending}
    def count_stories(self) -> dict: ...
    def count_epics(self) -> dict: ...

    # Phase gates
    def current_phase(self) -> dict: ...  # first phase with incomplete tasks
    def is_phase_complete(self, phase_id: str) -> bool: ...

    # DAG
    def validate_dag(self) -> dict: ...  # check for circular deps, return {valid, cycles}
    def are_deps_satisfied(self, task_id: str) -> bool: ...

    # Backward compat
    @classmethod
    def from_flat_features(cls, features: list) -> "WorkPlan": ...  # wrap flat list in single phase/epic/story
```

**Acceptance criteria:**
- `WorkPlan.load()` loads `work_plan.json` with phases/epics/stories/tasks hierarchy
- `get_next_task()` returns the first pending task whose `depends_on` tasks are ALL done
- `get_next_task()` respects phase gates: no Phase 1 tasks until all Phase 0 tasks are done
- `get_next_task()` returns None when all tasks are done or all remaining are blocked/dep-blocked
- `validate_dag()` detects circular dependencies and returns `{valid: False, cycles: [...]}`
- `mark_task_done()` + `mark_task_blocked()` update status in-place and save atomically
- `count_tasks()` returns correct counts across all phases/epics/stories
- `from_flat_features()` wraps a flat feature list into a single-phase/single-epic/single-story plan (backward compat)
- Tests use real temp files with real JSON

### Feature 10: Orchestrator Hierarchy Integration

Replace the flat feature loop in `orchestrator.py` with the hierarchical work plan.

**Modify:** `src/core/orchestrator.py` — the generator ↔ evaluator loop

```python
# BEFORE: flat feature iteration
next_feature = state_mgr.get_next_feature()

# AFTER: hierarchical task iteration
work_plan = WorkPlan.load(state_dir / "work_plan.json")
dag_check = work_plan.validate_dag()
if not dag_check["valid"]:
    print(f"ERROR: Circular dependencies: {dag_check['cycles']}")
    break
next_task = work_plan.get_next_task()  # respects deps + phase gates
```

**Key changes:**
- Load `WorkPlan` instead of flat feature list
- `get_next_task()` replaces `state_mgr.get_next_feature()`
- `work_plan.mark_task_done()` replaces `state_mgr.mark_feature_passing()`
- `work_plan.mark_task_blocked()` replaces `state_mgr.mark_feature_blocked()`
- `work_plan.count_tasks()` replaces `state_mgr.count_features()`
- Phase transitions logged: "Phase 0 complete. Advancing to Phase 1: Core Features"
- DAG validated once at start of generator phase

**Backward compatibility:**
- If `work_plan.json` doesn't exist but `feature_list.json` does → auto-convert via `WorkPlan.from_flat_features()`
- Existing state.json fields (`current_feature_id` etc.) still updated for compatibility

**Acceptance criteria:**
- Orchestrator loads work_plan.json and iterates tasks in dependency order
- Phase gates enforced: Phase 1 tasks don't start until all Phase 0 tasks are done
- DAG validation runs at start — circular deps cause hard error with clear message
- Flat feature_list.json auto-converts to work_plan (backward compat)
- Phase transitions printed and posted as events
- All existing orchestrator tests still pass (they use flat features → auto-converted)

### Feature 11: Planner Produces Hierarchical Work Plan

Update the planner role prompts to produce Epic/Story/Task hierarchy instead of flat features.

**Modify:** `src/prompts/architect.md` — produces `draft_work_plan.json` (hierarchical) instead of `draft_features.json` (flat)

**Modify:** `src/prompts/refiner.md` — produces `work_plan.json` (final, hierarchical) instead of `feature_list.json` (flat)

**Modify:** `src/prompts/adversary.md` — reviews the hierarchy: "Are epics properly scoped? Do stories have cross-cutting dependencies that should be explicit? Are phase gates in the right places?"

**Modify:** `src/prompts/validator.md` — validates coverage against the hierarchy, checks phase ordering, runs DAG validation

**Update strategy config:**
```yaml
strategies:
  feature:
    planner_roles:
      - name: architect
        artifact: draft_work_plan.json
        validation:
          type: work_plan
          min_tasks: 5
          min_phases: 1
      - name: refiner
        artifact: work_plan.json
        validation:
          type: work_plan
          min_tasks: 5
          min_phases: 1
          require_acceptance_criteria: true
```

**Add validation function:**
```python
def validate_work_plan(path: Path, min_tasks: int, min_phases: int, require_acceptance_criteria: bool = False) -> dict:
    """Validate hierarchical work plan structure."""
```

**Acceptance criteria:**
- Architect produces `draft_work_plan.json` with phases/epics/stories/tasks structure
- Adversary reviews the hierarchy (not just flat features)
- Refiner produces `work_plan.json` addressing all gaps from SPEC_GAPS.md
- Validator checks coverage matrix against epics/stories, validates DAG, checks phase ordering
- Every task has `depends_on`, `status: "pending"`, `attempts: 0`
- At least one phase gate exists (Phase 0 before Phase 1)
- Small projects (< 15 tasks) can have a single phase — hierarchy is not forced when unnecessary

## Critical Constraints

1. **Brownfield rules** — read every file before modifying it. Match existing code style. Do not restructure existing modules.
2. **No new dependencies** — stdlib + existing deps only.
3. **Test-first** — write failing test, implement, verify. Match existing test patterns (pytest, tmp_dir fixtures, unittest.mock for agent sessions).
4. **Reuse existing functions** — `run_agent_session()`, `load_prompt()`, `create_client_options()`, `atomic_write()`. Do NOT reimplement these.
5. **Keep legacy fallback** — `planner.md` and `build_planner_prompt()` stay. If strategy config is missing, old behavior runs.
6. **State compatibility** — old state.json (with `planner_complete` boolean, without `planner_roles`) must still work.
7. **Control plane is optional** — pipeline must work when HARNESS_CONTROL_PLANE_URL is not set.

## Known Failure Modes

- **FM-15: Role context bleed** — each role must only see its specified inputs. The Adversary must NOT see the knowledge chunks or input prompt (only spec.md + draft_features.json). If context leaks, the Adversary loses objectivity.
- **FM-16: Artifact overwrite race** — the Architect writes `draft_work_plan.json`, the Refiner writes `work_plan.json`. They are separate files. Do NOT overwrite one with the other.
- **FM-20: Forced hierarchy on small projects** — a 10-task project doesn't need 3 phases and 5 epics. The hierarchy must be proportional: small projects get 1 phase, 1-2 epics, 2-3 stories. The planner should NOT force deep nesting for simplicity's sake.
- **FM-21: DAG validation at wrong time** — validate the DAG at plan time (after Refiner), not at generator runtime. Circular deps discovered mid-build waste all prior work. The Validator role must run `validate_dag()` and reject if cycles found.
- **FM-22: Phase gate deadlock** — if a task in Phase 0 depends on a task in Phase 1, the phase gate creates a deadlock. Dependencies must only point within the same phase or to earlier phases. The Validator must check this.
- **FM-17: Validation false positive** — `validate_markdown` counting gaps by searching for `### GAP` headings can false-positive on other `###` headings. Use specific patterns: `### GAP-` prefix.
- **FM-18: Model resolution failure** — if config has `planner_model: claude-opus-4-6` and a role says `model: opus`, the resolution must map `"opus"` to the planner_model value, not hardcode a model ID. Read from config at runtime.
- **FM-19: Resume artifact staleness** — when resuming at Refiner, the Adversary's SPEC_GAPS.md on disk must be from the CURRENT run, not a stale file from a previous run. Check file modification time against run start time, or re-read unconditionally (simpler).

## What "Done" Looks Like

- `python3 -m pytest tests/ -v` → all tests pass (268 existing + new)
- Harness run with strategy config → prints 4 role completions: architect, adversary, refiner, validator
- Each role produces its artifact in state_dir
- Artifacts pass schema+quality validation
- `--resume` after crash at any role works correctly
- Harness run without strategy config → legacy planner runs (backward compatible)
- **Dashboard shows the FULL run lifecycle**: setup events (sweep, browser, knowledge) → planner role events (architect/adversary/refiner/validator with PASS/FAIL badges) → feature events → completion events (suspicion, retro, knowledge ingest)
- Every `print()` in the harness output has a corresponding event visible in the dashboard
- Planner produces `work_plan.json` with phases/epics/stories/tasks hierarchy
- Orchestrator iterates tasks in dependency order with phase gates enforced
- Phase transitions visible in dashboard: "Phase 0 complete → Phase 1: Core Features"
- DAG validated at plan time — circular deps caught before generator starts
- Flat `feature_list.json` auto-converts to `work_plan.json` (backward compat)
- Smoke test on a small project produces better, hierarchically-organized plans than the v2.1 flat feature list
