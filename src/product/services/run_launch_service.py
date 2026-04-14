"""Background launch service for request-backed Harness runs."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

try:
    from core.run_registry import RunRegistry
    from core.state import atomic_read, atomic_write
    from product.services.request_service import RequestService
except ImportError:  # pragma: no cover - package import path under pytest
    from src.core.run_registry import RunRegistry
    from src.core.state import atomic_read, atomic_write
    from src.product.services.request_service import RequestService


class RunLaunchService:
    """Launch Harness runs in background subprocesses and persist launch state."""

    def __init__(self, project_dir: Path):
        self.project_dir = Path(project_dir).resolve()
        self.product_dir = self.project_dir / ".harness" / "product"
        self.product_dir.mkdir(parents=True, exist_ok=True)
        self.launches_path = self.product_dir / "launches.json"
        self.launch_logs_dir = self.product_dir / "launch_logs"
        self.launch_logs_dir.mkdir(parents=True, exist_ok=True)

    def list_launches(self) -> list[dict[str, Any]]:
        data = atomic_read(self.launches_path)
        if data is None:
            return []
        return data.get("launches", [])

    def get_launch(self, launch_id: str) -> Optional[dict[str, Any]]:
        for launch in self.list_launches():
            if launch.get("id") == launch_id:
                return launch
        return None

    def launch_request_run(
        self,
        request_id: str,
        *,
        config_path: Optional[Path] = None,
        overrides: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        launch_id = f"launch-{uuid4().hex[:10]}"
        log_path = self.launch_logs_dir / f"{launch_id}.log"
        cmd = self._build_command(
            request_id=request_id,
            config_path=config_path,
            overrides=overrides or {},
            launch_id=launch_id,
        )

        with open(log_path, "ab") as log_file:
            process = subprocess.Popen(
                cmd,
                cwd=str(self.project_dir),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        record = {
            "id": launch_id,
            "request_id": request_id,
            "status": "running",
            "pid": process.pid,
            "started_at": self._now(),
            "completed_at": None,
            "config_path": str(config_path) if config_path else None,
            "log_path": str(log_path),
            "run_id": None,
            "request_status_after_exit": None,
            "error": None,
            "exit_code": None,
        }
        launches = self.list_launches()
        launches.append(record)
        self._save_launches(launches)
        return record

    def complete_launch(
        self,
        launch_id: str,
        *,
        request_status_after_exit: str,
        run_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._update_launch(
            launch_id,
            status="completed",
            completed_at=self._now(),
            request_status_after_exit=request_status_after_exit,
            run_id=run_id,
            error=None,
        )

    def fail_launch(
        self,
        launch_id: str,
        *,
        error: str,
        exit_code: Optional[int] = None,
        request_status_after_exit: str = "blocked",
    ) -> dict[str, Any]:
        return self._update_launch(
            launch_id,
            status="failed",
            completed_at=self._now(),
            request_status_after_exit=request_status_after_exit,
            error=error,
            exit_code=exit_code,
        )

    def reconcile(self, request_service: RequestService) -> list[dict[str, Any]]:
        launches = self.list_launches()
        registry = RunRegistry(self.project_dir / ".harness")
        runs_by_id = {
            str(run.get("run_id")): run
            for run in registry.list_runs()
            if run.get("run_id")
        }
        updated: list[dict[str, Any]] = []
        changed = False

        for launch in launches:
            if launch.get("status") != "running":
                updated.append(launch)
                continue

            pid = launch.get("pid")
            if pid and self._is_pid_alive(int(pid)):
                updated.append(launch)
                continue

            changed = True
            failure = dict(launch)
            failure.update(
                {
                    "status": "failed",
                    "completed_at": self._now(),
                    "request_status_after_exit": "blocked",
                    "error": "Launch process exited before reporting completion.",
                    "exit_code": launch.get("exit_code"),
                }
            )
            updated.append(failure)

            request = request_service.get_request(str(launch.get("request_id", "")))
            if request is None:
                continue

            metadata = dict(request.get("metadata", {}) or {})
            if metadata.get("active_launch_id") != launch.get("id"):
                continue

            metadata.pop("active_launch_id", None)
            metadata["last_launch_id"] = launch.get("id")
            metadata["launch_error"] = failure["error"]
            request_service.update_request(
                str(request.get("id")),
                status="blocked",
                metadata=metadata,
            )

        for request in request_service.list_requests():
            if request.get("status") != "running":
                continue
            metadata = dict(request.get("metadata", {}) or {})
            active_launch_id = metadata.get("active_launch_id")
            last_launch_id = metadata.get("last_launch_id")
            last_run_id = metadata.get("last_run_id")
            active_launch = next((launch for launch in updated if launch.get("id") == active_launch_id), None)
            last_launch = next((launch for launch in updated if launch.get("id") == last_launch_id), None)
            last_run = runs_by_id.get(str(last_run_id)) if last_run_id else None

            if active_launch and active_launch.get("status") == "running":
                continue

            if last_run is not None:
                run_status = str(last_run.get("status") or "")
                if run_status == "in_progress":
                    continue

                metadata.pop("active_launch_id", None)
                if run_status == "complete":
                    metadata.pop("launch_error", None)
                    request_service.update_request(
                        str(request.get("id")),
                        status="completed",
                        metadata=metadata,
                    )
                    continue

                metadata["launch_error"] = metadata.get("launch_error") or "Run ended without completing successfully."
                request_service.update_request(
                    str(request.get("id")),
                    status="blocked",
                    metadata=metadata,
                )
                continue

            if last_launch and last_launch.get("status") == "failed":
                metadata.pop("active_launch_id", None)
                metadata["launch_error"] = last_launch.get("error") or "Launch failed."
                request_service.update_request(
                    str(request.get("id")),
                    status="blocked",
                    metadata=metadata,
                )
                continue

            if active_launch_id:
                metadata.pop("active_launch_id", None)

            metadata["launch_error"] = metadata.get("launch_error") or "Request was left running without an active launch."
            request_service.update_request(
                str(request.get("id")),
                status="blocked",
                metadata=metadata,
            )

        if changed:
            self._save_launches(updated)
        return updated

    def _build_command(
        self,
        *,
        request_id: str,
        config_path: Optional[Path],
        overrides: dict[str, Any],
        launch_id: str,
    ) -> list[str]:
        cmd = [
            sys.executable,
            "src/product_cli.py",
            "--project-dir",
            str(self.project_dir),
            "runs",
            "start",
            "--request-id",
            request_id,
            "--launch-id",
            launch_id,
        ]
        if config_path is not None:
            cmd.extend(["--config", str(config_path)])
        if overrides.get("max_cost_usd") is not None:
            cmd.extend(["--max-cost", str(overrides["max_cost_usd"])])
        if overrides.get("max_duration_minutes") is not None:
            cmd.extend(["--max-duration", str(overrides["max_duration_minutes"])])
        if overrides.get("max_iterations") is not None:
            cmd.extend(["--max-iterations", str(overrides["max_iterations"])])
        if overrides.get("model"):
            cmd.extend(["--model", str(overrides["model"])])
        if overrides.get("planner_model"):
            cmd.extend(["--planner-model", str(overrides["planner_model"])])
        return cmd

    def _update_launch(self, launch_id: str, **updates: Any) -> dict[str, Any]:
        launches = self.list_launches()
        for launch in launches:
            if launch.get("id") == launch_id:
                launch.update(updates)
                self._save_launches(launches)
                return launch
        raise ValueError(f"Launch not found: {launch_id}")

    def _save_launches(self, launches: list[dict[str, Any]]) -> None:
        atomic_write(self.launches_path, {"launches": launches})

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
