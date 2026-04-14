"""Product-facing read service for Harness run metadata, details, and events."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from adapters.events.event_normalizer import ProductEventNormalizer
    from adapters.storage.artifact_store import ArtifactStore
    from core.run_registry import RunRegistry
    from core.state import atomic_read
    from product.services.run_launch_service import RunLaunchService
except ImportError:  # pragma: no cover - package import path under pytest
    from src.adapters.events.event_normalizer import ProductEventNormalizer
    from src.adapters.storage.artifact_store import ArtifactStore
    from src.core.run_registry import RunRegistry
    from src.core.state import atomic_read
    from src.product.services.run_launch_service import RunLaunchService


class RunQueryService:
    """Read normalized run data from the current local-first Harness layout."""

    def __init__(self, project_dir: Path):
        self.project_dir = Path(project_dir)
        self.harness_dir = self.project_dir / ".harness"
        self.registry = RunRegistry(self.harness_dir)
        self.artifact_store = ArtifactStore()
        self.event_normalizer = ProductEventNormalizer()
        self.launch_service = RunLaunchService(self.project_dir)

    def list_runs(self) -> list[dict[str, Any]]:
        """List all indexed runs, newest first."""
        self.reconcile_runs()
        runs = list(self.registry.list_runs())
        runs.reverse()
        return [self._run_summary(run) for run in runs]

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        """Return an enriched view of one run or `None` if it does not exist."""
        self.reconcile_runs()
        run = next((item for item in self.registry.list_runs() if item.get("run_id") == run_id), None)
        if run is None:
            return None

        summary = self._run_summary(run)
        run_dir = self._run_dir_or_none(run_id)
        if run_dir is None:
            summary["artifacts"] = []
            summary["state"] = {}
            return summary

        state = atomic_read(run_dir / "state.json") or {}
        feature_counts = self._feature_counts(run_dir)
        summary["artifacts"] = self.artifact_store.list_run_artifacts(self.project_dir, run_id)
        summary["state"] = {
            "phase": state.get("phase"),
            "current_feature_id": state.get("current_feature_id"),
            "iteration": state.get("iteration"),
            "planner_complete": state.get("planner_complete"),
        }
        summary["feature_counts"] = feature_counts
        return summary

    def reconcile_runs(self) -> list[dict[str, Any]]:
        """Downgrade abandoned `in_progress` runs that no longer have an active launch."""
        runs = self.registry.list_runs()
        if not runs:
            return []

        active_launches = {
            launch.get("run_id")
            for launch in self.launch_service.list_launches()
            if launch.get("status") == "running" and launch.get("run_id")
        }

        reconciled: list[dict[str, Any]] = []
        for run in runs:
            if run.get("status") != "in_progress":
                reconciled.append(run)
                continue

            run_id = str(run.get("run_id", ""))
            if run_id in active_launches:
                reconciled.append(run)
                continue

            run_dir = self._run_dir_or_none(run_id)
            state = atomic_read(run_dir / "state.json") if run_dir else {}
            if self._looks_abandoned(run, state or {}, run_dir):
                self.registry.update_run(
                    run_id,
                    status="failed",
                    completed_at=self._now(),
                )
                run = next((item for item in self.registry.list_runs() if item.get("run_id") == run_id), run)
            reconciled.append(run)

        return reconciled

    def list_run_events(self, run_id: str) -> list[dict[str, Any]]:
        """Return normalized events for a run, newest last."""
        run_dir = self._run_dir_or_none(run_id)
        if run_dir is None:
            return []

        log_path = run_dir / "logs" / "run.jsonl"
        if not log_path.exists():
            return []

        events: list[dict[str, Any]] = []
        for line in log_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(self._normalize_log_event(run_id, entry))
        return events

    def _run_summary(self, run: dict[str, Any]) -> dict[str, Any]:
        total = int(run.get("features_total", 0) or 0)
        passing = int(run.get("features_passing", 0) or 0)
        remaining = max(total - passing, 0)
        return {
            "run_id": run.get("run_id", ""),
            "status": run.get("status", "unknown"),
            "prompt": run.get("prompt", ""),
            "started_at": run.get("started_at"),
            "completed_at": run.get("completed_at"),
            "total_cost_usd": float(run.get("total_cost_usd", 0.0) or 0.0),
            "features_total": total,
            "features_passing": passing,
            "features_remaining": remaining,
        }

    def _looks_abandoned(self, run: dict[str, Any], state: dict[str, Any], run_dir: Optional[Path]) -> bool:
        started_at = self._parse_ts(run.get("started_at"))
        if started_at is None:
            return False
        if datetime.now(timezone.utc) - started_at < timedelta(minutes=2):
            return False

        if state.get("last_updated"):
            return False
        if state.get("phase") not in {None, "init"}:
            return False

        planner = state.get("planner", {}) or {}
        roles = planner.get("roles", []) or []
        if roles and any(role.get("status") not in {"running", None} for role in roles):
            return False

        log_path = run_dir / "logs" / "run.jsonl" if run_dir else None
        if log_path and log_path.exists():
            lines = [line for line in log_path.read_text().splitlines() if line.strip()]
            if len(lines) > 1:
                return False

        return True

    def _feature_counts(self, run_dir: Path) -> dict[str, int]:
        features = atomic_read(run_dir / "feature_list.json")
        if not isinstance(features, list):
            return {"total": 0, "passing": 0, "blocked": 0, "remaining": 0}

        total = len(features)
        passing = sum(1 for item in features if item.get("passes"))
        blocked = sum(1 for item in features if item.get("blocked"))
        return {
            "total": total,
            "passing": passing,
            "blocked": blocked,
            "remaining": max(total - passing - blocked, 0),
        }

    def _run_dir_or_none(self, run_id: str) -> Optional[Path]:
        try:
            return self.registry.run_dir(run_id)
        except ValueError:
            return None

    def _normalize_log_event(self, run_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        if entry.get("type") or entry.get("event_type"):
            return self.event_normalizer.normalize(run_id, entry)

        event_name = str(entry.get("event") or "event")
        message = str(entry.get("message") or entry.get("context") or event_name)
        metadata = {
            key: value
            for key, value in entry.items()
            if key not in {"ts", "event", "message", "context"}
        }
        phase = str(entry.get("context") or "execution")
        return {
            "run_id": run_id,
            "ts": str(entry.get("ts") or ""),
            "phase": phase,
            "kind": event_name,
            "task_id": None,
            "message": message,
            "metadata": metadata,
        }

    @staticmethod
    def _parse_ts(value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
