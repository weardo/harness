"""Tests for the product-facing run query service."""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.run_registry import RunRegistry
from src.product.services.run_query_service import RunQueryService


def test_get_run_surfaces_artifacts_state_and_feature_counts():
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        harness_dir = project_dir / ".harness"
        harness_dir.mkdir()
        registry = RunRegistry(harness_dir)
        run_id = registry.create_run("build auth")
        run_dir = registry.run_dir(run_id)

        (run_dir / "state.json").write_text(
            json.dumps(
                {
                    "phase": "generator",
                    "current_feature_id": "001",
                    "iteration": 2,
                    "planner_complete": True,
                }
            )
        )
        (run_dir / "feature_list.json").write_text(
            json.dumps(
                [
                    {"id": "001", "passes": True},
                    {"id": "002", "passes": False, "blocked": True},
                    {"id": "003", "passes": False},
                ]
            )
        )
        (run_dir / "spec.md").write_text("# Spec")

        service = RunQueryService(project_dir)
        run = service.get_run(run_id)

        assert run is not None
        assert run["state"]["phase"] == "generator"
        assert run["feature_counts"] == {"total": 3, "passing": 1, "blocked": 1, "remaining": 1}
        assert any(artifact["kind"] == "spec" for artifact in run["artifacts"])


def test_list_run_events_normalizes_log_entries():
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        harness_dir = project_dir / ".harness"
        harness_dir.mkdir()
        registry = RunRegistry(harness_dir)
        run_id = registry.create_run("build auth")
        run_dir = registry.run_dir(run_id)
        (run_dir / "logs").mkdir()
        (run_dir / "logs" / "run.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"ts": "2026-04-08T10:00:00+00:00", "event": "step", "context": "planner"}),
                    json.dumps(
                        {
                            "ts": "2026-04-08T10:01:00+00:00",
                            "event_type": "feature_pass",
                            "feature_id": "001",
                            "feature_desc": "Build auth",
                        }
                    ),
                ]
            )
            + "\n"
        )

        service = RunQueryService(project_dir)
        events = service.list_run_events(run_id)

        assert events[0]["kind"] == "step"
        assert events[0]["phase"] == "planner"
        assert events[1]["kind"] == "passed"
        assert events[1]["task_id"] == "001"


def test_list_runs_reconciles_abandoned_in_progress_runs():
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        harness_dir = project_dir / ".harness"
        harness_dir.mkdir()
        registry = RunRegistry(harness_dir)
        run_id = registry.create_run("build auth")
        run_dir = registry.run_dir(run_id)
        registry.update_run(
            run_id,
            started_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        )
        (run_dir / "state.json").write_text(
            json.dumps(
                {
                    "phase": "init",
                    "last_updated": None,
                    "planner_complete": False,
                    "planner": {
                        "status": "in_progress",
                        "roles": [{"name": "architect", "status": "running"}],
                    },
                }
            )
        )
        (run_dir / "logs").mkdir()
        (run_dir / "logs" / "run.jsonl").write_text(
            json.dumps({"ts": "2026-04-08T10:00:00+00:00", "event": "run_start"}) + "\n"
        )

        service = RunQueryService(project_dir)
        runs = service.list_runs()

        assert runs[0]["run_id"] == run_id
        assert runs[0]["status"] == "failed"
