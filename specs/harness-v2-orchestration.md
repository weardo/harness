# Harness v2 — Orchestration Control Plane

## Problem Statement
The current harness (run.py) is a black box: no visibility without shelling out to bash, no multi-project support, no persistent knowledge across runs, no API surface for agents to query. This spec defines the control plane that fixes all of these.

## Architecture Overview

Hub-and-spoke:
- **Control Plane** (persistent HTTP daemon) — the hub
- **run.py instances** (one per project run) — the spokes, post events to control plane
- **Dashboard UI** — consumes SSE from control plane
- **CLI** — talks to control plane REST API
- **MCP Server** — wraps control plane REST API for agent consumption
- **Knowledge DB** — sqlite-vec store fed by retrospectives, queried by planner

Control plane is OPTIONAL — run.py works standalone if not present.

## Deployment Tiers

**Tier 1 (this spec):** Single machine, all components local, SQLite + sqlite-vec storage
**Tier 2 (future):** Single machine, multiple concurrent runs, same storage
**Tier 3 (future):** Multi-machine, shared Postgres + Qdrant, networked agents
**Tier 4+ (future):** Docker/K8s orchestration, pod-per-agent, consent-based knowledge sharing

Storage architecture is designed for Tier 1 but abstracted for Tier 3 upgrade.

## Storage Architecture

### Per Tier Recommendation

| Concern | Tier 1 | Tier 3+ |
|---------|--------|---------|
| Run metadata / state | SQLite | PostgreSQL |
| Knowledge embeddings | sqlite-vec | Qdrant (self-hosted) |
| Event streaming | SSE (in-process) | Redis pub/sub |
| Run logs | Flat files per run | Structured log DB |

**Rationale:** sqlite-vec for Tier 1 because it's zero-infrastructure, file-based, and already used in brain. Qdrant for Tier 3 because it's production-grade, has filtering, and supports multi-node. The knowledge interface is abstracted behind a `KnowledgeStore` interface so the upgrade is a config change, not a rewrite.

### Database Schema (SQLite — Tier 1)

```sql
-- Projects registry
CREATE TABLE projects (
  id TEXT PRIMARY KEY,           -- slug, e.g. "llm-obs-dashboard"
  name TEXT NOT NULL,
  path TEXT NOT NULL UNIQUE,     -- absolute path to project dir
  harness_prompt_path TEXT,      -- path to harness-prompt.md
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_run_at TEXT
);

-- Runs (one per harness execution)
CREATE TABLE runs (
  id TEXT PRIMARY KEY,           -- uuid
  project_id TEXT NOT NULL REFERENCES projects(id),
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|running|complete|failed|cancelled
  started_at TEXT,
  completed_at TEXT,
  duration_s INTEGER,
  features_planned INTEGER,
  features_done INTEGER,
  features_failed INTEGER,
  cb_opens INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0,
  score_planner INTEGER,         -- from retrospective
  score_generator INTEGER,
  score_evaluator INTEGER,
  score_overall INTEGER,
  notes TEXT                     -- free text
);

-- Feature events (one row per feature attempt)
CREATE TABLE feature_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id),
  feature_id TEXT NOT NULL,      -- e.g. "FEAT-001"
  feature_desc TEXT,
  event_type TEXT NOT NULL,      -- start|pass|fail|retry|skip
  attempt INTEGER DEFAULT 1,
  error TEXT,
  duration_ms INTEGER,
  occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Circuit breaker events
CREATE TABLE cb_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id),
  event_type TEXT NOT NULL,      -- open|close|half_open
  reason TEXT,
  feature_id TEXT,
  occurred_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Knowledge chunks (retrospectives + learnings)
CREATE TABLE knowledge_chunks (
  id TEXT PRIMARY KEY,
  project_id TEXT,               -- null = global
  run_id TEXT,
  chunk_type TEXT NOT NULL,      -- failure_mode|pattern|convention|spec_gap|retro_section
  content TEXT NOT NULL,
  visibility TEXT DEFAULT 'private',  -- private|team|public
  embedding BLOB,                -- sqlite-vec float32 vector
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  tags TEXT                      -- JSON array
);

-- Indexes
CREATE INDEX idx_runs_project ON runs(project_id);
CREATE INDEX idx_runs_status ON runs(status);
CREATE INDEX idx_feature_events_run ON feature_events(run_id);
CREATE INDEX idx_knowledge_chunks_project ON knowledge_chunks(project_id);
CREATE INDEX idx_knowledge_chunks_type ON knowledge_chunks(chunk_type);
```

