# Harness Kernel Streamlining Plan

**Date:** 2026-04-08  
**Status:** Draft  
**Goal:** Replace the current planner-first monolith with an intent-driven execution kernel that can handle variable request types without forcing everything through the same full-build flow.

## 1. Why This Plan Exists

The recent product prototype work proved something important:

- the new product layer can create requests, assemble context, launch runs, and surface artifacts
- the product API and UI are no longer the main problem
- the kernel is still too rigid for product-shaped work

A tiny brownfield request like:

> "Show the total number of saved requests in the hero area"

currently becomes:

- full product-architecture planning
- full `spec.md` generation
- full `draft_work_plan.json` / `work_plan.json` contract
- full planner pipeline validation

That is not a UI bug. It is the kernel's default worldview.

The current kernel assumes:

- every request is a build
- every build needs planning first
- every plan should expand into a large hierarchical task graph
- every execution path converges on the same planner -> generator -> evaluator loop

That was a reasonable architecture for autonomous greenfield builds.
It is the wrong architecture for a product system handling:

- small brownfield changes
- bug fixes
- scoped refactors
- review-only requests
- partial replans
- execution from existing specs or plans

This plan is explicitly **not** a patchwork plan.
It is a kernel-streamlining plan.

## 2. Core Diagnosis

The rigidity is structural, not incidental.

### 2.1 Single monolithic entry flow

[`run_harness`](../../src/core/orchestrator.py) owns:

- run setup
- signal handling
- planner selection
- planner execution
- planner validation
- work plan loading / conversion
- sequential execution
- parallel execution
- evaluator retries
- completion handling
- run finalization

This makes it difficult to choose different execution paths without adding more conditionals into one giant function.

### 2.2 Planner-first assumption

Today the kernel effectively assumes:

```text
request -> planner -> work_plan -> generator/evaluator
```

That means the planner is not just one possible stage.
It is the gatekeeper of all work.

### 2.3 Planning contract assumes greenfield scope

The planner pipeline and strategy config in [`src/config.yaml`](../../src/config.yaml) assume large task decomposition with:

- minimum tasks
- phase hierarchy
- acceptance criteria on every task
- work-plan validation as the source of truth

This is excellent for long autonomous builds.
It is excessive for tiny changes and hostile to flexible product requests.

### 2.4 State model is phase-rigid

Current state revolves around:

- `phase = init | generator | complete | drained`
- `planner_complete`
- `feature_list.json`
- `work_plan.json`

That makes state recovery reliable, but it bakes in the assumption that all runs are planner-oriented task-graph executions.

### 2.5 Product intent is discarded before execution

The product layer currently maps requests into a generic execution request in [`src/product/services/run_service.py`](../../src/product/services/run_service.py).

But after that, the kernel still mostly sees:

- `prompt`
- maybe `spec_path`
- maybe `plan_path`
- resume flag

Important product facts are lost as first-class kernel inputs:

- request kind
- scope size
- linked files
- desired planning depth
- expected output kind
- whether the user wants implementation, review, triage, or only planning

## 3. Refactor Principle

The kernel should stop asking:

> "How do I run the planner for this input?"

and instead ask:

> "What execution recipe should this request use?"

This is the key architectural shift.

## 4. Target Kernel Model

The new kernel should be shaped around **Execution Recipes**.

### 4.1 New flow

```text
request
  -> intent classification
  -> context assembly
  -> execution recipe selection
  -> optional planning stage
  -> task graph or direct scoped execution
  -> evaluation policy
  -> finalization
```

### 4.2 Execution recipe

An execution recipe is a typed contract that tells the kernel:

- whether planning is needed
- what planning depth to use
- whether to execute direct-to-code or via task graph
- how evaluation should work
- what counts as completion

### 4.3 Proposed request/recipe taxonomy

At minimum, the kernel should support:

1. `greenfield_build`
   - full planner pipeline
   - full work graph
   - strong validation

2. `brownfield_change`
   - minimal or scoped planning
   - task graph optional
   - linked paths strongly influence execution

3. `bugfix`
   - no product-architecture planner
   - reproduction + scoped implementation + focused evaluation

