# Long-Running Agent Harness

Production 3-agent harness for autonomous multi-hour builds with parallel fleet execution.

## Why this exists

I'm a solo developer building seekora.ai — a multi-tenant SaaS across 11 repos in Kubernetes (Go + TypeScript + Temporal + a stack of 7 datastores). Long-running build tasks — multi-file refactors, cross-repo features, end-to-end specs — fail in three ways with off-the-shelf AI tooling:

1. **Context loss mid-run.** The model forgets earlier decisions partway through a multi-hour build.
2. **Confidently wrong output.** It generates code that runs but is incorrect, and no one catches it.
3. **Session loss on crash.** Laptop sleeps, network drops, terminal closes — and progress evaporates.

The harness is a production answer to each. Planning is separated from generation, generation from evaluation, and any wave of work resumes cleanly from any crash point. Cursor agent mode, Devin, and Cognition each fit one slice — none fit the specific shape of multi-hour autonomous builds I actually run on my own SaaS.

## Architecture

```
Planner (Opus, multi-phase pipeline)
  → Architect → Adversary → Refiner → Validator
  → work_plan.json (Phase → Epic → Story → Task)

Generator (Sonnet, N sessions) ↔ Evaluator (Sonnet)
  → Sequential: one feature at a time
  → Parallel: wave-based fleet (Citadel-inspired)
    → scope claims, discovery relay, worktree isolation

Circuit Breaker (every cycle)
  → CLOSED/HALF_OPEN/OPEN stagnation detection

Control Plane (optional, localhost:7842)
  → Live tracking, wave events, work plan visualization
  → Offline buffering when control plane is down
```

## Install into a Project

```bash
# Install into current directory
bash ~/harness/install.sh

# Install into a specific project
bash ~/harness/install.sh /path/to/project

# Overwrite existing installation
bash ~/harness/install.sh /path/to/project --force
```

This copies the harness into `.harness/` inside your project.

## Usage

```bash
cd /path/to/project

# Build from a prompt (planner generates spec + plan automatically)
python3 .harness/run.py --prompt "Build a REST API with JWT auth and rate limiting"

# Build from an existing spec
python3 .harness/run.py --spec docs/specs/my-feature.md

# Build from an existing plan
python3 .harness/run.py --plan docs/plans/my-plan.md

# Resume after crash or interruption
python3 .harness/run.py --resume

# With cost/time/iteration limits
python3 .harness/run.py --prompt "..." --max-cost 50 --max-duration 120 --max-iterations 20

# Override model
python3 .harness/run.py --prompt "..." --model claude-sonnet-4-6
```

## Configuration

Edit `.harness/config.yaml` after install:

```yaml
# Models
model: "claude-sonnet-4-6"           # generator + evaluator
planner_model: "claude-opus-4-6"     # planner (Opus recommended)

# Limits
max_cost_usd: 100.0
max_duration_minutes: 240
max_iterations: 50

# Parallel fleet mode (opt-in)
parallel:
  enabled: false          # flip to true to activate
  max_workers: 3          # max concurrent generators per wave
  agent_timeout_minutes: 30
  discovery_relay: true   # share findings between waves
  merge_strategy: requeue # requeue conflicting features

# Evaluator
evaluator:
  test_suite_command: "npm test"    # your test command
  browser_verification: auto        # auto | never
```

## Control Plane (Optional)

Start the control plane for live tracking:

```bash
cd ~/harness-control-plane
npm start    # runs on localhost:7842
```

The harness auto-detects it. If it's down, events buffer locally and drain when it comes back.

```bash
# Set explicitly via env var
HARNESS_CONTROL_PLANE_URL=http://localhost:7842 python3 .harness/run.py --prompt "..."
```

## Parallel Fleet Mode

When `parallel.enabled: true`, independent features run simultaneously in isolated git worktrees:

```
Wave 1: feat-001 + feat-002 + feat-003 → run in parallel worktrees
  ← collect results, compress discovery briefs
  ← merge branches (requeue on conflict)
Wave 2: feat-004 (depends on 001,002) → informed by Wave 1 discoveries
  ← collect, merge
```

Requirements:
- Tasks must have a `scope` field (planner populates this automatically)
- Git must be initialized in the project
- Features in the same wave must not touch the same files

## How It Works

1. **Planner** (1 session, Opus) — reads your prompt/spec, produces a hierarchical work plan
2. **Generator** (N sessions, Sonnet) — implements one feature per session
3. **Evaluator** (N sessions, Sonnet) — grades each feature against acceptance criteria
4. **Circuit Breaker** — detects stagnation, prevents infinite loops
5. **Resume** — crash-safe; `--resume` picks up where it left off

## A concrete build (April 2026)

Real plans shipped through the harness from `docs/plans/`:

- `harness-kernel-streamlining-plan.md` — refactoring the orchestrator's core loop
- `token-efficiency-t0-t2.md` — three-tier token-budget optimization across the planner pipeline
- `submodule-merge-conflict-resolution.md` — automated cross-repo merge logic
- `harness-linear-equivalent-prototype-plan.md` — Linear-style work-tracking surface
- `harness-product-refactor-map.md` — restructuring around the product abstraction
- `spec-fidelity-kernel.md` — guarantees that generated code stays aligned with the spec

For each: the Planner reads the source spec, produces a hierarchical work plan (Phase → Epic → Story → Task), the Generator runs across N worktrees in parallel, the Evaluator grades each feature against acceptance criteria before allowing merge to the integration branch. Adversary + Refiner + Validator passes catch architectural drift and ensure spec fidelity.

## Failure modes handled in production

Each of these has hit during real builds and shaped how the harness is structured:

- **Stagnation.** Generator gets stuck rewriting the same file or re-running the same failing test. Circuit Breaker (CLOSED → HALF_OPEN → OPEN) detects this and forces a re-plan instead of letting the loop run unbounded. → `src/core/`
- **Crash recovery.** Laptop sleep, network drop, OOM, accidental terminal close. State persists per-cycle; `--resume` picks up cleanly without redoing finished work. → `specs/graceful-resume-design.md`
- **Cost overruns.** Runaway loops would otherwise burn the entire budget. Hard `max_cost_usd` limit; fleet halts the moment it's hit, with state preserved for resume after budget top-up. → `src/config.yaml`
- **Worktree corruption.** Parallel sessions trying to touch the same files. Scope claims + per-worktree isolation make conflicts impossible at the filesystem level; merge-conflict requeue handles them at the branch level. → `src/core/coordination`
- **Generator/Evaluator disagreement.** Generator says "done", Evaluator says "missing X". Re-queue with updated requirements; Circuit Breaker prevents oscillation by escalating to re-plan after N rounds.
- **Discovery blackholes.** Parallel agents discovering conflicting facts about the codebase. Discovery relay shares findings between waves so Wave 2 starts with Wave 1's compressed knowledge instead of rediscovering. → `src/core/discovery`
- **Spec drift.** Generator's interpretation diverges from the original spec over a long run. Spec-fidelity kernel re-anchors against the original spec at validator gates.

## Development

```bash
cd ~/harness

# Run all tests (539)
python3 -m pytest tests/ -v

# Run specific module tests
python3 -m pytest tests/test_coordination.py -v
python3 -m pytest tests/test_parallel_integration.py -v
```

## Project Structure

```
~/harness/
  src/
    core/           — orchestrator, state, circuit breaker, parallel, coordination, discovery
    prompts/        — planner, generator, evaluator, architect, adversary, refiner, validator
    templates/      — retrospective templates
    hooks/          — stop-completion hook (Mode B)
    config.yaml     — default configuration
    run.py          — CLI entry point
  tests/            — 539 tests
  test-projects/    — sample projects (smoke-test, hook-test)
  install.sh        — project installer
```
