# Harness Product Plan — Early Linear-Equivalent Prototype

**Date:** 2026-04-08  
**Status:** Draft execution plan  
**Intent:** Build a working prototype fast, not a full Linear clone

## 1. Executive Summary

Yes, Harness can evolve into a product in the same category as Linear's March 24, 2026 "Next" vision, but the right first move is not to build a full product operating system all at once.

The fastest viable path is:

1. Keep Harness as the execution kernel.
2. Add a real product layer on top of it.
3. Narrow the first prototype to one extremely strong workflow:
   intake -> contextual planning -> agent execution -> human review -> ship.

The prototype should serve a small AI-native product team that already works in GitHub, Slack, and docs, and wants a shared system where:

- context is captured in one place
- agents can turn that context into scoped work
- work can be executed through Harness
- humans can review runs, outcomes, and diffs

The prototype should not try to match all of Linear. It should win on one thing:

**turning messy product context into agent-executable work with clear observability and human control.**

## 2. What We Learned From Linear

## 2.1 Linear's current thesis

Linear's March 24, 2026 "Next" direction is not "issue tracking with AI." It is a much broader system:

- "Issue tracking is dead."
- The new system is designed around "context and agents."
- Linear is becoming "the shared product system that turns context into execution."
- Inputs include feedback, decisions, plans, specs, discussions, and code.
- The operating model includes agents, rules, skills, and automations.
- Current launches: Linear Agent, Skills, Automations.
- Announced next: Code Intelligence, Code Diffs, Linear Coding Agent.

This matters because the real product category is shifting from:

- tracker -> backlog -> tickets

to:

- context system -> agent routing -> execution system

## 2.2 Linear's practical product moves

From Linear's recent launches:

- March 24, 2026: Linear Agent is grounded in workspace context and can synthesize, recommend, and act.
- March 24, 2026: Skills are reusable workflows and automations can trigger on triage events.
- February 26, 2026: Linear added direct launch into coding tools with prefilled issue context and customizable prompt templates.

This is especially important: Linear is not insisting on owning all coding from day one. It is already acting as the context and launch surface for external coding agents. That creates a very strong prototype lesson for Harness:

**the early product does not need a native coding agent UI if it can reliably marshal context into execution.**

## 2.3 What not to copy from Linear yet

We should not start with:

- full issue tracker parity
- full documents product
- broad enterprise permissions matrix
- deep analytics/reporting
- polished mobile experience
- a giant integration matrix

Those are scale advantages, not prototype requirements.

## 3. What the Brain Knowledge Base Suggests

The `brain` repo strongly reinforces a few ideas:

- `docs/VISION.md`: the durable idea is a system that "learns, connects, anticipates, and acts."
- `docs/specs/brain-intelligence-layer.md`: context quality depends on indexing, linking, resurfacing, and session context, not just storage.
- `docs/superpowers/specs/2026-03-14-open-brain-design.md`: MCP + local-first + shared knowledge + dashboard is the right foundation.
- `vault/industry/multi-agent-team-orchestration-2026.md`: scalable agent systems converge on hierarchical decomposition, DAG execution, contract-first phases, blackboard state, and evaluator gates.
- `vault/industry/claude-code-skills-2-0-and-automation-innovations.md`: skills + automations + evaluation-driven workflow design are becoming core primitives.
- `vault/industry/ai-workflow-setup-landscape-2026.md`: the market gap is not "more plugins," it is tailored workflow generation from real project context.

The strongest implication:

**Harness should not become "just another coding agent." It should become the system that prepares context, decomposes work, orchestrates execution, and records learning across runs.**

## 4. Latest AI Coding Trends That Matter

As of April 8, 2026, the strongest recurring themes across official product and engineering sources are:

### 4.1 Multi-agent orchestration is going mainstream

- Anthropic's June 13, 2025 multi-agent research system writeup shows orchestrator + bounded subagents + parallel work are now a production pattern.
- GitHub Copilot CLI's January 14, 2026 update added built-in custom agents and parallel delegation.
- OpenAI's February 2, 2026 Codex app centers on managing multiple coding agents in parallel.