## REST API Contract

Base URL: `http://localhost:7842/api/v1`
Auth: `X-Harness-Key: <key>` header (optional in dev mode)

### Projects

```
GET    /projects                    → list all projects
POST   /projects                    → register project {id, name, path, harness_prompt_path}
GET    /projects/:id                → get project
DELETE /projects/:id                → deregister (does not delete files)
```

### Runs

```
GET    /runs                        → list runs (query: ?project_id=&status=&limit=)
POST   /runs                        → start run {project_id, env?: {}}
GET    /runs/:id                    → get run detail
DELETE /runs/:id                    → cancel run
GET    /runs/:id/features           → list feature events for run
GET    /runs/:id/logs               → stream run logs (SSE)
```

### Events (posted by run.py)

```
POST   /runs/:id/events             → post event {type, feature_id?, feature_desc?, error?, attempt?, duration_ms?}
  Event types: run_start | feature_start | feature_pass | feature_fail | feature_retry |
               cb_open | cb_close | cb_half_open | run_complete | run_failed
```

### Stream (SSE)

```
GET    /stream                      → global SSE feed (all active runs)
GET    /stream/:run_id              → SSE feed for specific run
  Events: run_started | feature_update | cb_event | run_completed | heartbeat (30s)
```

### Knowledge

```
GET    /knowledge/search?q=&limit=  → semantic search across knowledge chunks
POST   /knowledge/ingest            → ingest retrospective {run_id, content, project_id?}
GET    /knowledge/chunks            → list chunks (query: ?type=&project_id=)
```

### System

```
GET    /health                      → {status, db, runs_active, version}
GET    /metrics                     → aggregate metrics across all runs
```

## SSE Event Schema

```json
// feature_update
{
  "event": "feature_update",
  "run_id": "abc-123",
  "project_id": "llm-obs-dashboard",
  "feature_id": "FEAT-012",
  "feature_desc": "Implement SSE endpoint",
  "status": "pass",           // start|pass|fail|retry
  "attempt": 1,
  "done": 12,
  "total": 50,
  "pct": 24,
  "cb_state": "CLOSED",
  "timestamp": "2026-03-28T10:00:00Z"
}

// cb_event
{
  "event": "cb_event",
  "run_id": "abc-123",
  "cb_state": "OPEN",
  "reason": "3 consecutive failures",
  "feature_id": "FEAT-015",
  "timestamp": "2026-03-28T10:05:00Z"
}

// run_completed
{
  "event": "run_completed",
  "run_id": "abc-123",
  "project_id": "llm-obs-dashboard",
  "features_done": 50,
  "features_total": 50,
  "cb_opens": 0,
  "duration_s": 21600,
  "timestamp": "2026-03-28T16:00:00Z"
}

// heartbeat (every 30s to keep connection alive)
{
  "event": "heartbeat",
  "active_runs": 2,
  "timestamp": "2026-03-28T10:00:30Z"
}
```

## MCP Server Tools

```json
// list_runs
{
  "name": "list_runs",
  "description": "List active and recent harness runs with status",
  "inputSchema": {
    "type": "object",
    "properties": {
      "status": {"type": "string", "enum": ["running","complete","failed","all"]},
      "project_id": {"type": "string"},
      "limit": {"type": "number", "default": 10}
    }
  }
}

// get_run_status
{
  "name": "get_run_status",
  "description": "Get detailed status for a specific run including feature progress",
  "inputSchema": {
    "type": "object",
    "required": ["run_id"],
    "properties": {
      "run_id": {"type": "string"}
    }
  }
}

// start_run
{
  "name": "start_run",
  "description": "Start a new harness run for a registered project",
  "inputSchema": {
    "type": "object",
    "required": ["project_id"],
    "properties": {
      "project_id": {"type": "string"},
      "env": {"type": "object", "description": "Optional env vars to pass to harness"}
    }
  }
}

// query_knowledge
{
  "name": "query_knowledge",
  "description": "Semantic search across accumulated learnings, failure modes, and retrospectives",
  "inputSchema": {
    "type": "object",
    "required": ["query"],
    "properties": {
      "query": {"type": "string"},
      "chunk_type": {"type": "string", "enum": ["failure_mode","pattern","convention","spec_gap","retro_section","all"]},
      "project_id": {"type": "string", "description": "Scope to project, or omit for global"},
      "limit": {"type": "number", "default": 5}
    }
  }
}

// get_retrospectives
{
  "name": "get_retrospectives",
  "description": "List and read retrospectives by project",
  "inputSchema": {
    "type": "object",
    "properties": {
      "project_id": {"type": "string"},
      "limit": {"type": "number", "default": 5}
    }
  }
}
```

