"""
Atomic JSON State Management
=============================

All state writes use temp file + flush + fsync + os.replace for crash safety.
Pattern from autonomous-coding-harness (GantisStorm).
"""

import asyncio
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically: temp file -> flush -> fsync -> replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp = tempfile.mkstemp(
        dir=path.parent,
        prefix=".harness_tmp_",
        suffix=".json",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_read(path: Path) -> Optional[dict]:
    """Read JSON file, returning None if file doesn't exist."""
    path = Path(path)
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


class RunRegistry:
    """Manages run lifecycle and index. No singleton — every run is isolated."""

    def __init__(self, harness_dir: Path):
        self.harness_dir = Path(harness_dir)
        self.harness_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir = self.harness_dir / "runs"
        self.runs_dir.mkdir(exist_ok=True)
        self.index_path = self.harness_dir / "runs.json"

    def _load_index(self) -> list:
        data = atomic_read(self.index_path)
        if data is None:
            return []
        return data.get("runs", [])

    def _save_index(self, runs: list) -> None:
        atomic_write(self.index_path, {"runs": runs})

    def _make_slug(self, prompt: str) -> str:
        """Sanitize prompt into a short slug for the run ID."""
        if not prompt:
            return ""
        slug = prompt.lower().strip()
        slug = re.sub(r"[^a-z0-9\s-]", "", slug)
        slug = re.sub(r"[\s-]+", "-", slug).strip("-")
        return slug[:40]

    def create_run(self, prompt: str = "") -> str:
        """Create a new run directory and index entry. Returns run_id."""
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y%m%dT%H%M%S")
        slug = self._make_slug(prompt)
        run_id = f"run-{ts}-{slug}" if slug else f"run-{ts}"

        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        runs = self._load_index()
        runs.append({
            "run_id": run_id,
            "prompt": prompt,
            "status": "in_progress",
            "started_at": now.isoformat(),
            "completed_at": None,
            "total_cost_usd": 0.0,
            "features_total": 0,
            "features_passing": 0,
        })
        self._save_index(runs)
        return run_id

    def run_dir(self, run_id: str) -> Path:
        """Get directory path for a run. Raises ValueError if not found."""
        d = self.runs_dir / run_id
        if not d.exists():
            raise ValueError(f"Run '{run_id}' not found in {self.runs_dir}")
        return d

    def list_runs(self) -> list:
        """List all runs from the index."""
        return self._load_index()

    def update_run(self, run_id: str, **updates) -> None:
        """Update fields on a run in the index."""
        runs = self._load_index()
        for run in runs:
            if run["run_id"] == run_id:
                run.update(updates)
                break
        self._save_index(runs)

    def find_resumable(self) -> Optional[str]:
        """Find the latest run with status 'in_progress'. Returns run_id or None."""
        runs = self._load_index()
        for run in reversed(runs):
            if run.get("status") == "in_progress":
                return run["run_id"]
        return None

    def migrate_legacy(self) -> Optional[str]:
        """Migrate old .harness/state/ layout to .harness/runs/. Returns run_id or None."""
        legacy_dir = self.harness_dir / "state"
        if not legacy_dir.is_dir():
            return None

        # Only migrate if state.json exists — empty state/ dirs are not real runs
        legacy_state = atomic_read(legacy_dir / "state.json")
        if legacy_state is None:
            # Empty dir (e.g., from old install.sh) — just remove it
            try:
                legacy_dir.rmdir()
            except OSError:
                pass  # Dir not empty with non-state files — leave it
            return None
        status = "complete" if legacy_state and legacy_state.get("planner_complete") else "in_progress"
        started_at = (legacy_state or {}).get("started_at", datetime.now(timezone.utc).isoformat())

        run_id = "run-legacy-migrated"
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        # Move all files from state/ to the new run dir
        for item in legacy_dir.iterdir():
            shutil.move(str(item), str(run_dir / item.name))

        # Remove the now-empty legacy dir
        legacy_dir.rmdir()

        # Add index entry
        runs = self._load_index()
        runs.append({
            "run_id": run_id,
            "prompt": "",
            "status": status,
            "started_at": started_at,
            "completed_at": None,
            "total_cost_usd": (legacy_state or {}).get("total_cost_usd", 0.0),
            "features_total": 0,
            "features_passing": 0,
        })
        self._save_index(runs)
        return run_id


class StateManager:
    """Manages all harness state files with atomic operations."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()  # Protects concurrent state writes during parallel runs

    @property
    def state_path(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def circuit_breaker_path(self) -> Path:
        return self.state_dir / "circuit_breaker.json"

    @property
    def exit_signals_path(self) -> Path:
        return self.state_dir / "exit_signals.json"

    @property
    def feature_list_path(self) -> Path:
        return self.state_dir / "feature_list.json"

    @property
    def feedback_path(self) -> Path:
        return self.state_dir / "feedback.md"

    @property
    def spec_path(self) -> Path:
        return self.state_dir / "spec.md"

    def load_state(self) -> dict:
        """Load orchestrator state, creating defaults if missing."""
        state = atomic_read(self.state_path)
        if state is None:
            state = self._default_state()
            self.save_state(state)
        return state

    def save_state(self, state: dict) -> None:
        """Save orchestrator state atomically."""
        atomic_write(self.state_path, state)

    def update_state(self, **updates) -> dict:
        """Load, update fields, save atomically. Returns updated state."""
        state = self.load_state()
        state.update(updates)
        self.save_state(state)
        return state

    async def update_state_async(self, **updates) -> dict:
        """Concurrent-safe state update for parallel runs. Uses asyncio.Lock."""
        async with self._lock:
            state = self.load_state()
            state.update(updates)
            self.save_state(state)
            return state

    async def mark_feature_passing_async(self, feature_id: str) -> None:
        """Concurrent-safe version of mark_feature_passing."""
        async with self._lock:
            self.mark_feature_passing(feature_id)

    async def mark_feature_blocked_async(self, feature_id: str, reason: str = "") -> None:
        """Concurrent-safe version of mark_feature_blocked."""
        async with self._lock:
            self.mark_feature_blocked(feature_id, reason)

    def load_feature_list(self) -> list:
        """Load feature list, returning empty list if missing."""
        data = atomic_read(self.feature_list_path)
        if data is None:
            return []
        if isinstance(data, list):
            return data
        return data.get("features", [])

    def save_feature_list(self, features: list) -> None:
        """Save feature list atomically."""
        atomic_write(self.feature_list_path, features)

    def count_features(self) -> dict:
        """Count feature statuses."""
        features = self.load_feature_list()
        total = len(features)
        passing = sum(1 for f in features if f.get("passes"))
        blocked = sum(1 for f in features if f.get("blocked"))
        remaining = total - passing - blocked
        return {
            "total": total,
            "passing": passing,
            "blocked": blocked,
            "remaining": remaining,
        }

    def get_next_feature(self) -> Optional[dict]:
        """Get highest priority feature that is not passing and not blocked."""
        features = self.load_feature_list()
        candidates = [
            f for f in features
            if not f.get("passes") and not f.get("blocked")
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda f: f.get("priority", 999))

    def mark_feature_passing(self, feature_id: str) -> None:
        """Mark a feature as passing."""
        features = self.load_feature_list()
        for f in features:
            if f.get("id") == feature_id:
                f["passes"] = True
                break
        self.save_feature_list(features)

    def mark_feature_blocked(self, feature_id: str, reason: str = "") -> None:
        """Mark a feature as blocked."""
        features = self.load_feature_list()
        for f in features:
            if f.get("id") == feature_id:
                f["blocked"] = True
                f["block_reason"] = reason
                break
        self.save_feature_list(features)

    def increment_retries(self, feature_id: str) -> int:
        """Increment retry count for a feature. Returns new count."""
        features = self.load_feature_list()
        for f in features:
            if f.get("id") == feature_id:
                f["retries"] = f.get("retries", 0) + 1
                self.save_feature_list(features)
                return f["retries"]
        return 0

    def load_exit_signals(self) -> dict:
        """Load exit signals rolling window."""
        data = atomic_read(self.exit_signals_path)
        if data is None:
            data = {
                "completion_signals": [],
                "blocked_signals": [],
                "error_signals": [],
            }
        return data

    def save_exit_signals(self, signals: dict) -> None:
        """Save exit signals atomically."""
        atomic_write(self.exit_signals_path, signals)

    def record_exit_signal(self, signal_type: str, iteration: int) -> None:
        """Record an exit signal, maintaining rolling window of 5."""
        signals = self.load_exit_signals()
        key = f"{signal_type}_signals"
        if key not in signals:
            signals[key] = []
        signals[key].append(iteration)
        signals[key] = signals[key][-5:]  # Keep last 5
        self.save_exit_signals(signals)

    def write_feedback(self, content: str) -> None:
        """Write evaluator feedback file."""
        self.feedback_path.parent.mkdir(parents=True, exist_ok=True)
        self.feedback_path.write_text(content)

    def read_feedback(self) -> Optional[str]:
        """Read evaluator feedback, returning None if missing."""
        if self.feedback_path.exists():
            return self.feedback_path.read_text()
        return None

    def clear_feedback(self) -> None:
        """Remove feedback file after generator addresses it."""
        if self.feedback_path.exists():
            self.feedback_path.unlink()

    # -------------------------------------------------------------------------
    # Planner State Machine
    # -------------------------------------------------------------------------

    def _default_planner_state(self) -> dict:
        return {
            "status": "pending",
            "started_at": None,
            "completed_at": None,
            "total_cost_usd": 0.0,
            "fix_loops_completed": 0,
            "max_fix_loops": 2,
            "roles": [],
        }

    def get_planner_state(self) -> dict:
        """Get structured planner state, creating defaults if missing."""
        state = self.load_state()
        return state.get("planner", self._default_planner_state())

    def _save_planner_state(self, planner: dict) -> None:
        """Save planner state into the main state dict."""
        state = self.load_state()
        state["planner"] = planner
        self.save_state(state)

    def start_planner(self) -> None:
        """Mark planner as in_progress with a start timestamp."""
        ps = self.get_planner_state()
        ps["status"] = "in_progress"
        ps["started_at"] = datetime.now(timezone.utc).isoformat()
        self._save_planner_state(ps)

    def complete_planner(self) -> None:
        """Mark planner as complete."""
        ps = self.get_planner_state()
        ps["status"] = "complete"
        ps["completed_at"] = datetime.now(timezone.utc).isoformat()
        self._save_planner_state(ps)
        # Keep backward compat field in sync
        self.update_state(planner_complete=True)

    def start_role(self, name: str, model: str = "", triggered_by: str = "") -> None:
        """Append a new role entry with status=running."""
        ps = self.get_planner_state()
        # Calculate attempt number for this role name
        attempt = sum(1 for r in ps["roles"] if r["name"] == name) + 1
        entry = {
            "name": name,
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "duration_s": 0,
            "cost_usd": 0.0,
            "model": model,
            "artifact": None,
            "artifact_size_bytes": 0,
            "attempt": attempt,
            "validation": None,
        }
        if triggered_by:
            entry["triggered_by"] = triggered_by
        ps["roles"].append(entry)
        self._save_planner_state(ps)

    def _find_latest_role(self, name: str, planner: dict) -> Optional[dict]:
        """Find the most recent role entry by name."""
        for role in reversed(planner["roles"]):
            if role["name"] == name:
                return role
        return None

    def complete_role(self, name: str, cost_usd: float = 0.0,
                      artifact: str = "", artifact_size_bytes: int = 0,
                      validation: Optional[dict] = None) -> None:
        """Mark the latest entry for this role as complete."""
        ps = self.get_planner_state()
        role = self._find_latest_role(name, ps)
        if role:
            now = datetime.now(timezone.utc).isoformat()
            role["status"] = "complete"
            role["completed_at"] = now
            role["cost_usd"] = cost_usd
            if artifact:
                role["artifact"] = artifact
            if artifact_size_bytes:
                role["artifact_size_bytes"] = artifact_size_bytes
            if validation is not None:
                role["validation"] = validation
            # Calculate duration
            if role.get("started_at"):
                started = datetime.fromisoformat(role["started_at"])
                ended = datetime.fromisoformat(now)
                role["duration_s"] = int((ended - started).total_seconds())
        ps["total_cost_usd"] += cost_usd
        self._save_planner_state(ps)

    def reject_role(self, name: str, cost_usd: float = 0.0,
                    artifact: str = "", validation: Optional[dict] = None) -> None:
        """Mark the latest entry for this role as rejected."""
        ps = self.get_planner_state()
        role = self._find_latest_role(name, ps)
        if role:
            now = datetime.now(timezone.utc).isoformat()
            role["status"] = "rejected"
            role["completed_at"] = now
            role["cost_usd"] = cost_usd
            if artifact:
                role["artifact"] = artifact
            if validation is not None:
                role["validation"] = validation
            if role.get("started_at"):
                started = datetime.fromisoformat(role["started_at"])
                ended = datetime.fromisoformat(now)
                role["duration_s"] = int((ended - started).total_seconds())
        ps["total_cost_usd"] += cost_usd
        self._save_planner_state(ps)

    def fail_role(self, name: str, reason: str = "") -> None:
        """Mark the latest entry for this role as failed."""
        ps = self.get_planner_state()
        role = self._find_latest_role(name, ps)
        if role:
            role["status"] = "failed"
            role["completed_at"] = datetime.now(timezone.utc).isoformat()
            role["error"] = reason
        self._save_planner_state(ps)

    def increment_fix_loops(self) -> int:
        """Increment fix loop counter. Returns new count."""
        ps = self.get_planner_state()
        ps["fix_loops_completed"] = ps.get("fix_loops_completed", 0) + 1
        self._save_planner_state(ps)
        return ps["fix_loops_completed"]

    def get_resume_point(self) -> dict:
        """Determine where to resume planner execution.

        Returns:
            {"action": "skip_planner"} — planner already complete
            {"action": "rerun", "role_name": str} — role crashed mid-run, re-run it
            {"action": "fix_loop", "role_name": str} — validator rejected, run refiner-fix
            {"action": "proceed_with_warning"} — max fix loops hit, proceed anyway
            {"action": "next_role"} — all existing roles complete, continue to next
        """
        ps = self.get_planner_state()

        if ps["status"] == "complete":
            return {"action": "skip_planner"}

        roles = ps["roles"]
        if not roles:
            return {"action": "next_role"}

        last_role = roles[-1]

        if last_role["status"] == "running":
            return {"action": "rerun", "role_name": last_role["name"]}

        if last_role["status"] == "rejected":
            if ps.get("fix_loops_completed", 0) >= ps.get("max_fix_loops", 2):
                return {"action": "proceed_with_warning"}
            return {"action": "fix_loop", "role_name": last_role["name"]}

        # All roles ended in complete or failed — move to next
        return {"action": "next_role"}

    def migrate_legacy_planner_state(self) -> None:
        """Migrate old planner_roles dict to structured planner state."""
        state = self.load_state()
        old_roles = state.get("planner_roles", {})
        if not old_roles or "planner" in state:
            return  # Already migrated or nothing to migrate

        ps = self._default_planner_state()
        if state.get("planner_complete"):
            ps["status"] = "complete"

        for name, done in old_roles.items():
            if done:
                ps["roles"].append({
                    "name": name,
                    "status": "complete",
                    "started_at": None,
                    "completed_at": None,
                    "duration_s": 0,
                    "cost_usd": 0.0,
                    "model": "",
                    "artifact": None,
                    "artifact_size_bytes": 0,
                    "attempt": 1,
                    "validation": None,
                })

        state["planner"] = ps
        self.save_state(state)

    def _default_state(self) -> dict:
        return {
            "phase": "init",
            "current_feature_id": None,
            "iteration": 0,
            "planner_complete": False,
            "generator_sessions": 0,
            "evaluator_sessions": 0,
            "evaluator_retries_current_feature": 0,
            "total_cost_usd": 0.0,
            "cost_breakdown": {
                "planner": 0.0,
                "generator": 0.0,
                "evaluator": 0.0,
            },
            "started_at": None,
            "last_updated": None,
        }
