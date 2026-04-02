# Long-Running Agent Harness

Production 3-agent harness for autonomous multi-hour builds.

## Architecture

```
Planner (Opus, 1 session)
  → spec.md + feature_list.json + init.sh

Generator (Sonnet, N sessions) ↔ Evaluator (Sonnet)
  → Implement features → Grade against criteria → Retry or pass

Circuit Breaker (every cycle)
  → CLOSED/HALF_OPEN/OPEN stagnation detection
```

## Quick Start

```bash
# Install into a project
bash vault/library/ai-development/long-running-harness/install.sh /path/to/project

# Mode A: Autonomous build
cd /path/to/project
python .harness/run.py --prompt "Build a todo app with auth" --max-cost 50

# Mode B: Enhanced Claude Code session (add Stop hook to settings.json)
# Then use Claude Code normally — it can't quit until tests pass
```

## Development

```bash
cd harness-dev

# Run tests
python -m pytest tests/ -v

# Run specific test
python -m pytest tests/test_circuit_breaker.py -v
```

## Test Results

160 tests passing across 7 test files:
- `test_state.py` (20) — atomic JSON, StateManager
- `test_circuit_breaker.py` (23) — state machine transitions
- `test_security.py` (36) — bash allowlist, injection blocking
- `test_progress.py` (22) — status block parsing, multi-source detection
- `test_completion.py` (13) — exit conditions, rolling window
- `test_cost_tracker.py` (10) — per-agent cost tracking
- `test_client.py` (14) — SDK client factory, settings generation
- `test_orchestrator.py` (11) — prompt loading, conditionals
- `test_parallel.py` (11) — dependency grouping, git worktrees

## Integration Testing

Requires a standalone terminal (no active Claude Code session competing for API quota):

```bash
cd harness-dev/test-projects/counter-app
python .harness/run.py --prompt "Build a simple HTML counter" --max-iterations 3 --max-cost 5
```