4. `review_or_analysis`
   - no code execution by default
   - evidence collection + findings output

5. `plan_only`
   - plan/spec output only
   - no generator/evaluator loop

6. `resume`
   - resume existing recipe, not just existing run directory

The point is not to create many modes for their own sake.
The point is to make the kernel stop pretending all work is the same.

## 5. New Kernel Contracts

## 5.1 ExecutionIntent

New typed object produced before entering the kernel:

```python
ExecutionIntent = {
  "intent_type": "greenfield_build" | "brownfield_change" | "bugfix" | "review_or_analysis" | "plan_only" | "resume",
  "scope_level": "tiny" | "small" | "medium" | "large",
  "desired_outcome": "code_change" | "plan" | "spec" | "analysis",
  "linked_paths": [...],
  "has_existing_spec": bool,
  "has_existing_plan": bool,
  "resume_run_id": str | None,
}
```

## 5.2 ExecutionRecipe

The recipe drives the kernel:

```python
ExecutionRecipe = {
  "recipe_id": "brownfield-scoped-v1",
  "planning_policy": "none" | "minimal" | "full" | "resume_existing",
  "task_source": "direct_scope" | "work_plan" | "feature_list" | "existing_plan",
  "execution_mode": "single_scope" | "task_loop" | "parallel_task_loop",
  "evaluation_policy": "light" | "scoped_tests" | "full_suite",
  "completion_policy": "single_change" | "task_graph" | "analysis_only",
}
```

## 5.3 KernelRunState

State should stop encoding implicit assumptions about planner-first runs.

Replace the current phase-centric shape with explicit stage data:

```python
KernelRunState = {
  "recipe_id": "...",
  "stage": "bootstrap" | "planning" | "execution" | "evaluation" | "finalization" | "completed" | "failed" | "interrupted",
  "planning_status": "...",
  "execution_status": "...",
  "active_scope": "...",
  "active_task_id": "...",
  "last_updated": "...",
}
```

`planner_complete` should become recipe/stage-specific status, not a universal top-level truth.

## 6. Streamlined Kernel Architecture

Break the orchestrator into explicit modules instead of one large control function.

## 6.1 Kernel bootstrap

Owns:

- config loading
- run registry
- signal handling
- event tracker
- state initialization
- worktree sweep

Suggested module:

- `src/kernel/bootstrap.py`

## 6.2 Recipe selector

Owns:

- classify request intent
- choose execution recipe
- select planning depth

Suggested modules:

- `src/kernel/intent.py`
- `src/kernel/recipes.py`

## 6.3 Planning engine

Owns:

- no planning
- minimal planning
- full planner pipeline
- plan reuse / plan resume

Suggested modules:

- `src/kernel/planning/__init__.py`
- `src/kernel/planning/full_pipeline.py`
- `src/kernel/planning/minimal_scope.py`
- `src/kernel/planning/reuse.py`

## 6.4 Task source adapter

Owns:

- convert planning output into executable units
- support `work_plan`, `direct_scope`, or `existing_plan`

Suggested modules:

- `src/kernel/task_sources/work_plan_source.py`
- `src/kernel/task_sources/direct_scope_source.py`
- `src/kernel/task_sources/existing_plan_source.py`

## 6.5 Execution engine

Owns:

- sequential scoped execution
- sequential task execution
- parallel task execution
- retry policy

Suggested modules:

- `src/kernel/execution/scoped_change.py`
- `src/kernel/execution/task_loop.py`
- `src/kernel/execution/parallel_task_loop.py`

## 6.6 Evaluation engine

Owns:

- lightweight evaluation for small changes
- scoped tests
- full evaluator prompt
- review-only output

Suggested modules:

- `src/kernel/evaluation/light.py`
- `src/kernel/evaluation/scoped.py`
- `src/kernel/evaluation/full.py`

## 6.7 Finalizer

Owns:

- completion decision
- registry update
- artifact summary
- exit semantics

Suggested module:

- `src/kernel/finalize.py`

## 7. Recommended Execution Recipes

## 7.1 `brownfield-scoped-v1`

