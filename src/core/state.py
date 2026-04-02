"""
Atomic JSON State Management
=============================

All state writes use temp file + flush + fsync + os.replace for crash safety.
Pattern from autonomous-coding-harness (GantisStorm).
"""

import asyncio
import json
import os
import tempfile
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