Implication:

**parallel agent orchestration is now a product feature, not just an internal architecture.**

### 4.2 Context engineering beats raw prompting

- Anthropic's September 29, 2025 context engineering article reframes performance as a context design problem.
- Linear's February 26, 2026 coding-tool deeplink feature explicitly focuses on packaging issue context for agents.
- Your own `brain-intelligence-layer` spec says the same thing: indexing and session context are the unlock.

Implication:

**the product moat is increasingly the quality of context assembly, not the base model alone.**

### 4.3 Skills and automations are becoming standard primitives

- Linear launched Skills and Automations on March 24, 2026.
- Anthropic's March 2026 skills/automation wave pushed evals, skill reuse, and recurring automation.
- GitHub Copilot is also moving toward agent skills and custom agents.

Implication:

**users will expect reusable workflows, not one-off prompts.**

### 4.4 Async/background execution is the norm

- OpenAI Codex app emphasizes long-horizon and background tasks.
- GitHub Copilot cloud agent is asynchronous and PR-oriented.
- Harness already does long-running execution well.

Implication:

**the product must make async work legible: status, checkpoints, artifacts, decisions, and review.**

### 4.5 Human control still matters

- Linear positions agents as helpers inside product context, not independent owners.
- GitHub explicitly frames review and pull request iteration as human-controlled.
- Anthropic repeatedly recommends evaluator gates, sandboxes, and bounded trust.

Implication:

**the winning UX is not "full autonomy." It is high-trust delegation with strong observability and review.**

## 5. Product Thesis for Harness

## 5.1 Product statement

Harness should become:

**a shared execution system for product and engineering teams that turns messy context into scoped agent work, runs that work through reliable orchestration, and keeps humans in control of the final outcome.**

## 5.2 Positioning

Early positioning:

- not a generic issue tracker
- not just a coding agent shell
- not just an orchestration backend

Instead:

**Harness is the execution layer between product context and working software.**

## 5.3 Ideal first user

The best first user is not an enterprise PM org. It is:

- founder-led startup
- AI-native product team
- 3 to 20 engineers
- strong GitHub usage
- willing to let agents draft plans, specs, and code
- currently stitching together Slack, docs, GitHub, and coding tools manually

## 6. The Right Prototype Wedge

## 6.1 Core job to be done

When a new request arrives, the team should be able to:

1. capture it
2. enrich it with surrounding context
3. turn it into a scoped execution plan
4. launch agent work
5. review what happened
6. accept, revise, or ship

If we nail that loop, we have a real product.

## 6.2 Prototype scope

### Include

- Workspace
  - projects
  - issues/requests
  - specs
  - runs
- Intake
  - manual issue creation
  - Slack paste/import
  - GitHub issue import
- Context synthesis
  - related issues
  - linked docs/specs
  - recent decisions/comments
  - codebase/repo metadata
- Agent session launch
  - create execution plan from issue/spec
  - run Harness planner/generator/evaluator
  - show status, logs, artifacts, costs, retries
- Review
  - run summary
  - generated plan/spec
  - generated branch/PR links
  - evaluator verdicts
- Skills
  - save prompt + workflow template
  - run manually on issue/project
- One automation
  - triage automation on new issue

### Exclude

- full backlog planning product
- full docs editor
- multi-team hierarchy
- deep permissions/RBAC
- billing
- mobile app
- advanced analytics
- native code intelligence over huge monorepos
- native diff/code-review UI beyond a minimal embedded or linked review

## 6.3 Prototype promise

**Give me one place to turn a request into an agent-executed implementation attempt with the right context and clear human review.**

## 7. MVP Feature Set

## 7.1 Surface 1: Inbox / Intake

Purpose:

- central place for incoming work

Prototype features:

- create request manually
- paste Slack thread or customer note
- import GitHub issue URL
- classify request type: bug, feature, task, research
- assign project and priority

Why this matters:

