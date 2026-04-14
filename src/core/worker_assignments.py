"""
Worker Assignments -- Durable worktree-to-task mapping for crash recovery.

Persists to fleet/worker_assignments.json inside the run's state_dir.
Unlike coordination files (swept on resume), this file survives kills
and drives worktree-aware resume.
"""

from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from .state import atomic_write, atomic_read


def _assignments_path(state_dir: Path) -> Path:
    """Return fleet/worker_assignments.json path."""
    return Path(state_dir) / "fleet" / "worker_assignments.json"


def load_assignments(state_dir: Path) -> dict:
    """Load assignments. Returns empty structure if file doesn't exist."""
    data = atomic_read(_assignments_path(state_dir))
    if data is None:
        return {"assignments": {}}
    return data


def save_assignments(data: dict, state_dir: Path) -> None:
    """Save assignments atomically."""
    atomic_write(_assignments_path(state_dir), data)


def add_assignment(
    state_dir: Path,
    worker_id: str,
    feature_id: str,
    branch: str,
    worktree_dir: str,
    agent_id: str,
    wave: int,
    scope: list[str],
) -> None:
    """Record a worker assignment. Called before agent launch."""
    data = load_assignments(state_dir)
    now = datetime.now(timezone.utc).isoformat()
    data["assignments"][worker_id] = {
        "feature_id": feature_id,
        "branch": branch,
        "worktree_dir": worktree_dir,
        "agent_id": agent_id,
        "wave": wave,
        "scope": scope,
        "assigned_at": now,
        "status": "running",
        "phase": "generator",
        "gen_session_id": "",
        "eval_session_id": "",
        "live_state": "assigned",
        "live_state_at": now,
    }
    save_assignments(data, state_dir)


def remove_assignment(state_dir: Path, worker_id: str) -> None:
    """Remove a worker assignment. Called after successful merge + cleanup."""
    data = load_assignments(state_dir)
    data["assignments"].pop(worker_id, None)
    save_assignments(data, state_dir)


def update_assignment_status(
    state_dir: Path, worker_id: str, status: str
) -> None:
    """Update assignment status (running → completed | interrupted | failed)."""
    data = load_assignments(state_dir)
    if worker_id in data["assignments"]:
        data["assignments"][worker_id]["status"] = status
        save_assignments(data, state_dir)


def update_assignment_phase(
    state_dir: Path, worker_id: str, phase: str
) -> None:
    """Update assignment phase (generator → evaluator). Tracks current agent type."""
    data = load_assignments(state_dir)
    if worker_id in data["assignments"]:
        data["assignments"][worker_id]["phase"] = phase
        save_assignments(data, state_dir)


def update_assignment_live_state(
    state_dir: Path, worker_id: str, live_state: str
) -> None:
    """Update the worker's real-time live_state and timestamp.

    Unlike `status` and `phase` (which feed into recovery semantics), this
    field is purely observational. The orchestrator updates it at every
    transition (gen-running → gen-done → eval-running → eval-done → merging
    → ...) so observers like status.py can show what's actually happening
    without waiting for the next iteration boundary.

    Recovery logic ignores this field.
    """
    data = load_assignments(state_dir)
    if worker_id in data["assignments"]:
        data["assignments"][worker_id]["live_state"] = live_state
        data["assignments"][worker_id]["live_state_at"] = (
            datetime.now(timezone.utc).isoformat()
        )
        save_assignments(data, state_dir)


def update_assignment_session_id(
    state_dir: Path, worker_id: str, session_id: str, phase: str = "generator"
) -> None:
    """Store the agent's CLI session ID so crash recovery can --resume it.

    Args:
        phase: "generator" or "evaluator" — determines which field is updated.

    On eval-fail retry, the orchestrator reads gen_session_id back and passes it
    to run_agent_session(resume_session_id=...) so the agent keeps full
    conversation context from the prior run.

    On crash recovery, the orchestrator reads the session_id for whichever
    phase was active (gen_session_id or eval_session_id) and resumes that
    exact agent session.
    """
    data = load_assignments(state_dir)
    if worker_id in data["assignments"]:
        key = "eval_session_id" if phase == "evaluator" else "gen_session_id"
        data["assignments"][worker_id][key] = session_id
        save_assignments(data, state_dir)


def mark_all_running_as_interrupted(state_dir: Path) -> list[str]:
    """Mark all 'running' assignments as 'interrupted'. Returns affected worker IDs.

    Called during hard stop (second Ctrl+C).
    """
    data = load_assignments(state_dir)
    affected = []
    for worker_id, assignment in data["assignments"].items():
        if assignment["status"] == "running":
            assignment["status"] = "interrupted"
            affected.append(worker_id)
    if affected:
        save_assignments(data, state_dir)
    return affected


def get_resumable_assignments(state_dir: Path) -> dict[str, dict]:
    """Return assignments that can be resumed.

    Includes 'running', 'interrupted', 'failed', and 'merge_failed':
    - running/interrupted: orchestrator killed mid-work
    - failed: eval failed with commits, worth retrying generator
    - merge_failed: eval passed but merge hit conflicts; needs merge-retry,
      not another full eval cycle (see resume_interrupted_workers for the
      merge-only retry path)
    """
    data = load_assignments(state_dir)
    return {
        worker_id: assignment
        for worker_id, assignment in data["assignments"].items()
        if assignment["status"] in ("running", "interrupted", "failed", "merge_failed")
    }


def has_assignment_for_worktree(state_dir: Path, worktree_dir: str) -> bool:
    """Check if any assignment points to this worktree. Used by sweep guard."""
    data = load_assignments(state_dir)
    return any(
        a["worktree_dir"] == worktree_dir
        for a in data["assignments"].values()
    )