## CLI Commands

```bash
harness register <path>              # register project from path (reads harness-prompt.md)
harness start <project-id>           # start a run
harness status                       # list all active runs
harness status <run-id>              # detailed status for run
harness logs <run-id>                # stream logs for run (SSE)
harness ps                           # list registered projects
harness cancel <run-id>              # cancel a running harness
harness knowledge search "<query>"   # semantic search knowledge base
harness knowledge ingest <retro.md>  # manually ingest a retrospective
```

## run.py Integration Protocol

run.py posts events to the control plane via HTTP. Integration is optional — if `HARNESS_CONTROL_PLANE_URL` is not set, run.py operates standalone.

```python
# Environment variables
HARNESS_CONTROL_PLANE_URL=http://localhost:7842   # enables integration
HARNESS_API_KEY=<key>                              # auth key (optional in dev)

# Event posting (fire-and-forget, non-blocking)
# run.py calls POST /runs to create it, control plane returns the ID. run.py stores it.
# On startup: POST /runs (creates run), stores run_id
# On each feature: POST /runs/:id/events {type: "feature_start"|"feature_pass"|...}
# On CB change: POST /runs/:id/events {type: "cb_open"|"cb_close"}
# On complete: POST /runs/:id/events {type: "run_complete"}
# On retrospective write: POST /knowledge/ingest {run_id, content}
```

### Worktree Lifecycle

The orchestrator manages git worktrees for agent isolation. Without active cleanup, worktrees accumulate unboundedly (observed: 46 orphaned worktrees after 2 runs). The orchestrator MUST implement:

```python
# On agent dispatch (before creating worktree)
def dispatch_agent(task, needs_write_access):
    if not needs_write_access:
        # Read-only tasks (explore, validate, research) run in main tree
        return run_in_main_tree(task)

    # Check for reusable existing worktree
    existing = find_worktree_by_topic(task.topic)
    if existing and existing.head == master_head:
        return reuse_worktree(existing, task)

    return create_new_worktree(task)

# On feature merge (after cherry-pick/merge succeeds)
def post_merge_cleanup(worktree_path, branch_name):
    subprocess.run(["git", "worktree", "remove", "--force", worktree_path])
    subprocess.run(["git", "branch", "-D", branch_name])

# On run complete or session exit
def sweep_worktrees():
    for wt in git_worktree_list():
        if wt.path == main_tree:
            continue
        if is_ancestor_of_master(wt.head):
            git_worktree_remove(wt.path, force=True)
            git_branch_delete(wt.branch)
    subprocess.run(["git", "worktree", "prune"])
```

**Rules:**
1. Never create a worktree for a read-only task (exploration, validation, research)
2. After every successful merge to master, delete the source worktree + branch immediately
3. On run completion, sweep all worktrees whose HEAD is at or behind master
4. On session start, check for and report orphaned worktrees before creating new ones
5. Log worktree create/reuse/delete events to the control plane for visibility

## Knowledge Ingestion Pipeline

On run completion:
1. RETROSPECTIVE.md is written to project dir (existing machinery)
2. run.py reads RETROSPECTIVE.md and posts it to `POST /knowledge/ingest`
3. Control plane splits it into chunks by section
4. Each chunk is embedded (text-embedding-3-small via OpenAI, or local via ollama if no API key)
5. Embeddings stored in knowledge_chunks with sqlite-vec

On next run start, planner queries knowledge before writing feature_list.json:
```
GET /knowledge/search?q=failure+modes+TypeScript+backend&limit=10
```
Results are injected into the planner context as "LEARNINGS FROM PREVIOUS RUNS".

## Dashboard UI

Single-page app (vanilla JS or React, dark theme):

