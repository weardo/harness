# Harness Refactor Map — From Rigid Harness to Flexible Product System

**Date:** 2026-04-08  
**Status:** Draft  
**Goal:** Separate Harness into a stable execution kernel plus a flexible product layer

## 1. Why This Refactor Exists

Harness is strong where Linear is weak:

- long-running execution
- planner/generator/evaluator orchestration
- retries, circuit breaking, resumability
- parallel worktrees
- run-level artifacts and telemetry

Harness is weak where Linear is strong:

- shared product context
- flexible intake
- reusable workflows
- workspace-level objects
- human/agent collaboration surfaces
- tool/provider abstraction

Today Harness behaves like a deterministic autonomous build machine.
Linear behaves like a context system that can route work to agents.

The refactor goal is not to replace the Harness engine.
The goal is to stop forcing the entire product to look like the engine.

## 2. Core Diagnosis

The current rigidity comes from five architectural choices:

### 2.1 Input rigidity

Harness begins from:

- `--prompt`
- `--spec`
- `--plan`
- `--resume`

This is execution-friendly but product-hostile.

Missing:

- request objects
- workspace/project objects
- context bundles
- decisions/discussions/feedback as first-class inputs

### 2.2 Planning rigidity

Harness planning assumes:

- one canonical plan shape
- strong validation gates
- mostly immutable task definitions
- sequential or DAG-based execution from a fixed plan

This is reliable for autonomous builds, but too rigid for a collaborative product where people and agents continuously reshape scope.

### 2.3 State rigidity

State is run-local and file-local:

- `state.json`
- `work_plan.json`
- `feature_list.json`
- `feedback.md`
- `spec.md`

That makes crash recovery good, but collaboration awkward.

### 2.4 Provider/runtime rigidity

Harness is effectively a Claude-native engine with Claude-native settings, prompts, hooks, and tool assumptions.

That is fine for the kernel.
It is not fine for the product surface.

### 2.5 Event-model rigidity

The current event model is optimized for dashboard telemetry, not for a rich product domain.

Examples:

- many lifecycle events are squeezed into generic `feature_start/pass/fail`
- there is little distinction between business objects and runtime events
- the dashboard is a consumer of engine output, not the home of the work itself

## 3. The Target Architecture

The new architecture should have three layers:

```
Layer A: Product Layer
  workspace, projects, requests, specs, skills, automations, reviews

Layer B: Execution Orchestration Layer
  context assembly, run creation, runtime selection, event normalization,
  artifact persistence, retry APIs

Layer C: Harness Kernel
  planning pipeline, work DAG, execution loop, evaluator, circuit breaker,
  worktree isolation, progress detection, run state
```

### 3.1 Rule of separation

The product layer should not know about:

- `.harness/runs/...`
- `feature_list.json`
- `.claude_settings.json`
- raw planner role retries

The kernel should not know about:

- workspace members
- request inboxes
- triage automations
- product UI states
- Slack/GitHub UX semantics

The execution orchestration layer is the bridge.

## 4. New Logical Boundaries

## 4.1 Harness Kernel

Responsibility:

- run a scoped execution job reliably

Inputs:

- execution request
- normalized context bundle
- selected runtime profile
- execution policy

Outputs:

- structured runtime events
- artifacts
- run summary
- result status

## 4.2 Product Layer

Responsibility:

- store and operate on product/team objects

Objects:

- Workspace
- Project
- Request
- Spec
- Decision
- Skill
- Automation
- Run
- Review

## 4.3 Adapter Layer

Responsibility:

- translate product intent into engine-compatible execution

Examples:

- request -> context bundle
- spec -> execution request
- skill -> execution template
- runtime events -> product timeline events
- branch/PR output -> review surface

## 5. Proposed Package Layout

Do not do a huge rewrite first.

The clean target could look like this over time:

```
src/
  kernel/
    orchestrator.py
    planner_pipeline.py
    work_plan.py
    parallel.py
    progress.py
    completion.py
    circuit_breaker.py
    cost_tracker.py
    state.py
    worktree.py
    coordination.py
    discovery.py

  adapters/
    runtime/
      claude_runtime.py
      runtime_types.py
    events/
      event_normalizer.py
    storage/
      artifact_store.py
      run_store.py
    integrations/
      github.py
      slack.py
      knowledge.py

  product/
    models/
      workspace.py
      project.py
      request.py
      spec.py
      skill.py
      automation.py
      review.py
    services/
      context_assembler.py
      request_service.py
      run_service.py
      skill_service.py
      automation_service.py
      review_service.py

  interfaces/
    api/
    cli/
```

For the near term, the actual code can remain in current files while we introduce the new abstractions around them.

## 6. Current Module Classification

