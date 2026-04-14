"""Run registry for top-level run metadata.

This module is the first step toward separating product-facing run metadata
from the kernel's crash-safe local execution state.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .state import atomic_read, atomic_write


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
        runs.append(
            {
                "run_id": run_id,
                "prompt": prompt,
                "status": "in_progress",
                "started_at": now.isoformat(),
                "completed_at": None,
                "total_cost_usd": 0.0,
                "features_total": 0,
                "features_passing": 0,
            }
        )
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

        legacy_state = atomic_read(legacy_dir / "state.json")
        if legacy_state is None:
            try:
                legacy_dir.rmdir()
            except OSError:
                pass
            return None

        status = "complete" if legacy_state and legacy_state.get("planner_complete") else "in_progress"
        started_at = (legacy_state or {}).get("started_at", datetime.now(timezone.utc).isoformat())

        run_id = "run-legacy-migrated"
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        for item in legacy_dir.iterdir():
            shutil.move(str(item), str(run_dir / item.name))

        legacy_dir.rmdir()

        runs = self._load_index()
        runs.append(
            {
                "run_id": run_id,
                "prompt": "",
                "status": status,
                "started_at": started_at,
                "completed_at": None,
                "total_cost_usd": (legacy_state or {}).get("total_cost_usd", 0.0),
                "features_total": 0,
                "features_passing": 0,
            }
        )
        self._save_index(runs)
        return run_id
