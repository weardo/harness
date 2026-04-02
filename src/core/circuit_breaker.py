"""
Circuit Breaker — Nygard Pattern
=================================

CLOSED → HALF_OPEN → OPEN state machine for stagnation detection.
Based on ralph-claude-code (frankbria) implementation.
"""

import hashlib
import time
from pathlib import Path
from typing import Optional

from .state import atomic_read, atomic_write


class CircuitBreaker:
    """Detects stagnation in the harness loop and halts execution."""

    CLOSED = "CLOSED"
    HALF_OPEN = "HALF_OPEN"
    OPEN = "OPEN"

    def __init__(self, state_dir: Path, config: Optional[dict] = None):
        self.state_dir = Path(state_dir)
        self.state_path = self.state_dir / "circuit_breaker.json"
        config = config or {}
        self.no_progress_threshold = config.get("no_progress_threshold", 3)
        self.same_error_threshold = config.get("same_error_threshold", 3)
        self.output_decline_pct = config.get("output_decline_pct", 0.7)
        self.cooldown_seconds = config.get("cooldown_seconds", 300)
        self.max_open_count = config.get("max_open_count", 2)
        self._load()

    def _load(self) -> None:
        data = atomic_read(self.state_path)
        if data is None:
            data = self._default_data()
            atomic_write(self.state_path, data)
        self._data = data

    def _save(self) -> None:
        atomic_write(self.state_path, self._data)

    @property
    def state(self) -> str:
        return self._data["state"]

    @property
    def total_opens(self) -> int:
        return self._data["total_opens"]

    @property
    def is_permanently_halted(self) -> bool:
        return self._data["total_opens"] >= self.max_open_count

    def record_iteration(
        self,
        progress: bool,
        error_hash: Optional[str] = None,
        output_length: Optional[int] = None,
        iteration: Optional[int] = None,
    ) -> str:
        """Record an iteration result and return the new state."""
        self._load()  # Fresh state from disk

        old_state = self._data["state"]
        self._data["current_iteration"] = iteration or (
            self._data.get("current_iteration", 0) + 1
        )

        if progress:
            self._data["consecutive_no_progress"] = 0
            self._data["consecutive_same_error"] = 0
            self._data["last_progress_iteration"] = self._data["current_iteration"]
            self._data["peak_output_length"] = max(
                self._data.get("peak_output_length", 0), output_length or 0
            )
            if old_state == self.HALF_OPEN:
                self._transition(self.CLOSED, "progress_detected_in_half_open")
            elif old_state == self.OPEN:
                pass  # Shouldn't happen, but don't transition from OPEN on progress
            # CLOSED stays CLOSED
        else:
            self._data["consecutive_no_progress"] += 1

            # Check same error
            if error_hash:
                if error_hash == self._data.get("last_error_hash"):
                    self._data["consecutive_same_error"] += 1
                else:
                    self._data["consecutive_same_error"] = 1
                    self._data["last_error_hash"] = error_hash

            # Check output decline
            if output_length is not None:
                peak = self._data.get("peak_output_length", output_length)
                if peak > 0:
                    self._data["peak_output_length"] = max(peak, output_length)
                    if output_length < peak * self.output_decline_pct:
                        self._data["output_declining"] = True
                    else:
                        self._data["output_declining"] = False

            # State transitions
            if old_state == self.CLOSED:
                if self._should_open():
                    self._transition(self.OPEN, self._open_reason())
                elif self._data["consecutive_no_progress"] >= 2:
                    self._transition(self.HALF_OPEN, "no_progress_warning")
            elif old_state == self.HALF_OPEN:
                if self._should_open():
                    self._transition(self.OPEN, self._open_reason())
            # OPEN stays OPEN

        self._save()
        return self._data["state"]

    def check_cooldown(self) -> bool:
        """Check if OPEN state has cooled down enough for HALF_OPEN probe.
        Returns True if transition to HALF_OPEN occurred."""
        self._load()
        if self._data["state"] != self.OPEN:
            return False
        if self.is_permanently_halted:
            return False

        opened_at = self._data.get("opened_at", 0)
        if time.time() - opened_at >= self.cooldown_seconds:
            self._data["consecutive_no_progress"] = 0
            self._data["consecutive_same_error"] = 0
            self._transition(self.HALF_OPEN, "cooldown_elapsed")
            self._save()
            return True
        return False

    def reset(self) -> None:
        """Force reset to CLOSED. Use for manual recovery."""
        self._load()
        self._transition(self.CLOSED, "manual_reset")
        self._data["consecutive_no_progress"] = 0
        self._data["consecutive_same_error"] = 0
        self._data["output_declining"] = False
        self._save()

    def get_history(self) -> list:
        """Return transition history."""
        return self._data.get("history", [])

    def _should_open(self) -> bool:
        if self._data["consecutive_no_progress"] >= self.no_progress_threshold:
            return True
        if self._data["consecutive_same_error"] >= self.same_error_threshold:
            return True
        return False

    def _open_reason(self) -> str:
        reasons = []
        if self._data["consecutive_no_progress"] >= self.no_progress_threshold:
            reasons.append(
                f"no_progress x{self._data['consecutive_no_progress']}"
            )
        if self._data["consecutive_same_error"] >= self.same_error_threshold:
            reasons.append(
                f"same_error x{self._data['consecutive_same_error']}"
            )
        return " + ".join(reasons) if reasons else "unknown"

    def _transition(self, new_state: str, reason: str) -> None:
        old_state = self._data["state"]
        if old_state == new_state:
            return

        if new_state == self.OPEN:
            self._data["total_opens"] += 1
            self._data["opened_at"] = time.time()

        entry = {
            "iteration": self._data.get("current_iteration", 0),
            "from": old_state,
            "to": new_state,
            "reason": reason,
            "timestamp": time.time(),
        }
        self._data["history"].append(entry)
        self._data["history"] = self._data["history"][-20:]  # Keep last 20
        self._data["state"] = new_state
        self._data["reason"] = reason

    def _default_data(self) -> dict:
        return {
            "state": self.CLOSED,
            "consecutive_no_progress": 0,
            "consecutive_same_error": 0,
            "last_error_hash": None,
            "last_progress_iteration": 0,
            "peak_output_length": 0,
            "output_declining": False,
            "total_opens": 0,
            "current_iteration": 0,
            "opened_at": None,
            "reason": None,
            "history": [],
        }


def hash_error(error_text: str) -> str:
    """Create a stable hash of an error message for same-error detection."""
    # Normalize: strip whitespace, lowercase, remove line numbers/timestamps
    import re
    normalized = error_text.strip().lower()
    normalized = re.sub(r"line \d+", "line N", normalized)
    normalized = re.sub(r"\d{4}-\d{2}-\d{2}", "DATE", normalized)
    normalized = re.sub(r"\d{2}:\d{2}:\d{2}", "TIME", normalized)
    return hashlib.md5(normalized.encode()).hexdigest()[:12]