- Linear starts with intake too
- without intake, the system has nothing to execute

## 7.2 Surface 2: Context View

Purpose:

- assemble the minimum context humans and agents need

Prototype features:

- request description
- linked comments/discussion
- related prior issues
- linked spec or draft spec
- repo target
- acceptance criteria
- prompt template / selected skill

This is where Harness differentiates. The context bundle should be explicit and inspectable.

## 7.3 Surface 3: Agent Chat / Command View

Purpose:

- allow a user to ask for a plan or execution run in plain English

Prototype features:

- "scope this"
- "turn this into a spec"
- "split into tasks"
- "start implementation"
- "summarize blockers"
- "retry with stricter acceptance criteria"

Under the hood this can still call Harness primitives. The value is the productized entrypoint.

## 7.4 Surface 4: Run Console

Purpose:

- make async execution legible

Prototype features:

- run status
- planner/generator/evaluator phases
- current task
- attempt count
- cost
- elapsed time
- logs and artifacts
- final summary

This is the most natural leverage point from current Harness capabilities.

## 7.5 Surface 5: Skills

Purpose:

- let teams codify good workflows

Prototype features:

- save a skill from a successful agent prompt/run template
- attach skill to issue type or manual slash action
- version a skill as markdown/json config

Do not overbuild a marketplace yet. Team-local reusable skills are enough.

## 7.6 Surface 6: Automation

Purpose:

- prove the system can act automatically on intake

Prototype automation:

- on new issue in triage:
  - summarize request
  - identify duplicates/related work
  - draft acceptance criteria
  - recommend next action

That one automation is enough to validate the model.

## 8. The Product Architecture

## 8.1 High-level architecture

```
Web app / API
  -> product database
  -> context assembly layer
  -> Harness orchestration engine
  -> Git provider integrations
  -> notification channels

Product database
  -> workspaces
  -> projects
  -> requests/issues
  -> docs/specs
  -> runs
  -> skills
  -> automations
  -> context links

Harness engine
  -> planner
  -> generator
  -> evaluator
  -> parallel fleet
  -> event tracker
  -> control plane transport
```

## 8.2 Reuse from current Harness

Reuse directly:

- planner/generator/evaluator architecture
- run state management
- circuit breaker
- work plans
- parallel execution
- event tracking
- control-plane concepts
- knowledge ingestion hooks

Do not rewrite this first.

## 8.3 New product-layer components

Need to build:

- frontend app
- API server
- product datastore
- auth
- issue/project/spec models
- context bundle assembler
- integration adapters
- runs UI
- skills + automations layer

## 8.4 Recommended stack for prototype

- Frontend: Next.js + TypeScript
- Backend API: Next.js route handlers or a small Node service
- DB: PostgreSQL
- Queue/jobs: Postgres-backed jobs or Redis queue
- Realtime: SSE first, websockets later
- Auth: simple workspace auth, likely GitHub login first
- Execution: current Python Harness invoked as worker jobs

Why:

- keeps the prototype simple
- works well with realtime run updates
- allows the Python engine to remain mostly untouched

## 9. Domain Model

Prototype entities:

- Workspace
- Project
- Request
- Spec
- Run
- Artifact
- Skill
- Automation
- IntegrationConnection
- ContextBundle

Minimal fields:

### Workspace

- id
- name
- slug
- settings

### Project

- id
- workspace_id
- name
- repo_url
- default_branch
- status

### Request

- id
- project_id
- title
- description
- source
- type
- priority
- status
- acceptance_criteria
- linked_urls

### Spec

- id
- request_id
- content
- version
- status

### Run

- id
- request_id
- project_id
- mode
- status
- started_at
- ended_at
- cost_usd
- branch_name
- pr_url
- summary

### Artifact

- id
- run_id
- kind
- path_or_blob
- metadata

### Skill

- id
- workspace_id
- name
- trigger
- prompt_template
- config

### Automation

- id
- workspace_id
- name
- trigger_event
- action_type
- config
- enabled

### ContextBundle

