"""Tests for the product-facing CLI."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from src.product.cli import main
from src.product.services.request_service import RequestService


class FakeRunService:
    def __init__(self):
        self.calls = []

    async def run_for_request(self, *, config_path, project_dir, request, cli_overrides=None):
        self.calls.append(
            {
                "config_path": config_path,
                "project_dir": project_dir,
                "request": request,
                "cli_overrides": cli_overrides,
            }
        )
        return {
            "run_id": "run-123",
            "status": "complete",
            "summary": "Harness run completed.",
            "cost_usd": 0.0,
            "duration_seconds": 0,
            "artifacts": [],
            "branch_name": None,
            "pr_url": None,
            "raw_result": {},
        }


def test_requests_create_and_list(capsys):
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)

        exit_code = main(
            [
                "--project-dir",
                str(project_dir),
                "requests",
                "create",
                "--title",
                "Build auth",
                "--description",
                "Add auth flow",
                "--acceptance-criterion",
                "Login works",
                "--link-path",
                "docs/spec.md",
            ]
        )

        assert exit_code == 0
        created = json.loads(capsys.readouterr().out)
        assert created["title"] == "Build auth"
        assert created["acceptance_criteria"] == ["Login works"]

        exit_code = main(["--project-dir", str(project_dir), "requests", "list"])
        assert exit_code == 0
        listed = json.loads(capsys.readouterr().out)
        assert len(listed) == 1
        assert listed[0]["linked_paths"] == ["docs/spec.md"]


def test_context_show_assembles_request_context(capsys):
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        spec_path = project_dir / "docs" / "spec.md"
        spec_path.parent.mkdir(parents=True)
        spec_path.write_text("# Spec")

        request_service = RequestService(project_dir)
        record = request_service.create_request(
            project_id="demo",
            title="Build auth",
            description="Add auth flow\nSupport sessions",
            acceptance_criteria=["Login works"],
            linked_paths=["docs/spec.md"],
        )

        exit_code = main(
            [
                "--project-dir",
                str(project_dir),
                "context",
                "show",
                "--request-id",
                record["id"],
            ]
        )

        assert exit_code == 0
        bundle = json.loads(capsys.readouterr().out)
        assert bundle["request_id"] == record["id"]
        assert bundle["request_summary"] == "Add auth flow"
        assert bundle["linked_artifacts"][0]["path"].endswith("docs/spec.md")


def test_runs_start_launches_run_for_request_and_updates_request(capsys, monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        harness_dir = project_dir / ".harness"
        harness_dir.mkdir()
        (harness_dir / "config.yaml").write_text("models: {}\n")

        request_service = RequestService(project_dir)
        record = request_service.create_request(
            project_id="demo",
            title="Build auth",
            description="Add auth flow",
        )
        fake_run_service = FakeRunService()

        monkeypatch.setattr("src.product.cli.maybe_detect_control_plane", lambda: None)

        exit_code = main(
            [
                "--project-dir",
                str(project_dir),
                "runs",
                "start",
                "--request-id",
                record["id"],
                "--max-cost",
                "5",
                "--model",
                "gpt-5.4",
            ],
            run_service=fake_run_service,
        )

        assert exit_code == 0
        result = json.loads(capsys.readouterr().out)
        assert result["run_id"] == "run-123"
        assert fake_run_service.calls[0]["cli_overrides"] == {
            "max_cost_usd": 5.0,
            "model": "gpt-5.4",
        }

        updated = request_service.get_request(record["id"])
        assert updated["status"] == "completed"
        assert updated["metadata"]["last_run_id"] == "run-123"


def test_runs_start_marks_request_blocked_when_run_fails(capsys, monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        harness_dir = project_dir / ".harness"
        harness_dir.mkdir()
        (harness_dir / "config.yaml").write_text("models: {}\n")

        request_service = RequestService(project_dir)
        record = request_service.create_request(
            project_id="demo",
            title="Build auth",
            description="Add auth flow",
        )

        class FailingRunService:
            async def run_for_request(self, **kwargs):
                raise RuntimeError("planner artifact missing")

        monkeypatch.setattr("src.product.cli.maybe_detect_control_plane", lambda: None)

        exit_code = main(
            [
                "--project-dir",
                str(project_dir),
                "runs",
                "start",
                "--request-id",
                record["id"],
                "--launch-id",
                "launch-123",
            ],
            run_service=FailingRunService(),
        )

        assert exit_code == 1
        updated = request_service.get_request(record["id"])
        assert updated["status"] == "blocked"
        assert updated["metadata"]["launch_error"] == "planner artifact missing"


def test_runs_start_marks_request_blocked_when_run_is_incomplete(capsys, monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        harness_dir = project_dir / ".harness"
        harness_dir.mkdir()
        (harness_dir / "config.yaml").write_text("models: {}\n")

        request_service = RequestService(project_dir)
        record = request_service.create_request(
            project_id="demo",
            title="Build auth",
            description="Add auth flow",
        )

        class IncompleteRunService:
            async def run_for_request(self, **kwargs):
                return {
                    "run_id": "run-123",
                    "status": "in_progress",
                    "summary": "0/1 tasks passing, 0 blocked, 1 remaining.",
                    "cost_usd": 0.0,
                    "duration_seconds": 0,
                    "artifacts": [],
                    "branch_name": None,
                    "pr_url": None,
                    "raw_result": {},
                }

        monkeypatch.setattr("src.product.cli.maybe_detect_control_plane", lambda: None)

        exit_code = main(
            [
                "--project-dir",
                str(project_dir),
                "runs",
                "start",
                "--request-id",
                record["id"],
            ],
            run_service=IncompleteRunService(),
        )

        assert exit_code == 0
        updated = request_service.get_request(record["id"])
        assert updated["status"] == "blocked"
        assert updated["metadata"]["last_run_id"] == "run-123"
        assert updated["metadata"]["launch_error"] == "0/1 tasks passing, 0 blocked, 1 remaining."
