# Long-Running Agent Harness

Production 3-agent harness for autonomous multi-hour builds with parallel fleet execution.

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