**Pages:**
1. **Overview** — active runs (live progress bars), recent completions, aggregate metrics (total runs, avg score, total features built). Must show data on load without user action.
2. **Run Detail** — stat cards (status, features %, duration, cost, CB opens), feature-by-feature timeline with descriptions (not just IDs), click-to-expand detail panel per feature showing attempt/duration/error, retrospective section showing knowledge chunks for the run. Each feature row must be clickable and expand to show all fields.
3. **Projects** — registered projects with auto-loaded run history (no lazy load on click). Each project card clickable → navigates to project detail page with full run table. Each run row clickable → navigates to run detail.
4. **Knowledge** — loads all chunks on mount (browse mode). Search filters results in real time. Each chunk displays type badge, project association, and full content. Must NOT require a search query to show results.

**Behavioral acceptance criteria (anti-dashboard-theatre):**
- Every page that lists data must call its API endpoint on mount and render results without user input
- Every clickable element (card, row, button) must have a registered click handler that produces visible navigation or state change
- Every entity must display its human-readable name/description, not just its ID/slug
- The evaluator must perform an end-to-end walkthrough: Overview → click project → see project detail → click run → see features → expand a feature → see detail → navigate to Knowledge → see all chunks

**Real-time:** Dashboard connects to `GET /stream` SSE endpoint on load. Progress bars update in real time without polling.

## Tech Stack

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| Control plane | Node.js + Express | Matches existing harness (no new runtime) |
| Storage | SQLite + sqlite-vec | Zero-infra, already in brain |
| Embeddings | OpenAI text-embedding-3-small (fallback: none) | Consistent with brain MCP server |
| Dashboard | Vanilla JS SPA | No build step, dark theme, data-dense |
| CLI | Node.js (bin/harness) | Single runtime |
| MCP server | Node.js stdio | Matches brain MCP pattern |
| Docker | docker-compose.yml | Control plane + optional Qdrant for Tier 3 |

## 3-Pass Adversarial Review

### Pass 1: Technical Gaps
1. **Run ID ownership** — who generates the run ID? run.py should POST /runs to create it, control plane returns the ID. run.py stores it for subsequent event posts. RESOLVED: run.py calls POST /runs at startup.
2. **Concurrent run isolation** — two concurrent runs for same project_id? Schema allows it (run_id is UUID), but CLI should warn. RESOLVED: warn, don't block.
3. **SSE backpressure** — fast harness floods SSE. Control plane must buffer last N events per run for late-joining dashboard clients. Add `run_events_buffer` (last 100 events per run, in-memory).
4. **sqlite-vec availability** — sqlite-vec requires native module. If not installed, knowledge search should degrade gracefully (text search fallback via FTS5). RESOLVED: try/catch on import, fall back to FTS5.
5. **run.py fire-and-forget** — HTTP event posts must not block generator loop. Use async non-blocking posts with timeout (500ms). If control plane is down, log warning, continue.
6. **Log persistence** — logs are SSE-streamed but not persisted. Add `run_logs` table or flat files under `{data_dir}/logs/{run_id}.log`. RESOLVED: flat file per run.

