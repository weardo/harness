"""Tests for the initial brownfield kernel bootstrap path."""

import json
import tempfile
from pathlib import Path

from src.core.orchestrator import _bootstrap_brownfield_scoped_run
from src.core.state import StateManager


def test_bootstrap_brownfield_scoped_run_writes_minimal_work_plan_and_spec():
    with tempfile.TemporaryDirectory() as d:
        state_dir = Path(d)
        state_mgr = StateManager(state_dir)
        execution_request = {
            "objective": "Show request count in hero",
            "context_bundle": {
                "prompt": "Show request count in hero",
                "request": {
                    "id": "req-1",
                    "title": "Show request count in hero",
                    "description": "Update the hero to include request count.",
                    "linked_paths": ["src/product/static/app.js"],
                    "acceptance_criteria": ["Hero shows request count"],
                },
                "assembled_context": {
                    "request_summary": "Update the hero to include request count.",
                    "acceptance_criteria": ["Hero shows request count"],
                    "linked_paths": ["src/product/static/app.js"],
                },
            },
        }

        work_plan = _bootstrap_brownfield_scoped_run(
            state_dir=state_dir,
            state_mgr=state_mgr,
            execution_request=execution_request,
        )

        assert work_plan.count_tasks()["total"] == 1
        work_plan_json = json.loads((state_dir / "work_plan.json").read_text())
        task = work_plan_json["phases"][0]["epics"][0]["stories"][0]["tasks"][0]
        assert task["scope"] == ["src/product/static/app.js"]
        assert task["acceptance_criteria"] == ["Hero shows request count"]

        features = json.loads((state_dir / "feature_list.json").read_text())
        assert features[0]["id"] == "task-001"
        assert features[0]["passes"] is False

        spec_text = (state_dir / "spec.md").read_text()
        assert "Scoped Request Spec" in spec_text
        assert "src/product/static/app.js" in spec_text

        state = state_mgr.load_state()
        assert state["planner_complete"] is True
        assert state["phase"] == "generator"
