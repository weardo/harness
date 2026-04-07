"""
FleetSession -- Wave execution state management for parallel agent runs.

Tracks work queue, wave results, discoveries (for relay), requeued features,
and continuation state. All writes are atomic.
"""

from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

from .state import atomic_write, atomic_read


def fleet_dir(state_dir: Path) -> Path:
    """Return fleet/ directory inside the run's state_dir.

    Fleet state (session, briefs, wave results) is per-run — each run
    tracks its own parallel execution independently.
    """
    return Path(state_dir) / "fleet"


def _session_path(state_dir: Path) -> Path:
    """Return .harness/fleet/session.json path."""
    return fleet_dir(state_dir) / "session.json"


def _default_session() -> dict:
    """Return an empty default session structure."""
    return {
        "status": "inactive",
        "started_at": None,
        "current_wave": 0,
        "total_layers": 0,
        "work_queue": [],
        "requeued_features": [],
        "discoveries": [],
        "wave_results": {},
        "completed_features": [],
        "failed_features": [],
        "conflict_features": [],
    }


def load_session(state_dir: Path) -> dict:
    """Load fleet session. Returns default empty session if file doesn't exist."""
    data = atomic_read(_session_path(state_dir))
    if data is None:
        return _default_session()
    return data


def save_session(session: dict, state_dir: Path) -> None:
    """Save fleet session atomically."""
    atomic_write(_session_path(state_dir), session)


def init_session(
    state_dir: Path,
    total_layers: int,
    *,
    already_completed: list[str] | None = None,
    already_failed: list[str] | None = None,
) -> dict:
    """Initialize a new fleet session, pre-seeded with known state from work_plan.

    Pre-populating completed_features/failed_features ensures session.json is
    consistent with work_plan.json from the moment it is written — even if a
    previous session was killed mid-wave and left stale data on disk.

    Returns session dict. Also persists to disk immediately.
    """
    session = {
        "status": "active",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "current_wave": 0,
        "total_layers": total_layers,
        "work_queue": [],
        "requeued_features": [],
        "discoveries": [],
        "wave_results": {},
        "completed_features": list(already_completed or []),
        "failed_features": list(already_failed or []),
        "conflict_features": [],
    }
    save_session(session, state_dir)
    return session


def add_to_queue(session: dict, feature_id: str, desc: str, scope: list[str], wave: int) -> None:
    """Add a feature to the work queue."""
    session["work_queue"].append({
        "feature_id": feature_id,
        "desc": desc,
        "scope": scope,
        "wave": wave,
        "agent_id": None,
        "status": "pending",
    })


def start_wave(session: dict, wave: int, agent_ids: list[str]) -> None:
    """Record wave start. Updates current_wave, creates wave_results entry."""
    session["current_wave"] = wave
    session["wave_results"][str(wave)] = {
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "agents": {aid: {"feature_id": None, "status": "running", "brief_path": ""} for aid in agent_ids},
    }


def record_agent_result(session: dict, wave: int, agent_id: str, feature_id: str,
                        status: str, brief_path: str = "") -> None:
    """Record an individual agent's result within a wave.

    status: 'complete' | 'failed' | 'timeout' | 'conflict'
    """
    wave_key = str(wave)
    if wave_key not in session["wave_results"]:
        session["wave_results"][wave_key] = {
            "status": "running",
            "started_at": None,
            "completed_at": None,
            "agents": {},
        }
    session["wave_results"][wave_key]["agents"][agent_id] = {
        "feature_id": feature_id,
        "status": status,
        "brief_path": brief_path,
    }


def complete_wave(session: dict, wave: int) -> None:
    """Mark wave as completed. Summarize results."""
    wave_key = str(wave)
    if wave_key in session["wave_results"]:
        session["wave_results"][wave_key]["status"] = "complete"
        session["wave_results"][wave_key]["completed_at"] = datetime.now(timezone.utc).isoformat()


def mark_feature_complete(session: dict, feature_id: str) -> None:
    """Move feature to completed list."""
    if feature_id not in session["completed_features"]:
        session["completed_features"].append(feature_id)
    # Update work queue status
    for item in session["work_queue"]:
        if item["feature_id"] == feature_id:
            item["status"] = "complete"
            break


def mark_feature_conflict(session: dict, feature_id: str) -> None:
    """Move feature to conflict list and requeued list."""
    if feature_id not in session["conflict_features"]:
        session["conflict_features"].append(feature_id)
    if feature_id not in session["requeued_features"]:
        session["requeued_features"].append(feature_id)
    # Update work queue status
    for item in session["work_queue"]:
        if item["feature_id"] == feature_id:
            item["status"] = "conflict"
            break


def mark_feature_failed(session: dict, feature_id: str, reason: str = "") -> None:
    """Move feature to failed list."""
    if feature_id not in session["failed_features"]:
        session["failed_features"].append(feature_id)
    # Update work queue status
    for item in session["work_queue"]:
        if item["feature_id"] == feature_id:
            item["status"] = "failed"
            if reason:
                item["fail_reason"] = reason
            break


def requeue_feature(session: dict, feature_id: str, reason: str = "conflict") -> None:
    """Add feature ID to requeued list for next layer.

    If reason is 'conflict', also adds to conflict_features.
    """
    if feature_id not in session["requeued_features"]:
        session["requeued_features"].append(feature_id)
    if reason == "conflict" and feature_id not in session["conflict_features"]:
        session["conflict_features"].append(feature_id)


def add_discovery(session: dict, wave: int, discovery: str) -> None:
    """Add a discovery string to the session's accumulated discoveries."""
    session["discoveries"].append({
        "wave": wave,
        "discovery": discovery,
    })


def get_requeued_features(session: dict) -> list[str]:
    """Return list of feature IDs that need reprocessing."""
    return list(session["requeued_features"])


def clear_requeued(session: dict) -> None:
    """Clear the requeue list (after features have been rescheduled)."""
    session["requeued_features"] = []


def complete_session(session: dict) -> None:
    """Mark session as completed with timestamp."""
    session["status"] = "completed"
    session["completed_at"] = datetime.now(timezone.utc).isoformat()


def is_wave_all_failed(session: dict, wave: int) -> bool:
    """Check if ALL agents in a wave failed (no successes).

    Used for escalation -- if entire wave fails, stop and report.
    Returns True if every agent has status 'failed' or 'timeout',
    or if the wave has no agents recorded.
    """
    wave_key = str(wave)
    wave_data = session["wave_results"].get(wave_key)
    if not wave_data:
        return True
    agents = wave_data.get("agents", {})
    if not agents:
        return True
    return all(
        a.get("status") in ("failed", "timeout")
        for a in agents.values()
    )


def get_session_summary(session: dict) -> dict:
    """Return summary: {waves_completed, features_done, features_failed,
    features_requeued, discoveries_count}."""
    waves_completed = sum(
        1 for w in session.get("wave_results", {}).values()
        if w.get("status") == "complete"
    )
    return {
        "waves_completed": waves_completed,
        "features_done": len(session.get("completed_features", [])),
        "features_failed": len(session.get("failed_features", [])),
        "features_requeued": len(session.get("requeued_features", [])),
        "discoveries_count": len(session.get("discoveries", [])),
    }
