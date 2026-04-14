"""Tests for surfaced run artifacts."""

import tempfile
from pathlib import Path

from src.adapters.storage.artifact_store import ArtifactStore
from src.core.run_registry import RunRegistry


def test_lists_known_run_artifacts():
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        harness_dir = project_dir / ".harness"
        harness_dir.mkdir()
        registry = RunRegistry(harness_dir)
        run_id = registry.create_run("build auth")
        run_dir = registry.run_dir(run_id)

        (run_dir / "spec.md").write_text("# Spec")
        (run_dir / "work_plan.json").write_text("{}")
        (run_dir / "logs").mkdir()
        (run_dir / "logs" / "run.jsonl").write_text("{}\n")
        (project_dir / "retrospective.json").write_text("{}")

        store = ArtifactStore()
        artifacts = store.list_run_artifacts(project_dir, run_id)
        kinds = {artifact["kind"] for artifact in artifacts}
        assert "spec" in kinds
        assert "work_plan" in kinds
        assert "run_log" in kinds
        assert "retrospective" in kinds