This is the practical refactor map.

## 6.1 Keep as kernel

These are strong, differentiated, and should remain engine internals.

| Current module | Keep? | Why |
|---|---|---|
| `src/core/orchestrator.py` | Keep, but hide behind service | Core execution brain |
| `src/core/planner_pipeline.py` | Keep | Good reliability scaffold |
| `src/core/work_plan.py` | Keep | Strong execution DAG/phase model |
| `src/core/parallel.py` | Keep | Valuable kernel capability |
| `src/core/coordination.py` | Keep | Useful concurrency primitive |
| `src/core/discovery.py` | Keep | Knowledge relay between workers is a real moat |
| `src/core/progress.py` | Keep | Runtime reliability logic |
| `src/core/completion.py` | Keep | Exit-gating logic belongs in kernel |
| `src/core/circuit_breaker.py` | Keep | Kernel safety |
| `src/core/cost_tracker.py` | Keep | Kernel accounting |
| `src/core/worktree.py` | Keep | Low-level execution primitive |
| `src/core/conflict_resolver.py` | Keep | Merge/runtime primitive |

Rule:

Do not expose these directly to product clients.

## 6.2 Wrap behind adapters

These are useful, but too tied to current implementation details.

| Current module | Action | Why |
|---|---|---|
| `src/core/client.py` | Wrap, then split | Too Claude-specific |
| `src/core/control_plane.py` | Wrap | Useful transport, not product model |
| `src/core/event_tracker.py` | Wrap and normalize | Event vocabulary too engine-shaped |
| `src/core/knowledge_client.py` | Wrap | Good idea, wrong layer |
| `src/run.py` | Keep only as CLI adapter | Should stop being the main system interface |

### Desired replacements

- `client.py` -> `RuntimeAdapter` interface
- `control_plane.py` -> `RunEventSink` adapter
- `event_tracker.py` -> `KernelEventPublisher` plus `ProductEventNormalizer`
- `run.py` -> one interface among many, not the default architecture

## 6.3 Split apart

These mix concerns and need decomposition.

| Current module | Action | Why |
|---|---|---|
| `src/core/state.py` | Split into kernel state vs run metadata store | Combines crash-safe engine state and top-level run index |
| `src/core/orchestrator.py` | Split over time | Contains runtime policy, execution flow, eventing, and output shaping |
| `src/core/planner_pipeline.py` | Split validation helpers out | Validation policy should be pluggable |

### Specific split for `state.py`

Split into:

- `kernel_state_store.py`
  - run-local atomic files
  - crash-safe internal execution state
- `run_registry.py`
  - product-facing run metadata
  - eventually backed by product DB instead of local JSON

### Specific split for `orchestrator.py`

Extract:

- `ExecutionCoordinator`
- `PlannerExecutor`
- `TaskExecutor`
- `EvaluationExecutor`
- `RunResultAssembler`

The current file can remain as a facade during migration.

## 6.4 Replace with product-layer services

These do not exist yet and must be added to make Harness flexible like Linear.

| New service | Purpose |
|---|---|
| `RequestService` | store and manage incoming work |
| `ContextAssembler` | build explicit context bundles for humans and agents |
| `RunService` | create runs from requests/specs/skills |
| `SkillService` | save, version, and apply reusable workflows |
| `AutomationService` | trigger workflows from product events |
| `ReviewService` | human review, retry, accept, reject |
| `IntegrationService` | GitHub, Slack, docs, repo metadata |
| `ArtifactService` | make artifacts product-visible and queryable |

## 7. Interfaces We Need First

This is the most important part of the refactor.

Do not start by moving files around.
Start by creating stable interfaces.

## 7.1 `ExecutionRequest`

New canonical input to the kernel:

```python
class ExecutionRequest(TypedDict):
    request_id: str
    project_id: str
    source_type: str           # request | spec | plan | manual
    objective: str
    context_bundle: dict
    runtime_profile: str
    execution_policy: dict
```

This replaces the kernel thinking primarily in terms of:

- raw prompt string
- spec path
- plan path

## 7.2 `KernelRunResult`

New canonical output from the kernel:

```python
class KernelRunResult(TypedDict):
    run_id: str
    status: str
    summary: str
    cost_usd: float
    duration_seconds: int
    artifacts: list[dict]
    branch_name: str | None
    pr_url: str | None
```

## 7.3 `KernelEvent`

New structured event format:

```python
class KernelEvent(TypedDict):
    run_id: str
    ts: str
    phase: str
    kind: str
    task_id: str | None
    message: str
    metadata: dict
```

This should exist before any product UI redesign.

## 7.4 `RuntimeAdapter`

New interface:

```python
class RuntimeAdapter(Protocol):
    def build_session_options(self, request: ExecutionRequest) -> dict: ...
    async def run_session(self, role: str, prompt: str, options: dict) -> dict: ...
```

Initial implementation:

- `ClaudeRuntimeAdapter`

Future implementations:

- `CodexRuntimeAdapter`
- `CursorLaunchAdapter`
- `ExternalAgentLaunchAdapter`

Even if only Claude is supported for now, this interface is critical.

## 8. Product-Layer Data Model We Need First

To become Linear-like in flexibility, the product needs first-class objects.

Minimum set:

### Workspace

- teams and settings

### Project

- repo target
- runtime defaults

### Request

- title
- description
- source
- status
- acceptance criteria
- linked context

### Spec

- draft/generated/approved versions

### Skill

- reusable prompt/workflow template

### Automation

- event trigger
- action

### Run

- execution attempt against a request/spec

### Review

- accept/retry/reject with human notes

Without these, Harness will keep feeling like a CLI with a dashboard.

## 9. How This Maps To Linear's Shape

Linear's current shape can be simplified to:

- Intake
- Plan
- Build
- Diffs
- Monitor
- Agents
- Skills
- Automations

Harness today only really owns:

- Build
- some Monitor

This refactor should create the missing map:

| Linear-shaped capability | Harness future owner |
|---|---|
| Intake | Product layer (`RequestService`) |
| Plan | Product layer + kernel planner |
| Build | Kernel |
| Diffs | Product review layer + Git integration |
| Monitor | Event normalization + run console |
| Agents | Runtime adapters + command surface |
| Skills | Skill service |
| Automations | Automation service |

## 10. First Migration Sequence

This is the safest path.

## Phase 1 — Add interfaces without breaking current CLI

Build:

- `ExecutionRequest`
- `KernelRunResult`
- `KernelEvent`
- `RuntimeAdapter`

Do not change behavior yet.
Just adapt current internals to emit and consume these types.

## Phase 2 — Introduce `RunService`

Build:

- product-facing service that turns requests/specs into `ExecutionRequest`
- wrapper around current `run_harness(...)`

Outcome:

- future UI can call `RunService`
- CLI can also call `RunService`

## Phase 3 — Normalize events

Build:

- `ProductEventNormalizer`
- richer event schema
- mapping from current `feature_*` events to `KernelEvent`

Outcome:

- run console can be useful
- dashboard stops depending on vague engine strings

## Phase 4 — Split run metadata from engine state

Build:

- product run metadata store
- keep current atomic local state for crash safety

Outcome:

- local reliability preserved
- shared product view becomes possible

## Phase 5 — Add request/context/spec objects

Build:

- request model
- context bundle assembler
- spec revisions

Outcome:

- engine no longer begins from a naked prompt

## Phase 6 — Add skills and one automation

Build:

- reusable workflow templates
- triage automation

Outcome:

- flexibility moves from prompt text into product primitives

## 11. The First Actual Code Changes I Would Make

If we start implementation, the first concrete code tasks should be:

1. Create `src/interfaces/execution_types.py`
   - `ExecutionRequest`
   - `KernelRunResult`
   - `KernelEvent`

2. Create `src/adapters/runtime/claude_runtime.py`
   - move current Claude-specific logic from `client.py` behind an adapter

3. Create `src/product/services/run_service.py`
   - one entrypoint that wraps current `run_harness`

4. Add event normalization
   - preserve current event tracker
   - add a normalized event stream for product consumers

5. Split `RunRegistry` out of `state.py`
   - keep atomic engine state local
   - prepare for DB-backed run metadata

These changes improve architecture immediately without requiring a rewrite.

## 12. What We Must Not Do

Do not:

- rewrite the orchestrator from scratch
- move every file into new folders first
- replace local crash-safe state with DB state immediately
- build a full UI before defining execution interfaces
- add many runtime providers before the adapter boundary exists

That would create churn without flexibility.

## 13. What "Flexible Harness" Should Mean

A flexible future Harness should let us:

- launch work from a request, not just a prompt
- attach different context bundles to the same kernel
- support multiple agent runtimes through adapters
- re-plan/retry at the product layer without mutating raw engine files manually
- save reusable workflows as skills
- trigger automations from intake events
- expose clean run/review surfaces for humans

The kernel can remain opinionated.
The product around it must become adaptable.

## 14. Final Recommendation

Treat current Harness as:

**an execution kernel with strong reliability properties**

not as:

**the finished architecture of the future product**

The right refactor is:

1. preserve the kernel
2. add stable interfaces
3. build adapters around current Claude/control-plane assumptions
4. introduce product objects
5. move flexibility into context, skills, automations, and reviews

That is the shortest path from "rigid autonomous builder" to "Linear-like context-to-execution product."