### Pass 2: AI Failure Modes (for when harness builds this)
1. **FM: SSE cleanup omission** — guard: every SSE endpoint must close connection on `req.on('close')`.
2. **FM: sqlite-vec import not guarded** — guard: wrap in try/catch, expose `knowledgeAvailable` flag, degrade gracefully.
3. **FM: run.py integration hardcoded URL** — guard: read from env var `HARNESS_CONTROL_PLANE_URL`, default to null (standalone mode).
4. **FM: MCP server not in stdio mode** — guard: detect `--mcp` flag, switch transport.
5. **FM: Knowledge ingestion blocks run completion** — guard: ingest is async, run.py does not await it.
6. **FM: Dashboard hardcoded localhost** — guard: dashboard reads control plane URL from `window.__HARNESS_CONFIG__` injected by server.
7. **FM: Missing auth on event endpoints** — spec says auth is optional in dev, but event POST endpoints must be keyed to prevent external injection. RESOLVED: API key required for write endpoints even in dev mode.
8. **FM: Dashboard theatre** — evaluator validates endpoints return 200 and tests pass, but never checks that the UI actually renders data, has working click handlers, or shows descriptions instead of IDs. Result: 48/48 features pass, dashboard is an empty shell. Guard: evaluator MUST perform browser-level behavioral verification for all UI features — check visible text matches API data, click every interactive element, assert expanded views show detail. Add integration walkthrough features at phase boundaries (see feature list).
9. **FM: Browse-requires-search** — knowledge/list pages show empty state until user types a query, even when data exists. Guard: every "browse" page must call its list/chunks endpoint on mount and render results without user input. Search refines, it doesn't activate.
10. **FM: Worktree accumulation** — agent dispatches with `isolation: "worktree"` create git worktrees eagerly but never clean them up. Three sub-problems:
    - **Ghost worktrees**: 61% of observed worktrees (28/46) had zero changes — created for read-only tasks (exploration, validation) where isolation was unnecessary. Auto-cleanup on "no changes" doesn't fire reliably.
    - **Duplicate branches**: Session death (context exhaustion) orphans worktrees. Next session doesn't discover existing worktrees, re-does the work in a new one. Result: parallel branches with overlapping commits (`vibrant-torvalds` and `zealous-dijkstra` both committed identical "learning loop" work).
    - **No post-merge reap**: Cherry-pick/merge to master succeeds, but source worktree + branch persist forever.
    Guard: implement a 3-part worktree lifecycle:
    (a) **Post-merge cleanup** — after any cherry-pick or merge to master, immediately `git worktree remove` the source + `git branch -D` the source branch.
    (b) **Session-end sweep** — on `/compact`, session exit, or context >80%, run `git worktree list`, prune any worktree whose HEAD is ancestor-of or equal-to master HEAD.
    (c) **Pre-create dedup** — before creating a new worktree for a task, scan existing worktrees for one with matching topic (grep commit messages or branch names). Reuse if found instead of creating a duplicate.
    (d) **Read-only dispatch guard** — if the agent task is exploration/research/validation (no Write/Edit tools needed), do NOT use `isolation: "worktree"`. Only isolate tasks that will write code.

### Pass 3: Community Alignment
- **Prefect/Temporal patterns:** Run state machine (pending→running→complete/failed) matches Prefect's flow run model. Adopt same state transitions.
- **OpenTelemetry:** Feature events map cleanly to OTEL spans. Future: export run events as OTEL traces for integration with LLM Obs Dashboard (built in run 1).
- **Prometheus metrics:** `/metrics` endpoint should expose Prometheus-compatible format for Tier 3 monitoring. Add `Content-Type: text/plain; version=0.0.4`.
- **MCP conventions:** Tool names should be verbs (list_runs ✓, get_run_status ✓). Follow brain MCP server patterns.
- **CLI conventions:** `harness` command follows `docker`/`kubectl` sub-command pattern — already done.

## Scale Architecture

> This section defines how the harness handles large projects (100+ features, multi-service, complex dependencies). The base v2 build targets single-service projects. This architecture is the design target for v2.1+, but the data model and API must support it from day one.

### The Problem With Flat Feature Lists

A flat feature_list.json breaks at scale in three ways:
1. **Context saturation** — at 100+ features, the generator loses track of code written 80 features ago and makes wrong assumptions
2. **Dependency blindness** — `depends_on` is decorative in a flat list; nothing enforces ordering
3. **Service cross-contamination** — one generator touching all services makes incoherent cross-boundary decisions

All leading agent frameworks (AutoGen, CrewAI, LangGraph, Devin) solve this the same way: hierarchical decomposition + enforced dependency graphs + service isolation.

### Hierarchical Work Model

Replace the flat feature list with a 3-tier hierarchy:

```
Epic (a coherent capability, e.g. "Auth system")
└── Story (a user-facing feature, e.g. "JWT login endpoint")
    └── Task (a single generator invocation, e.g. "Implement POST /auth/login")
```

- **Task** = atomic generator unit. Small enough for one context window. One agent, one session.
- **Story** = evaluator unit. A story is "done" when all its tasks pass evaluation.
- **Epic** = integration unit. An epic is "done" when cross-story integration tests pass.
- **Phase** = a group of epics that must all complete before the next phase starts.

### Enforced Dependency DAG

The orchestrator runs topological sort on the task/story/epic graph before execution begins:
- Circular dependencies = hard error at plan time, not runtime
- A task only starts when all its `depends_on` tasks are evaluator-approved (not just run)
- Phase gates: all tasks/stories in Phase N must pass before Phase N+1 begins

**Phase 0 is always contracts.** Before any implementation code is written:
- All API contracts (OpenAPI specs) are generated
- All event schemas are defined
- All database schemas are locked
- These become read-only inputs to all subsequent phases

### Blackboard State Store

Agents don't pass full context to each other. Instead they publish to and subscribe from a shared blackboard (a SQLite table in the control plane DB):

