# Long-Running Agent Harness — Build Tasks

Spec: `docs/superpowers/specs/2026-03-27-long-running-harness-design.md`

## Task List

```json
[
  {
    "id": "001",
    "priority": 1,
    "category": "setup",
    "description": "Create project scaffolding: requirements.txt, __init__.py files, config.yaml template, .gitignore for state/",
    "steps": [
      "Create harness-dev/src/requirements.txt with claude-code-sdk>=0.0.25",
      "Create __init__.py in src/, src/core/, src/hooks/",
      "Create src/config.yaml with all defaults from spec Section 13",
      "Create harness-dev/.gitignore ignoring state/, __pycache__, .cache/",
      "Verify: python -c 'import yaml; yaml.safe_load(open(\"src/config.yaml\"))'"
    ],
    "passes": true
  },
  {
    "id": "002",
    "priority": 2,
    "category": "core",
    "description": "Build state.py \u2014 atomic JSON state management with flush+fsync+replace pattern",
    "steps": [
      "Implement atomic_write(path, data) using tempfile.mkstemp + flush + fsync + os.replace",
      "Implement atomic_read(path) with file-not-found handling",
      "Implement StateManager class managing state.json, circuit_breaker.json, exit_signals.json",
      "Write tests/test_state.py: test atomic write survives, test read-after-write, test concurrent safety",
      "Verify: python -m pytest tests/test_state.py passes"
    ],
    "passes": true
  },
  {
    "id": "003",
    "priority": 3,
    "category": "core",
    "description": "Build circuit_breaker.py \u2014 CLOSED/HALF_OPEN/OPEN state machine with multi-source progress detection",
    "steps": [
      "Implement CircuitBreaker class with state machine: CLOSED, HALF_OPEN, OPEN",
      "Implement transition logic: no_progress threshold, same_error threshold, output_decline",
      "Implement HALF_OPEN recovery probe: 1 iteration, progress -> CLOSED, no progress -> OPEN",
      "Implement OPEN cooldown: after cooldown_seconds -> HALF_OPEN, max_open_count -> PERMANENT_HALT",
      "Implement record_iteration(progress, error_hash, output_length) method",
      "Implement transition history logging",
      "Uses state.py atomic_write for persistence",
      "Write tests/test_circuit_breaker.py: test all transitions, test cooldown, test permanent halt",
      "Verify: python -m pytest tests/test_circuit_breaker.py passes"
    ],
    "passes": true
  },
  {
    "id": "004",
    "priority": 4,
    "category": "core",
    "description": "Build security.py \u2014 bash command allowlist + command substitution blocking + PreToolUse hook",
    "steps": [
      "Implement ALLOWED_COMMANDS set with defaults from spec",
      "Implement extract_commands(command_string) using shlex parsing",
      "Implement validate_command(command) checking allowlist + extra validation for pkill/chmod",
      "Implement block_command_substitution: reject $(...), backticks, subshells, null bytes, >10KB",
      "Implement async bash_security_hook(input_data, tool_use_id, context) returning SDK hook format",
      "Write tests/test_security.py: test allowed commands pass, blocked commands fail, substitution blocked",
      "Verify: python -m pytest tests/test_security.py passes"
    ],
    "passes": true
  },
  {
    "id": "005",
    "priority": 5,
    "category": "core",
    "description": "Build progress.py \u2014 multi-source progress detection (git + status block + output trends)",
    "steps": [
      "Implement parse_status_block(output_text) extracting ---HARNESS_STATUS--- fields",
      "Implement detect_git_progress(project_dir) checking git diff --stat for meaningful changes",
      "Implement detect_feature_progress(feature_list_path) comparing passes counts",
      "Implement detect_output_decline(current_length, peak_length, threshold) for output trend",
      "Implement assess_progress(output, project_dir, feature_list_path, prev_peak) returning ProgressResult",
      "Write tests/test_progress.py: test status block parsing, test git detection, test feature counting",
      "Verify: python -m pytest tests/test_progress.py passes"
    ],
    "passes": true
  },
  {
    "id": "006",
    "priority": 6,
    "category": "core",
    "description": "Build completion.py \u2014 shared completion logic for both Mode A and Mode B",
    "steps": [
      "Implement check_completion(harness_dir) returning {complete, reason, progress}",
      "Checks: run test suite command, check feature_list.json all passes/blocked, check rolling window",
      "Implement update_exit_signals(signals_path, iteration, signal_type) with rolling window of 5",
      "Implement is_dual_condition_met(signals) requiring >= 2 completion signals in last 5",
      "Write tests/test_completion.py: test all-passing detection, test rolling window, test dual-condition",
      "Verify: python -m pytest tests/test_completion.py passes"
    ],
    "passes": true
  },
  {
    "id": "007",
    "priority": 7,
    "category": "core",
    "description": "Build cost_tracker.py \u2014 per-agent cost accumulation from ResultMessage",
    "steps": [
      "Implement CostTracker class with per-agent breakdown (planner, generator, evaluator)",
      "Implement record_cost(agent_type, cost_usd, usage_dict) method",
      "Implement check_budget(max_cost_usd) returning bool",
      "Implement format_summary() returning formatted cost breakdown string",
      "Implement persistence to state.json cost fields",
      "Write tests/test_cost_tracker.py: test accumulation, test budget check, test summary format",
      "Verify: python -m pytest tests/test_cost_tracker.py passes"
    ],
    "passes": true
  },
  {
    "id": "008",
    "priority": 8,
    "category": "core",
    "description": "Build client.py \u2014 ClaudeSDKClient factory with security config, MCP, sandbox",
    "steps": [
      "Implement create_client(project_dir, config) returning configured ClaudeSDKClient",
      "Configure: model, system_prompt, allowed_tools, mcp_servers, hooks, max_turns, cwd, sandbox",
      "Implement write_security_settings(project_dir, config) creating .claude_settings.json",
      "Support Playwright MCP and Puppeteer MCP based on config.evaluator.browser_tool",
      "Register bash_security_hook as PreToolUse hook",
      "Write tests/test_client.py: test settings file generation, test tool list assembly",
      "Verify: python -m pytest tests/test_client.py passes"
    ],
    "passes": true
  },
  {
    "id": "009",
    "priority": 9,
    "category": "prompts",
    "description": "Write all 3 prompt templates: planner.md, generator.md, evaluator.md",
    "steps": [
      "Write src/prompts/planner.md per spec Section 3: role, input modes, output format, feature_list schema",
      "Write src/prompts/generator.md per spec Section 4: 8-step startup, status block, recovery STEP 0",
      "Write src/prompts/evaluator.md per spec Section 5: dual-gate, skepticism rules, feedback.md format",
      "Include conditional template blocks: {{#IF_WEB_PROJECT}}...{{/IF_WEB_PROJECT}} for browser verification",
      "Verify: all 3 files exist and contain the mandatory sections from spec"
    ],
    "passes": true
  },
  {
    "id": "010",
    "priority": 10,
    "category": "core",
    "description": "Build orchestrator.py \u2014 the 3-agent loop with planner -> generator <-> evaluator cycle",
    "steps": [
      "Implement run_harness(config, prompt/spec/plan, project_dir) as main entry point",
      "Implement planner phase: create client, send planner prompt, collect spec.md + feature_list.json",
      "Implement generator loop: for each feature, create fresh client, send generator prompt, collect result",
      "Implement evaluator phase: create client, send evaluator prompt, parse PASS/FAIL, write feedback.md",
      "Implement retry logic: on FAIL, re-run generator with feedback, max_retries_per_feature then blocked",
      "Implement exit gate: check budget, time, iterations, circuit breaker, completion dual-condition",
      "Implement resume: read state.json, determine phase, continue from last checkpoint",
      "Wire in: circuit_breaker, cost_tracker, progress, completion at each iteration",
      "Write tests/test_orchestrator.py: test phase transitions, test exit conditions, test resume logic",
      "Verify: python -m pytest tests/test_orchestrator.py passes"
    ],
    "passes": true
  },
  {
    "id": "011",
    "priority": 11,
    "category": "entry",
    "description": "Build run.py \u2014 CLI entry point with argparse for all modes",
    "steps": [
      "Implement argparse: --prompt, --spec, --plan, --resume, --max-cost, --max-duration, --max-iterations, --model, --project-dir",
      "Load config.yaml from project .harness/ directory",
      "Override config with CLI flags where provided",
      "Call orchestrator.run_harness() with resolved config",
      "Print final summary on completion or exit",
      "Verify: python src/run.py --help shows all flags correctly"
    ],
    "passes": true
  },
  {
    "id": "012",
    "priority": 12,
    "category": "hook",
    "description": "Build stop_completion.py \u2014 Mode B Stop hook for Claude Code sessions",
    "steps": [
      "Implement hook script reading JSON from stdin (session_id, stop_hook_active, transcript_path)",
      "Check stop_hook_active to prevent infinite loop",
      "Import and call completion.check_completion()",
      "Import and check circuit_breaker state",
      "Output {decision: block, reason: ...} or exit 0",
      "Verify: echo test JSON | python src/hooks/stop_completion.py handles both complete and incomplete cases"
    ],
    "passes": true
  },
  {
    "id": "013",
    "priority": 13,
    "category": "integration",
    "description": "Create test-project-1: simple counter app \u2014 test full harness end-to-end with --prompt mode",
    "steps": [
      "Create harness-dev/test-projects/counter-app/ directory",
      "Install harness into test-projects/counter-app/.harness/ (copy src/)",
      "Run: python .harness/run.py --prompt 'Build a simple HTML counter with increment/decrement buttons' --max-iterations 5 --max-cost 10",
      "Verify: planner creates spec.md and feature_list.json",
      "Verify: generator implements at least 1 feature",
      "Verify: evaluator runs and produces PASS/FAIL verdict",
      "Verify: state.json tracks progress correctly",
      "Verify: cost summary prints on exit"
    ],
    "passes": true
  },
  {
    "id": "014",
    "priority": 14,
    "category": "integration",
    "description": "Create test-project-2: test --spec mode with existing spec and test adding features to test-project-1",
    "steps": [
      "Write a small spec.md for a todo app (5 features)",
      "Create harness-dev/test-projects/todo-app/ with the harness installed",
      "Run: python .harness/run.py --spec spec.md --max-iterations 5 --max-cost 10",
      "Verify: planner parses spec and generates feature_list.json (skips full expansion)",
      "Go back to counter-app and run --resume or --spec with new features added",
      "Verify: harness picks up where it left off, works on new features"
    ],
    "passes": true
  },
  {
    "id": "015",
    "priority": 15,
    "category": "integration",
    "description": "Test Mode B \u2014 Stop hook with a normal Claude Code session",
    "steps": [
      "Install harness into test-projects/hook-test/",
      "Register Stop hook in .claude/settings.json",
      "Create a simple feature_list.json with 2 features (passes: false)",
      "Verify: stop_completion.py blocks exit when features incomplete",
      "Verify: stop_completion.py allows exit when all features pass",
      "Verify: circuit breaker integration prevents infinite forced-continuation"
    ],
    "passes": true
  },
  {
    "id": "016",
    "priority": 16,
    "category": "phase2",
    "description": "Build parallel.py \u2014 Phase 2 agent teams with worktree isolation",
    "steps": [
      "Implement group_by_dependency(features) returning dependency layers",
      "Implement create_worker_worktree(project_dir, worker_id) using git worktree add",
      "Implement run_parallel_workers(features, project_dir, config) using asyncio.gather",
      "Implement merge_worktree(project_dir, branch, worktree) with conflict detection",
      "Implement cleanup_worktree(project_dir, worktree, branch)",
      "Wire into orchestrator: if config.parallel.enabled, use run_parallel_workers instead of sequential",
      "Write tests/test_parallel.py: test dependency grouping, test worktree creation/cleanup",
      "Verify: python -m pytest tests/test_parallel.py passes"
    ],
    "passes": true
  },
  {
    "id": "017",
    "priority": 17,
    "category": "phase2",
    "description": "Test parallel agent teams end-to-end",
    "steps": [
      "Create test-projects/parallel-test/ with harness + parallel.enabled: true",
      "Create a feature_list with 4 independent features (depends_on: [])",
      "Run with max_workers: 2",
      "Verify: 2 worktrees created simultaneously",
      "Verify: features built in parallel",
      "Verify: worktrees merged back cleanly",
      "Verify: evaluator runs on merged result"
    ],
    "passes": true
  },
  {
    "id": "018",
    "priority": 18,
    "category": "package",
    "description": "Package as library artifact for vault and bootstrap integration",
    "steps": [
      "Copy final src/ to vault/library/ai-development/long-running-harness/src/",
      "Update MANIFEST.md with new artifact entries",
      "Update pattern.md to reference actual src/ files instead of documentation-only",
      "Update bootstrap-project skills-catalog.md if needed",
      "Verify: /promote long-running-harness would install correctly"
    ],
    "passes": true
  }
]
```
