"""
Hierarchical Work Plan — Phases / Epics / Stories / Tasks
==========================================================

Replaces the flat feature_list.json with a 3-tier work model:
  Phase → Epic → Story → Task

Phases enforce ordering gates (all tasks in phase N must complete
before phase N+1 begins). Tasks are the atomic generator unit.
Stories are the evaluator unit. Epics are the integration unit.

File format: work_plan.json  (state_dir)

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
                  "description": "...",
                  "acceptance_criteria": [...],
                  "steps": [...],
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
"""

from pathlib import Path
from typing import Optional

from .state import atomic_write, atomic_read


class WorkPlan:
    """Hierarchical work plan with phases, epics, stories, and tasks.

    Provides query helpers, dependency resolution, phase gate enforcement,
    DAG validation, and atomic persistence.
    """

    def __init__(self, data: dict):
        """Initialize from a parsed work_plan.json dict."""
        self.data = data

    # -------------------------------------------------------------------------
    # Persistence
    # -------------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "WorkPlan":
        """Load work_plan.json atomically. Returns WorkPlan or raises FileNotFoundError."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"work_plan.json not found: {path}")
        data = atomic_read(path)
        if data is None:
            raise FileNotFoundError(f"work_plan.json not found: {path}")
        return cls(data)

    def save(self, path: Path) -> None:
        """Save work_plan.json atomically using atomic_write."""
        atomic_write(Path(path), self.data)

    # -------------------------------------------------------------------------
    # Internal traversal helpers
    # -------------------------------------------------------------------------

    def _all_tasks(self):
        """Yield (phase, epic, story, task) tuples in document order."""
        for phase in self.data.get("phases", []):
            for epic in phase.get("epics", []):
                for story in epic.get("stories", []):
                    for task in story.get("tasks", []):
                        yield phase, epic, story, task

    def _task_phase_index(self, task_id: str) -> int:
        """Return the 0-based index of the phase containing task_id, or -1."""
        for i, phase in enumerate(self.data.get("phases", [])):
            for epic in phase.get("epics", []):
                for story in epic.get("stories", []):
                    for task in story.get("tasks", []):
                        if task.get("id") == task_id:
                            return i
        return -1

    # -------------------------------------------------------------------------
    # Query helpers (feature 011)
    # -------------------------------------------------------------------------

    def get_task(self, task_id: str) -> Optional[dict]:
        """Find a task by ID. Returns task dict or None."""
        for _, _, _, task in self._all_tasks():
            if task.get("id") == task_id:
                return task
        return None

    def get_story(self, story_id: str) -> Optional[dict]:
        """Find a story by ID. Returns story dict or None."""
        for phase in self.data.get("phases", []):
            for epic in phase.get("epics", []):
                for story in epic.get("stories", []):
                    if story.get("id") == story_id:
                        return story
        return None

    def get_epic(self, epic_id: str) -> Optional[dict]:
        """Find an epic by ID. Returns epic dict or None."""
        for phase in self.data.get("phases", []):
            for epic in phase.get("epics", []):
                if epic.get("id") == epic_id:
                    return epic
        return None

    # -------------------------------------------------------------------------
    # Next task selection (features 012-013)
    # -------------------------------------------------------------------------

    def get_next_task(self) -> Optional[dict]:
        """Return next pending task respecting dependencies and phase gates.

        Phase gate: all tasks in phase N must be done or blocked before
        tasks in phase N+1 are eligible.

        Dependency resolution: task is eligible only if all depends_on tasks
        have status=done.

        Returns None when no eligible task exists.
        """
        phases = self.data.get("phases", [])
        for phase in phases:
            # Collect all tasks in this phase
            phase_tasks = [
                task
                for epic in phase.get("epics", [])
                for story in epic.get("stories", [])
                for task in story.get("tasks", [])
            ]

            # Check phase gate: are there any pending tasks in this phase?
            has_pending = any(t.get("status", "pending") == "pending" for t in phase_tasks)

            if has_pending:
                # This phase is active — find first eligible pending task
                for task in phase_tasks:
                    if task.get("status", "pending") != "pending":
                        continue
                    if self.are_deps_satisfied(task["id"]):
                        return task
                # Pending tasks exist but none are eligible (all have unsatisfied deps)
                return None

            # All tasks in this phase are done or blocked — check if any are pending
            # (they're not, so we advance to next phase)

        return None

    # -------------------------------------------------------------------------
    # Task mutations (features 014-015)
    # -------------------------------------------------------------------------

    def mark_task_done(self, task_id: str, path: Path) -> None:
        """Mark task as done and atomically save."""
        task = self.get_task(task_id)
        if task is not None:
            task["status"] = "done"
            self.save(path)

    def mark_task_blocked(self, task_id: str, reason: str, path: Path) -> None:
        """Mark task as blocked with reason and atomically save."""
        task = self.get_task(task_id)
        if task is not None:
            task["status"] = "blocked"
            task["blocked_reason"] = reason
            self.save(path)

    def increment_task_attempts(self, task_id: str, path: Path) -> int:
        """Increment attempts counter for task. Returns new count, or 0 if not found."""
        task = self.get_task(task_id)
        if task is None:
            return 0
        task["attempts"] = task.get("attempts", 0) + 1
        self.save(path)
        return task["attempts"]

    # -------------------------------------------------------------------------
    # Count methods (feature 016)
    # -------------------------------------------------------------------------

    def count_tasks(self) -> dict:
        """Return task counts: {total, done, blocked, pending} across all phases."""
        total = done = blocked = pending = 0
        for _, _, _, task in self._all_tasks():
            total += 1
            status = task.get("status", "pending")
            if status == "done":
                done += 1
            elif status == "blocked":
                blocked += 1
            else:
                pending += 1
        return {"total": total, "done": done, "blocked": blocked, "pending": pending}

    def count_stories(self) -> dict:
        """Return story count: {total}."""
        total = sum(
            1
            for phase in self.data.get("phases", [])
            for epic in phase.get("epics", [])
            for _ in epic.get("stories", [])
        )
        return {"total": total}

    def count_epics(self) -> dict:
        """Return epic count: {total}."""
        total = sum(
            1
            for phase in self.data.get("phases", [])
            for _ in phase.get("epics", [])
        )
        return {"total": total}

    # -------------------------------------------------------------------------
    # Phase state (feature 017)
    # -------------------------------------------------------------------------

    def current_phase(self) -> Optional[dict]:
        """Return first phase dict with any pending task, or None if all complete."""
        for phase in self.data.get("phases", []):
            for epic in phase.get("epics", []):
                for story in epic.get("stories", []):
                    for task in story.get("tasks", []):
                        if task.get("status", "pending") == "pending":
                            return phase
        return None

    def flatten_for_grouping(self) -> list[dict]:
        """Flatten current-phase pending tasks for parallel grouping.

        Returns flat list compatible with group_by_dependency():
        each dict has: id, depends_on, scope, description, passes, blocked.

        Respects phase gates: only returns tasks from the first phase
        that has pending tasks. Tasks from later phases are excluded.

        Done tasks from earlier phases are included as resolved stubs
        so cross-phase dependencies are visible to group_by_dependency().
        """
        phase = self.current_phase()
        if phase is None:
            return []

        phase_id = phase.get("id")
        result = []

        # Include done tasks from earlier phases as resolved stubs
        for ph in self.data.get("phases", []):
            if ph.get("id") == phase_id:
                break
            for epic in ph.get("epics", []):
                for story in epic.get("stories", []):
                    for task in story.get("tasks", []):
                        if task.get("status") == "done":
                            result.append({
                                "id": task["id"],
                                "depends_on": [],
                                "scope": [],
                                "description": "",
                                "passes": True,
                                "blocked": False,
                            })

        # Current phase tasks
        for epic in phase.get("epics", []):
            for story in epic.get("stories", []):
                for task in story.get("tasks", []):
                    status = task.get("status", "pending")
                    result.append({
                        "id": task["id"],
                        "depends_on": task.get("depends_on", []),
                        "scope": task.get("scope", []),
                        "description": task.get("description", ""),
                        "passes": status == "done",
                        "blocked": status == "blocked",
                    })
        return result

    def is_phase_complete(self, phase_id: str) -> bool:
        """Return True if all tasks in the phase are done or blocked (or phase doesn't exist)."""
        for phase in self.data.get("phases", []):
            if phase.get("id") == phase_id:
                for epic in phase.get("epics", []):
                    for story in epic.get("stories", []):
                        for task in story.get("tasks", []):
                            if task.get("status", "pending") == "pending":
                                return False
                return True
        return False

    # -------------------------------------------------------------------------
    # DAG validation (feature 018)
    # -------------------------------------------------------------------------

    def validate_dag(self) -> dict:
        """Validate dependency graph.

        Checks for:
        1. References to non-existent task IDs (GAP-3)
        2. Cycles via DFS (color marking)
        3. Cross-phase backward dependencies: phase N task depending on phase M task (M > N) (GAP-14)

        Returns {"valid": bool, "errors": list[str]}
        """
        all_task_ids = {task["id"] for _, _, _, task in self._all_tasks()}
        errors = []

        # Build phase index map: task_id → phase_index
        task_phase = {}
        for i, phase in enumerate(self.data.get("phases", [])):
            for epic in phase.get("epics", []):
                for story in epic.get("stories", []):
                    for task in story.get("tasks", []):
                        task_phase[task["id"]] = i

        # Build adjacency: task_id → [dep_ids]
        adj = {}
        for _, _, _, task in self._all_tasks():
            tid = task["id"]
            deps = task.get("depends_on", [])
            adj[tid] = deps

            for dep in deps:
                if dep not in all_task_ids:
                    errors.append(f"Task '{tid}' depends on non-existent task '{dep}'")
                else:
                    # Check cross-phase backward dep
                    my_phase = task_phase.get(tid, -1)
                    dep_phase = task_phase.get(dep, -1)
                    if dep_phase > my_phase:
                        errors.append(
                            f"Task '{tid}' (phase {my_phase}) has backward cross-phase dep on "
                            f"'{dep}' (phase {dep_phase})"
                        )

        # Cycle detection via DFS (white=0, gray=1, black=2)
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {tid: WHITE for tid in all_task_ids}
        cycle_tasks = []

        def dfs(node: str, path: list) -> bool:
            color[node] = GRAY
            path.append(node)
            for neighbor in adj.get(node, []):
                if neighbor not in all_task_ids:
                    continue
                if color[neighbor] == GRAY:
                    # Found cycle
                    cycle_start = path.index(neighbor)
                    cycle_tasks.extend(path[cycle_start:])
                    return True
                if color[neighbor] == WHITE:
                    if dfs(neighbor, path):
                        return True
            path.pop()
            color[node] = BLACK
            return False

        for tid in list(all_task_ids):
            if color[tid] == WHITE:
                if dfs(tid, []):
                    break

        if cycle_tasks:
            errors.append(f"Cycle detected involving tasks: {cycle_tasks}")

        return {"valid": len(errors) == 0, "errors": errors}

    # -------------------------------------------------------------------------
    # Dependency check (feature 019)
    # -------------------------------------------------------------------------

    def are_deps_satisfied(self, task_id: str) -> bool:
        """Return True if all declared deps of task_id have status=done.

        Returns False for non-existent task.
        """
        task = self.get_task(task_id)
        if task is None:
            return False
        for dep_id in task.get("depends_on", []):
            dep = self.get_task(dep_id)
            if dep is None or dep.get("status") != "done":
                return False
        return True

    # -------------------------------------------------------------------------
    # Backward compat (feature 020)
    # -------------------------------------------------------------------------

    def ingest_feature_list(self, feature_list_path: Path, work_plan_path: Path) -> list[dict]:
        """Read feature_list.json and sync agent-marked statuses back into work_plan.

        The generator agent writes passes/blocked directly to feature_list.json.
        This reads those back and updates work_plan.json as source of truth.
        Returns list of dicts with {id, description, status} for each updated task.
        """
        data = atomic_read(feature_list_path)
        if data is None:
            return []
        fl_status = {}
        for f in data:
            if f.get("passes"):
                fl_status[f["id"]] = "done"
            elif f.get("blocked"):
                fl_status[f["id"]] = "blocked"
        updated = []
        for _, _, _, task in self._all_tasks():
            new_status = fl_status.get(task["id"])
            if new_status and task.get("status") != new_status:
                task["status"] = new_status
                updated.append({
                    "id": task["id"],
                    "description": task.get("description", ""),
                    "status": new_status,
                })
        if updated:
            self.save(work_plan_path)
        return updated

    def sync_feature_list(self, feature_list_path: Path) -> None:
        """Write a flat feature_list.json derived entirely from work_plan tasks.

        work_plan.json is the single source of truth. feature_list.json is a
        derived view — passes is True iff work_plan status == 'done'.
        Generators do NOT modify feature_list.json directly.
        """
        features = []
        priority = 1
        for phase, epic, story, task in self._all_tasks():
            features.append({
                "id": task["id"],
                "priority": priority,
                "category": phase.get("name", "feature"),
                "depends_on": task.get("depends_on", []),
                "description": task.get("description", ""),
                "acceptance_criteria": task.get("acceptance_criteria", []),
                "steps": task.get("steps", []),
                "passes": task.get("status") == "done",
                "blocked": task.get("status") == "blocked",
                "retries": task.get("attempts", 0),
            })
            priority += 1
        atomic_write(feature_list_path, features)

    @classmethod
    def from_flat_features(cls, features: list) -> "WorkPlan":
        """Build a single-phase WorkPlan from a flat feature_list.json array.

        Backward compatibility shim. Each feature becomes a task:
          - passes=True → status=done
          - blocked=True → status=blocked
          - else → status=pending
          - retries → attempts
        """
        tasks = []
        for feat in features:
            if feat.get("passes"):
                status = "done"
            elif feat.get("blocked"):
                status = "blocked"
            else:
                status = "pending"

            tasks.append({
                "id": feat.get("id", ""),
                "description": feat.get("description", ""),
                "acceptance_criteria": feat.get("acceptance_criteria", []),
                "steps": feat.get("steps", []),
                "depends_on": feat.get("depends_on", []),
                "status": status,
                "attempts": feat.get("retries", 0),
                "blocked_reason": feat.get("block_reason", None),
            })

        data = {
            "phases": [
                {
                    "id": "phase-0",
                    "name": "Features",
                    "epics": [
                        {
                            "id": "epic-001",
                            "name": "All Features",
                            "stories": [
                                {
                                    "id": "story-001",
                                    "name": "Feature Implementation",
                                    "tasks": tasks,
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        return cls(data)
