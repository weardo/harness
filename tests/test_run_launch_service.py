"""Tests for background request run launching."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from src.core.run_registry import RunRegistry
from src.product.services.request_service import RequestService
from src.product.services.run_launch_service import RunLaunchService


class FakeProcess:
    def __init__(self, pid: int = 4242):
        self.pid = pid


def test_launch_request_run_persists_launch_record():
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        service = RunLaunchService(project_dir)

        with patch("src.product.services.run_launch_service.subprocess.Popen", return_value=FakeProcess(4242)) as popen:
            launch = service.launch_request_run(
                "req-123",
                config_path=project_dir / ".harness" / "config.yaml",
                overrides={"max_cost_usd": 5.0, "model": "gpt-5.4"},
            )

        assert launch["status"] == "running"
        assert launch["pid"] == 4242
        saved = service.get_launch(launch["id"])
        assert saved is not None
        assert saved["request_id"] == "req-123"
        args = popen.call_args.args[0]
        assert "--launch-id" in args
        assert "--model" in args
        assert "gpt-5.4" in args


def test_reconcile_marks_dead_running_launch_blocked():
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        request_service = RequestService(project_dir)
        request = request_service.create_request(
            project_id="demo",
            title="Build auth",
            description="Add auth flow",
            metadata={"active_launch_id": "launch-1"},
        )
        service = RunLaunchService(project_dir)
        launches_path = project_dir / ".harness" / "product" / "launches.json"
        launches_path.parent.mkdir(parents=True, exist_ok=True)
        launches_path.write_text(
            json.dumps(
                {
                    "launches": [
                        {
                            "id": "launch-1",
                            "request_id": request["id"],
                            "status": "running",
                            "pid": 999999,
                            "started_at": "2026-04-08T00:00:00+00:00",
                            "completed_at": None,
                            "config_path": None,
                            "log_path": "/tmp/launch-1.log",
                            "run_id": None,
                            "request_status_after_exit": None,
                            "error": None,
                            "exit_code": None,
                        }
                    ]
                }
            )
        )

        with patch("src.product.services.run_launch_service.os.kill", side_effect=OSError()):
            launches = service.reconcile(request_service)

        assert launches[0]["status"] == "failed"
        updated = request_service.get_request(request["id"])
        assert updated["status"] == "blocked"
        assert updated["metadata"]["launch_error"] == "Launch process exited before reporting completion."


def test_reconcile_marks_orphaned_running_request_blocked():
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        request_service = RequestService(project_dir)
        request = request_service.create_request(
            project_id="demo",
            title="Build auth",
            description="Add auth flow",
        )
        request_service.update_request(request["id"], status="running", metadata={})

        service = RunLaunchService(project_dir)
        service.reconcile(request_service)

        updated = request_service.get_request(request["id"])
        assert updated["status"] == "blocked"
        assert updated["metadata"]["launch_error"] == "Request was left running without an active launch."


def test_reconcile_marks_running_request_blocked_when_last_run_failed():
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        request_service = RequestService(project_dir)
        request = request_service.create_request(
            project_id="demo",
            title="Build auth",
            description="Add auth flow",
        )

        registry = RunRegistry(project_dir / ".harness")
        run_id = registry.create_run("Add auth flow")
        registry.update_run(run_id, status="failed", completed_at="2026-04-08T00:05:00+00:00")

        request_service.update_request(
            request["id"],
            status="running",
            metadata={"last_run_id": run_id, "last_launch_id": "launch-1"},
        )

        service = RunLaunchService(project_dir)
        service.reconcile(request_service)

        updated = request_service.get_request(request["id"])
        assert updated["status"] == "blocked"
        assert updated["metadata"]["last_run_id"] == run_id
        assert updated["metadata"]["launch_error"] == "Run ended without completing successfully."


def test_reconcile_marks_running_request_completed_when_last_run_completed():
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        request_service = RequestService(project_dir)
        request = request_service.create_request(
            project_id="demo",
            title="Build auth",
            description="Add auth flow",
        )

        registry = RunRegistry(project_dir / ".harness")
        run_id = registry.create_run("Add auth flow")
        registry.update_run(run_id, status="complete", completed_at="2026-04-08T00:05:00+00:00")

        request_service.update_request(
            request["id"],
            status="running",
            metadata={"last_run_id": run_id, "last_launch_id": "launch-1", "launch_error": "stale"},
        )

        service = RunLaunchService(project_dir)
        service.reconcile(request_service)

        updated = request_service.get_request(request["id"])
        assert updated["status"] == "completed"
        assert updated["metadata"]["last_run_id"] == run_id
        assert "launch_error" not in updated["metadata"]