- id
- request_id
- summary
- related_requests
- related_specs
- repo_context
- selected_skill

## 10. User Flows For The Prototype

## 10.1 Flow A: New feature request

1. User creates request or imports GitHub issue.
2. System enriches with related work and suggested acceptance criteria.
3. User clicks "Create spec."
4. Agent drafts spec and initial work plan.
5. User edits/approves.
6. User clicks "Start implementation."
7. Harness runs planner/generator/evaluator.
8. UI streams progress and artifacts.
9. System returns branch/PR/evaluation summary.
10. User accepts, retries, or closes.

## 10.2 Flow B: Triage automation

1. New issue lands in triage.
2. Automation generates summary, category, duplicate candidates, and next-step recommendation.
3. Human reviews and confirms.
4. Issue becomes ready for spec or execution.

## 10.3 Flow C: Reusable skill

1. User discovers a good planning pattern.
2. User saves it as "Split customer request into spec + implementation tasks."
3. Future requests can use that skill automatically or manually.

## 11. Execution Plan

## 11.1 Phase 0 — 3 to 5 days

Goal:

- lock prototype scope
- define product architecture
- decide whether UI lives inside this repo or a sibling repo

Deliverables:

- final product brief
- entity schema
- API contract draft
- clickable wireframes
- decision on naming and repo layout

## 11.2 Phase 1 — Week 1

Goal:

- establish product shell

Build:

- app shell
- auth
- workspace/project models
- request create/read/update
- database schema
- basic integrations abstraction

Definition of done:

- user can create project and request in UI

## 11.3 Phase 2 — Week 2

Goal:

- build context layer

Build:

- related request linking
- spec model
- context bundle generation
- skill selection on request
- prompt-template plumbing

Definition of done:

- user can open a request and see a structured context bundle

## 11.4 Phase 3 — Week 3

Goal:

- connect UI to Harness execution

Build:

- run creation API
- worker job dispatch
- run event ingestion
- basic run console
- artifact persistence

Definition of done:

- user can launch a run from a request and watch progress live

## 11.5 Phase 4 — Week 4

Goal:

- add useful human review loop

Build:

- run summary UI
- evaluator verdict UI
- branch/PR links
- retry with feedback
- save-as-skill

Definition of done:

- user can review, retry, or accept a run outcome

## 11.6 Phase 5 — Week 5

Goal:

- add one automation and harden the loop

Build:

- triage automation
- issue import from GitHub
- Slack paste/import helper
- quality instrumentation
- failure states and timeouts

Definition of done:

- new issue can be auto-enriched and moved into execution flow

## 11.7 Phase 6 — Week 6

Goal:

- internal alpha

Build:

- polish top flows
- improve run reliability
- fix state gaps
- add onboarding/demo data
- record benchmark metrics

Definition of done:

- one small team can use the product end-to-end on real tasks

## 12. Prototype Milestones

## Milestone A: "Can see it"

- create workspace/project/request
- view context
- no live execution yet

## Milestone B: "Can run it"

- launch Harness run from UI
- view live status and artifacts

## Milestone C: "Can trust it"

- accept/retry loop works
- evaluator findings visible
- one automation saves time

## Milestone D: "Can demo it"

- polished happy path
- real repo
- real issue -> real branch/PR

## 13. Team Shape

Ideal small team:

- 1 product/design lead
- 1 frontend/full-stack engineer
- 1 backend/platform engineer
- 1 agent/orchestration engineer

If only one or two people are available, sequence should be:

1. API + DB
2. request/context UI
3. Harness integration
4. run console
5. automations

## 14. Risks

## 14.1 Biggest product risk

Trying to build too much Linear instead of solving one sharp execution workflow.

Mitigation:

- force every feature to justify how it improves request -> execution -> review

## 14.2 Biggest technical risk

The current Harness event/control-plane layer may not expose enough clean product state for a robust UI.

Mitigation:

- define a stable run/event schema early
- add product-facing adapter layer instead of coupling UI to raw Harness internals