```sql
CREATE TABLE blackboard (
  key TEXT PRIMARY KEY,          -- e.g. "contracts/auth-api", "status/auth-service"
  value TEXT NOT NULL,           -- JSON value
  written_by TEXT,               -- agent/task that wrote it
  run_id TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Example entries:
- `contracts/auth-api` → OpenAPI spec JSON
- `schemas/users_table` → CREATE TABLE DDL
- `status/auth-service` → "complete"
- `known_failures/this-run` → JSON array of failure modes discovered

Each generator receives only the blackboard slice relevant to its task, not the full codebase context.

### Service-Scoped Generator Sessions

For multi-service projects, each service gets its own generator session with:
- A system prompt scoped to that service only ("You are building the auth-service. Do not touch api-gateway or user-service.")
- A working directory restricted to that service's folder
- Read-only access to the blackboard (contracts, schemas from other services)
- Write access only to its own service directory

The orchestrator manages handoffs between services. Generators never coordinate directly.

### Memory Bank (Context at Scale)

For large projects where the codebase grows beyond context window capacity, generators maintain a **memory bank** — a structured index of what's been built:

```json
{
  "files_created": ["src/auth/login.ts", "src/auth/middleware.ts"],
  "apis_implemented": ["POST /auth/login", "POST /auth/refresh"],
  "schemas_created": ["users", "sessions"],
  "known_issues": ["users.email unique constraint missing"],
  "dependencies_satisfied": ["FEAT-001", "FEAT-003"]
}
```

The generator reads the memory bank at the start of each task, not the full codebase. It updates the bank after each task. This is how Devin handles large codebases — RAG over the agent's own work history.

### Recommended Agent Roles for Complex Projects

```
Orchestrator (directs, never codes)
├── reads: full DAG, blackboard, phase status
├── writes: task assignments, phase decisions
└── never: touches implementation files

Architect Agent (Phase 0 only)
├── generates: API contracts, event schemas, DB schemas
└── output goes to blackboard as read-only

Service Team (one per service)
├── Planner — breaks epic into stories+tasks for this service
├── Generator — implements one task at a time, reads blackboard slice
└── Evaluator — validates story completion, writes pass/fail to blackboard

Integration Agent (cross-service phase)
└── runs end-to-end scenarios that cross service boundaries
```

This maps to AutoGen's "nested teams," CrewAI's "hierarchical crews," and Anthropic's orchestrator/subagent model. The evaluator is always treated as a hard gate — outputs are untrusted until evaluator-approved.

## Success Criteria (Binary)

1. `harness start llm-obs-dashboard` triggers a run visible in dashboard within 5 seconds
2. Dashboard shows live feature progress bar updating in real time during run
3. `harness status` returns active runs with done/total counts
4. MCP tool `get_run_status` returns structured JSON without bash
5. Knowledge base has entries after run completes and `query_knowledge` returns relevant results
6. run.py operates identically when control plane is not running (standalone mode)
7. `/health` returns 200 with db: connected
8. SSE connection count does not grow unboundedly across 10 connect/disconnect cycles
9. `/metrics` returns Prometheus-compatible output
10. All components start with `docker-compose up`

## Feature List (for harness prompt)

Approximately 45 features:
- Control plane: HTTP server, DB init, project CRUD (4), run CRUD (5), event ingestion (3), SSE global + per-run (3), knowledge ingest + search (3), health + metrics (2), log streaming (2)
- Dashboard: SPA shell, overview page, run detail page (with feature expand), projects page (with auto-loaded runs + project detail route), knowledge page (browse on mount + search filter), SSE client, real-time progress bars (9)
- Integration walkthrough features (evaluator MUST use browser automation for these):
  - "Navigate overview → click run → see feature timeline with descriptions → expand feature → see detail panel" (1)
  - "Navigate projects → click project card → see project detail with run table → click run → see run detail" (1)
  - "Navigate knowledge → see all chunks on load → type search → see filtered results → clear search → see all chunks again" (1)
- CLI: bin setup, register, start, status, logs, ps, cancel, knowledge commands (8)
- MCP: stdio server, 5 tools (6)
- run.py integration: env var detection, event posting, retro auto-ingest (3)
- Knowledge pipeline: chunking, embedding, sqlite-vec storage, FTS5 fallback (4)
- Docker: docker-compose, Dockerfile, init script (3)
