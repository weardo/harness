# Harness Planning Strategies

The Planner → Generator ↔ Evaluator loop is the invariant structure.
**Strategies are the variant behavior** — each one is just different prompt templates + validation rules. No orchestrator code changes required.

## Architecture

Each strategy changes exactly 3 things:
1. **Planner prompts** — different roles, different analysis, different artifact format
2. **Evaluator criteria** — what counts as "passing" differs per strategy
3. **Generator context** — extra input fed to the generator (error log, existing code, etc.)

All strategies share the same multi-phase planner pipeline:
```
Architect → Adversary → Refiner → Validator
```
The adversary's attack angle shifts per strategy:
- `feature`: "what will the generator build wrong?"
- `bugfix`: "what other code paths does this fix break?"
- `refactor`: "what behavior change might slip through unnoticed?"

## Strategies

| Strategy | Input | Planner Produces | Evaluator Checks | Example |
|----------|-------|-----------------|------------------|---------|
| **feature** | Spec / prompt | Feature list with acceptance criteria | Tests pass, criteria met | "Build a dashboard" |
| **bugfix** | Bug report / failing test / error log | Investigation steps → root cause → fix plan → regression test | Test fails before fix, passes after | "Fix: SSE connections leak on disconnect" |
| **test** | Existing codebase | Coverage analysis → test features targeting uncovered paths | Tests exist, pass, exercise target code | "Add tests for orchestrator.py" |
| **refactor** | Existing code + goal | Incremental steps preserving behavior | All existing tests pass after each step | "Split orchestrator.py into smaller modules" |
| **upgrade** | Dependency + target version | Migration checklist, breaking change guards, compat tests | Old tests pass on new version, deprecated APIs replaced | "Upgrade Express 4 → 5" |
| **security** | Existing codebase | OWASP/threat model scan → hardening features | Vuln tests fail before fix, pass after | "Security audit the REST API" |
| **performance** | Existing code + bottleneck | Profile → identify hotspots → optimization features | Benchmark before/after, behavior unchanged | "Optimize knowledge search latency" |

## Implementation in config.yaml

Each strategy is an entry under `strategies:` with its own `planner_roles` list. The orchestrator picks the strategy from `--strategy` flag or `default_strategy`.

```yaml
strategies:
  feature:
    planner_roles: [architect, adversary, refiner, validator]

  bugfix:
    planner_roles: [investigator, adversary, fixer, verifier]

  test:
    planner_roles: [coverage-analyst, adversary, test-planner, validator]

  refactor:
    planner_roles: [decomposer, adversary, sequencer, validator]

default_strategy: feature
```

## Implementation Priority

1. **`feature`** — already exists, upgrade to full multi-phase pipeline
2. **`bugfix`** — highest practical value, next to implement
3. **`test`** — every project needs more tests
4. `refactor`, `upgrade`, `security`, `performance` — follow same pattern, add later

## Bugfix Strategy Detail

The bugfix planner needs the error artifact as input. Proposed flow:

```
harness --strategy bugfix --input "error.log or failing test path"
```

Planner roles:
- **Investigator**: reads error/test, traces root cause, produces `investigation.json`
- **Adversary**: challenges the root cause — "is this really the cause or a symptom?"
- **Fixer**: produces fix plan as feature list (each feature = one atomic fix step)
- **Verifier**: writes regression test spec that must fail before fix, pass after

Evaluator criterion unique to bugfix: **the regression test must be run twice** — once against the pre-fix code (must fail) and once after (must pass). This is enforced before marking any feature as passing.