## 14.3 Biggest UX risk

The product feels like a dashboard on top of logs instead of a coherent operating surface.

Mitigation:

- design around the user's job, not engine internals
- request page and run page must be beautiful and legible

## 14.4 Biggest market risk

The wedge is too narrow to matter or too broad to ship.

Mitigation:

- target AI-native teams already feeling the pain of context assembly and async agent execution

## 15. What We Should Explicitly Not Build Yet

- full Jira/Linear migration importer
- advanced roadmap/cycle planning
- organization-level permissions complexity
- code intelligence over all repos
- embedded code diff reviewer
- voice/mobile
- marketplace/distribution layer
- multi-repo portfolio management

These are phase-2 or phase-3 moves.

## 16. Success Metrics For The Prototype

Product metrics:

- time from request creation to executable run
- percent of requests enriched with usable acceptance criteria
- percent of runs reaching evaluator-complete state
- percent of runs producing a branch or PR
- number of manual context-copy steps eliminated
- retry rate before acceptance

Experience metrics:

- median time to understand "what happened in this run"
- user-rated trust in run summaries
- user-rated usefulness of triage automation

## 17. Strategic Moat

If this works, the moat is not just "we also have agents."

The moat is:

- better context packaging
- better execution reliability
- better visibility into long-running agent work
- better reuse through skills and automations
- better feedback loop from past runs into future runs

That last point is especially important. Harness can compound execution knowledge across runs more naturally than a generic issue tracker.

## 18. Recommendation

Build the prototype now with this exact scope:

- requests
- context bundle
- Harness run launch
- live run console
- review/retry loop
- team-local skills
- one triage automation

Do not build a full Linear equivalent first.

Build the narrowest product that proves this sentence:

**Harness can turn product context into reliable agent execution faster than teams stitching together Linear, Slack, GitHub, and coding agents by hand.**

## 19. Sources

External:

- Linear "Issue tracking is dead" — March 24, 2026: https://linear.app/next
- Linear "Introducing Linear Agent" — March 24, 2026: https://linear.app/changelog/2026-03-24-introducing-linear-agent
- Linear "Deeplink to AI coding tools" — February 26, 2026: https://linear.app/changelog/2026-02-26-deeplink-to-ai-coding-tools
- OpenAI release notes on Codex app — February 2, 2026: https://help.openai.com/en/articles/10128477
- GitHub Copilot CLI enhanced agents/context management — January 14, 2026: https://github.blog/changelog/2026-01-14-github-copilot-cli-enhanced-agents-context-management-and-new-ways-to-install
- GitHub Copilot cloud/coding agent docs: https://docs.github.com/en/copilot/responsible-use/copilot-cloud-agent
- Anthropic "How we built our multi-agent research system" — June 13, 2025: https://www.anthropic.com/engineering/built-multi-agent-research-system
- Anthropic "Effective context engineering for AI agents" — September 29, 2025: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Anthropic "Writing effective tools for agents" — September 11, 2025: https://www.anthropic.com/engineering/writing-tools-for-agents
- Anthropic "Harness design for long-running application development" — March 24, 2026: https://www.anthropic.com/engineering/harness-design-long-running-apps

Local inputs:

- `/Users/prashantpandey/brain/docs/VISION.md`
- `/Users/prashantpandey/brain/docs/specs/brain-intelligence-layer.md`
- `/Users/prashantpandey/brain/docs/specs/dashboard-redesign.md`
- `/Users/prashantpandey/brain/docs/superpowers/specs/2026-03-14-open-brain-design.md`
- `/Users/prashantpandey/brain/docs/superpowers/specs/2026-03-30-harness-v3-multi-phase-planner-design.md`
- `/Users/prashantpandey/brain/vault/industry/multi-agent-team-orchestration-2026.md`
- `/Users/prashantpandey/brain/vault/industry/claude-code-skills-2-0-and-automation-innovations.md`
- `/Users/prashantpandey/brain/vault/industry/ai-workflow-setup-landscape-2026.md`
