"""Normalize current Harness/control-plane events into structured kernel events."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

try:
    from interfaces.execution_types import KernelEvent
except ImportError:  # pragma: no cover - package import path under pytest
    from src.interfaces.execution_types import KernelEvent


_SYSTEM_KEYS = {"type", "event_type", "feature_id", "feature_desc", "error", "ts"}


class ProductEventNormalizer:
    """Translate current control-plane event payloads into product-facing events."""

    def normalize(self, run_id: str, event: dict[str, Any]) -> KernelEvent:
        """Normalize one raw event payload into the shared KernelEvent shape."""
        event_type = str(event.get("event_type") or event.get("type") or "unknown")
        feature_id = event.get("feature_id")
        phase = self._phase_for(event_type, feature_id)
        kind = self._kind_for(event_type)
        message = self._message_for(event)
        task_id = self._task_id_for(feature_id)
        metadata = {k: v for k, v in event.items() if k not in _SYSTEM_KEYS}
        if feature_id is not None:
            metadata.setdefault("feature_id", feature_id)

        return KernelEvent(
            run_id=run_id,
            ts=str(event.get("ts") or datetime.now(timezone.utc).isoformat()),
            phase=phase,
            kind=kind,
            task_id=task_id,
            message=message,
            metadata=metadata,
        )

    def _phase_for(self, event_type: str, feature_id: Any) -> str:
        fid = str(feature_id or "")
        if event_type == "run_start" or fid.startswith("setup-"):
            return "setup"
        if fid.startswith("planner-"):
            return "planner"
        if fid.startswith("completion-") or event_type == "run_complete":
            return "completion"
        if event_type in {"wave_start", "wave_complete", "wave_all_failed", "agent_timeout", "merge_conflict"}:
            return "parallel"
        if fid == "circuit-breaker":
            return "circuit_breaker"
        return "execution"

    def _kind_for(self, event_type: str) -> str:
        mapping = {
            "feature_start": "started",
            "feature_pass": "passed",
            "feature_fail": "failed",
            "run_start": "started",
            "run_complete": "completed",
            "wave_start": "started",
            "wave_complete": "completed",
            "wave_all_failed": "failed",
            "agent_timeout": "failed",
            "merge_conflict": "failed",
        }
        return mapping.get(event_type, event_type)

    def _message_for(self, event: dict[str, Any]) -> str:
        return str(
            event.get("feature_desc")
            or event.get("error")
            or event.get("message")
            or event.get("type")
            or event.get("event_type")
            or "event"
        )

    def _task_id_for(self, feature_id: Any) -> str | None:
        if feature_id is None:
            return None
        fid = str(feature_id)
        if (
            fid.isdigit()
            or fid.startswith("task-")
            or fid.startswith("feature-")
        ):
            return fid
        return None