Use for:

- tiny and small changes
- linked-file requests
- UI tweaks
- focused implementation asks

Behavior:

- skip full planner pipeline
- build a tiny scoped task list from request + linked paths
- execute one scoped implementation session
- run lightweight evaluation or scoped tests
- complete on one successful validated change

This is the recipe the current product prototype needed.

## 7.2 `bugfix-diagnose-v1`

Use for:

- “X is broken”
- repro-driven requests
- regressions

Behavior:

- collect failure context
- optionally create 1-3 scoped tasks
- implement minimal fix
- evaluate against repro/test command

## 7.3 `greenfield-full-v1`

Use for:

- large new project asks
- no existing linked files
- no existing plan/spec

Behavior:

- current planner pipeline mostly preserved
- current work plan preserved
- current generator/evaluator loop preserved

This recipe keeps the kernel’s existing strength.

## 7.4 `plan-only-v1`

Use for:

- spec requests
- roadmap work
- architecture design

Behavior:

- run planning
- stop before generator/evaluator

## 8. Migration Strategy

This refactor should happen in stages, but the stages should move toward a cleaner architecture, not add ad hoc conditionals.

## Phase 1: Introduce intent + recipe types

Add:

- `ExecutionIntent`
- `ExecutionRecipe`
- a recipe selector in the product-to-kernel boundary

Outcome:

- the kernel is no longer entered as a generic planner-first black box

## Phase 2: Split `run_harness`

Extract from [`src/core/orchestrator.py`](../../src/core/orchestrator.py):

- bootstrap
- planning dispatch
- execution dispatch
- finalization

Outcome:

- different recipes can use different stage subsets

## Phase 3: Add `brownfield-scoped-v1`

Implement the first non-greenfield recipe.

Behavior:

- skip `run_planner_pipeline`
- skip mandatory `work_plan.json`
- generate a direct scoped execution plan from request + linked files
- use lighter evaluation

Outcome:

- tiny product requests stop becoming full greenfield plans

## Phase 4: Decouple completion from feature-list assumptions

Refactor completion logic in [`src/core/completion.py`](../../src/core/completion.py) so completion policy depends on recipe.

Outcome:

- scoped runs can complete without pretending they are feature-list graph runs

## Phase 5: Normalize event model by stage

Move away from planner/generator/evaluator-only status assumptions and toward:

- stage events
- scope events
- task events
- launch lifecycle events

Outcome:

- product UI no longer depends on legacy event semantics

## Phase 6: Retire legacy entry assumptions

Deprecate direct kernel assumptions that all runs begin from:

- `prompt`
- `spec`
- `plan`
- `resume`

Those can remain as compatibility inputs, but they should be translated into intent + recipe first.

## 9. What Should Stay

This is not a rewrite of everything.

Keep:

- planner pipeline for `greenfield-full-v1`
- work plan model for large structured builds
- parallel worktree execution
- evaluator reliability logic
- circuit breaker
- resumability
- run registry

The problem is not that the current kernel is bad.
The problem is that it only knows one way to be good.

## 10. Concrete Refactor Target

The near-term implementation target should be:

```text
product request
  -> classify intent
  -> select recipe
  -> kernel bootstrap
  -> recipe-specific planning path
  -> recipe-specific execution path
  -> recipe-specific evaluation path
  -> unified run summary
```

That is the streamlined kernel shape.

## 11. Recommendation

Do not add another layer of special cases inside the current planner-first `run_harness`.

Instead:

1. introduce intent classification
2. introduce execution recipes
3. split `run_harness` into stage modules
4. implement `brownfield-scoped-v1` first
5. leave `greenfield-full-v1` as the compatibility path

That gives Harness something it currently does not have:

**a stable kernel that can vary behavior by request type without losing reliability.**

## 12. Immediate Next Build Step

The first code refactor should be:

- add `ExecutionIntent` + `ExecutionRecipe` types
- add a recipe selector in the product run service
- extract planner dispatch out of `run_harness`
- implement a stub `brownfield-scoped-v1` path that bypasses full planner

That is the smallest real move away from rigidity.
