"""Integration tests — StateManager + CostTracker lifecycle.

Steps 1-8: Real file I/O with temp directories. No mocks.
"""

import json
import tempfile
from pathlib import Path

import pytest

from src.core.state import StateManager
from src.core.cost_tracker import CostTracker
from src.core.completion import check_completion, check_suspicion
from src.core.orchestrator import validate_planner_output


@pytest.fixture
def harness_dir(tmp_path):
    """Temp directory simulating a harness root (state/ lives inside it)."""
    d = tmp_path / "harness"
    d.mkdir()
    return d


class TestStateLifecycle:
    def test_state_lifecycle(self, harness_dir):
        """StateManager + CostTracker full lifecycle — real file I/O, no mocks."""
        # Step 2: Initialize StateManager with temp state_dir
        state_mgr = StateManager(harness_dir / "state")
        state = state_mgr.load_state()
        assert state["phase"] == "init"

        # Step 3: Create feature_list.json with 3 features
        features = [
            {"id": "001", "description": "First feature", "passes": False, "blocked": False, "priority": 1},
            {"id": "002", "description": "Second feature", "passes": False, "blocked": False, "priority": 2},
            {"id": "003", "description": "Third feature", "passes": False, "blocked": False, "priority": 3},
        ]
        state_mgr.save_feature_list(features)
        assert (harness_dir / "state" / "feature_list.json").exists()

        # Step 4: Mark 2 features passing — verify count_features shows 1 remaining
        state_mgr.mark_feature_passing("001")
        state_mgr.mark_feature_passing("002")
        counts = state_mgr.count_features()
        assert counts["total"] == 3
        assert counts["passing"] == 2
        assert counts["remaining"] == 1

        # Step 5: CostTracker — record costs including per-feature
        ct = CostTracker()
        ct.record("generator", 5.0)
        ct.record("evaluator", 2.0)
        ct.record_feature("001", "generator", 3.0)
        ct.record_feature("002", "evaluator", 2.0)
        assert ct.total == 7.0
        assert ct.feature_costs == {"001": 3.0, "002": 2.0}

        # Step 6: Check completion — should be incomplete (1 remaining)
        result = check_completion(harness_dir)
        assert result["complete"] is False
        assert result["progress"] is True  # 2 features passing = progress made
        assert "remaining" in result["reason"]


class TestCompletionAndSuspicion:
    def test_completion_and_suspicion(self, harness_dir):
        """Completion detection + suspicion heuristic — real file I/O, no mocks."""
        # Setup: StateManager with 3 features, 2 already passing
        state_mgr = StateManager(harness_dir / "state")
        features = [
            {"id": "001", "description": "First feature", "passes": True, "blocked": False, "priority": 1, "retries": 0},
            {"id": "002", "description": "Second feature", "passes": True, "blocked": False, "priority": 2, "retries": 0},
            {"id": "003", "description": "Third feature", "passes": False, "blocked": False, "priority": 3, "retries": 0},
        ]
        state_mgr.save_feature_list(features)

        # Step 2: Mark remaining feature passing — verify count shows 0 remaining
        state_mgr.mark_feature_passing("003")
        counts = state_mgr.count_features()
        assert counts["remaining"] == 0
        assert counts["passing"] == 3

        # Step 3: Record 2 completion signals
        state_mgr.record_exit_signal("completion", 1)
        state_mgr.record_exit_signal("completion", 2)

        # Step 4: Check completion — verify complete=True
        result = check_completion(harness_dir)
        assert result["complete"] is True
        assert result["progress"] is True

        # Step 5: Run check_suspicion — 0 retries + 0 CB opens → suspicious=True
        config = {"suspicion": {"enabled": True, "min_avg_feature_seconds": 30}}
        suspicion = check_suspicion(state_mgr, config, cb_total_opens=0)
        assert suspicion["suspicious"] is True
        assert len(suspicion["reasons"]) > 0


class TestCostSummaryAndStateValidity:
    def test_cost_summary_and_state_validity(self, harness_dir):
        """Cost summary formatting + state file validity (steps 11-12) — real file I/O, no mocks."""
        state_dir = harness_dir / "state"

        # Step 2: Create CostTracker with per-feature costs
        ct = CostTracker()
        ct.record("generator", 4.0)
        ct.record("evaluator", 1.5)
        ct.record_feature("001", "generator", 2.5)
        ct.record_feature("002", "generator", 1.5)
        ct.record_feature("003", "evaluator", 0.75)

        # Step 3: Format summary and assert feature IDs appear
        summary = ct.format_summary()
        assert "001" in summary
        assert "002" in summary
        assert "003" in summary
        assert "Per-feature costs:" in summary

        # Step 4: Save all state files via StateManager
        state_mgr = StateManager(state_dir)
        state = state_mgr.load_state()
        state["cost_breakdown"] = ct.to_dict()
        state_mgr.save_state(state)

        features = [
            {"id": "001", "description": "First feature", "passes": True, "blocked": False, "priority": 1},
            {"id": "002", "description": "Second feature", "passes": True, "blocked": False, "priority": 2},
            {"id": "003", "description": "Third feature", "passes": False, "blocked": False, "priority": 3},
        ]
        state_mgr.save_feature_list(features)
        state_mgr.record_exit_signal("completion", 1)

        # Step 5: Read each JSON file and verify json.loads works
        json_files = list(state_dir.glob("*.json"))
        assert len(json_files) >= 2  # at minimum state.json + feature_list.json

        for json_file in json_files:
            raw = json_file.read_text()
            parsed = json.loads(raw)
            assert parsed is not None, f"{json_file.name} parsed to None"

        # Verify state.json and feature_list.json are present and parseable
        state_json = state_dir / "state.json"
        feature_list_json = state_dir / "feature_list.json"
        assert state_json.exists()
        assert feature_list_json.exists()

        parsed_state = json.loads(state_json.read_text())
        assert "phase" in parsed_state
        assert "cost_breakdown" in parsed_state

        parsed_features = json.loads(feature_list_json.read_text())
        assert isinstance(parsed_features, list)
        assert len(parsed_features) == 3


class TestPlannerValidation:
    def test_planner_validation(self, harness_dir):
        """Planner validation (steps 9-10) — real file I/O, no mocks."""
        state_dir = harness_dir / "state"
        state_dir.mkdir()

        # Step 2: Write feature_list.json so the first check (missing SPEC_GAPS.md) is reached
        features = [
            {"id": "001", "description": "First feature", "passes": False, "blocked": False, "priority": 1},
        ]
        (state_dir / "feature_list.json").write_text(json.dumps(features))

        # Validate without SPEC_GAPS.md — should fail
        result = validate_planner_output(state_dir)
        assert result["valid"] is False
        assert "SPEC_GAPS.md" in result["reason"]

        # Step 3: Create SPEC_GAPS.md with substantive content (>50 chars)
        gaps_content = "Gap 1: Missing acceptance criteria for error states. Gap 2: Timeout behavior unspecified."
        assert len(gaps_content) > 50
        (state_dir / "SPEC_GAPS.md").write_text(gaps_content)

        # Step 4: Validate again — should pass
        result2 = validate_planner_output(state_dir)
        assert result2["valid"] is True


class TestWorkPlanGeneratorLoop:
    """Feature 048: WorkPlan generator loop, phase transitions, auto-conversion, DAG failure."""

    def test_auto_conversion_from_feature_list(self, harness_dir):
        """If work_plan.json missing but feature_list.json exists, auto-conversion occurs."""
        import json
        from src.core.work_plan import WorkPlan

        state_dir = harness_dir / "state"
        state_dir.mkdir()

        features = [
            {"id": "001", "description": "Feature A", "acceptance_criteria": ["ac"],
             "steps": [], "depends_on": [], "passes": False, "blocked": False, "retries": 0},
            {"id": "002", "description": "Feature B", "acceptance_criteria": ["ac"],
             "steps": [], "depends_on": [], "passes": True, "blocked": False, "retries": 0},
        ]
        (state_dir / "feature_list.json").write_text(json.dumps(features))

        # Simulate what orchestrator does: convert feature_list to work_plan
        work_plan_path = state_dir / "work_plan.json"
        wp = WorkPlan.from_flat_features(features)
        wp.save(work_plan_path)

        assert work_plan_path.exists()
        wp2 = WorkPlan.load(work_plan_path)
        assert wp2.count_tasks()["total"] == 2
        assert wp2.get_task("002")["status"] == "done"
        assert wp2.get_task("001")["status"] == "pending"

    def test_work_plan_phase_gate_enforced(self, harness_dir):
        """Phase 0 tasks must complete before Phase 1 tasks are returned by get_next_task."""
        import json
        from src.core.work_plan import WorkPlan

        state_dir = harness_dir / "state"
        state_dir.mkdir()

        plan = {
            "phases": [
                {
                    "id": "phase-0",
                    "name": "Setup",
                    "epics": [{"id": "e0", "name": "E0", "stories": [{"id": "s0", "name": "S0", "tasks": [
                        {"id": "t0", "description": "Setup", "acceptance_criteria": ["ac"],
                         "steps": [], "depends_on": [], "status": "pending", "attempts": 0, "blocked_reason": None},
                    ]}]}]
                },
                {
                    "id": "phase-1",
                    "name": "Features",
                    "epics": [{"id": "e1", "name": "E1", "stories": [{"id": "s1", "name": "S1", "tasks": [
                        {"id": "t1", "description": "Feature", "acceptance_criteria": ["ac"],
                         "steps": [], "depends_on": [], "status": "pending", "attempts": 0, "blocked_reason": None},
                    ]}]}]
                },
            ]
        }
        wp_path = state_dir / "work_plan.json"
        WorkPlan(plan).save(wp_path)
        wp = WorkPlan.load(wp_path)

        # Phase 0 task should be returned first
        next_task = wp.get_next_task()
        assert next_task["id"] == "t0"

        # Mark t0 done
        wp.mark_task_done("t0", wp_path)
        wp = WorkPlan.load(wp_path)

        # Phase 1 task should now be returned
        next_task = wp.get_next_task()
        assert next_task["id"] == "t1"

    def test_dag_cycle_detected_before_running(self, harness_dir):
        """Circular dependency in work_plan.json is detected by validate_dag()."""
        import json
        from src.core.work_plan import WorkPlan

        state_dir = harness_dir / "state"
        state_dir.mkdir()

        plan = {
            "phases": [{
                "id": "phase-0",
                "epics": [{"id": "e0", "stories": [{"id": "s0", "tasks": [
                    {"id": "t1", "description": "A", "acceptance_criteria": [], "steps": [],
                     "depends_on": ["t2"], "status": "pending", "attempts": 0, "blocked_reason": None},
                    {"id": "t2", "description": "B", "acceptance_criteria": [], "steps": [],
                     "depends_on": ["t1"], "status": "pending", "attempts": 0, "blocked_reason": None},
                ]}]}]
            }]
        }
        wp = WorkPlan(plan)
        r = wp.validate_dag()
        assert r["valid"] is False
        assert len(r["errors"]) > 0


class TestPipelineResume:
    """Feature 049: pipeline resume after crash and old state backward compat."""

    def test_resume_from_partial_completion(self, harness_dir):
        """Pre-populated state (architect done) skips architect on resume."""
        import asyncio
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import AsyncMock, MagicMock, patch
        from src.core.planner_pipeline import run_planner_pipeline
        from src.core.state import StateManager

        state_dir = harness_dir / "state"
        state_dir.mkdir()
        prompts_dir = harness_dir / "prompts"
        prompts_dir.mkdir()
        project_dir = harness_dir / "project"
        project_dir.mkdir()

        for name in ["architect.md", "adversary.md"]:
            (prompts_dir / name).write_text(f"prompt for {name} {{{{STATE_DIR}}}}")

        # Pre-write architect's artifacts
        plan_data = {"phases": [{"id": "phase-0", "epics": [{"id": "e0", "stories": [
            {"id": "s0", "tasks": [{"id": "t0", "description": "X", "acceptance_criteria": ["a"],
             "steps": [], "depends_on": [], "status": "pending", "attempts": 0, "blocked_reason": None}]}]}]}]}
        (state_dir / "draft_work_plan.json").write_text(json.dumps(plan_data))
        (state_dir / "spec.md").write_text("## Spec")

        # Pre-populate state: architect done
        state_mgr = StateManager(state_dir)
        state_mgr.update_state(planner_roles={"architect": True})

        # Valid SPEC_GAPS.md for adversary validation
        (state_dir / "SPEC_GAPS.md").write_text(
            "x" * 200 + "\n### GAP-1: g1\n### GAP-2: g2\n### GAP-3: g3\n"
        )

        run_calls = []

        async def mock_run(prompt, options, project_dir, **kwargs):
            run_calls.append(True)
            return {"status": "success", "output": "", "cost": 0.1}

        single_config = {"planner_roles": [
            {"name": "architect", "model": "opus", "prompt": "architect.md",
             "artifact": "draft_work_plan.json",
             "validation": {"type": "work_plan", "min_tasks": 1, "min_phases": 1}},
            {"name": "adversary", "model": "opus", "prompt": "adversary.md",
             "artifact": "SPEC_GAPS.md",
             "validation": {"type": "markdown", "min_length": 100, "min_gaps": 3}},
        ]}

        with patch("src.core.orchestrator.run_agent_session", new=mock_run), \
             patch("src.core.planner_pipeline.create_client_options", return_value={}):
            result = asyncio.run(run_planner_pipeline(
                strategy_config=single_config,
                prompts_dir=prompts_dir,
                project_dir=project_dir,
                state_dir=state_dir,
                state_mgr=state_mgr,
                cost_tracker=MagicMock(),
                knowledge_section="",
                input_section="",
                config={"model": "test-sonnet", "planner_model": "test-opus",
                        "generator": {}, "security": {"sandbox": False}, "evaluator": {}},
            ))

        # Architect was skipped, only adversary ran
        assert len(run_calls) == 1
        assert "architect" in result["roles_completed"]
        assert "adversary" in result["roles_completed"]

    def test_old_state_without_planner_roles_runs_all(self, harness_dir):
        """Old state.json without planner_roles field — all roles run (backward compat)."""
        import asyncio
        import json
        from pathlib import Path
        from unittest.mock import AsyncMock, MagicMock, patch
        from src.core.planner_pipeline import run_planner_pipeline
        from src.core.state import StateManager, atomic_write

        state_dir = harness_dir / "state"
        state_dir.mkdir()
        prompts_dir = harness_dir / "prompts"
        prompts_dir.mkdir()
        project_dir = harness_dir / "project"
        project_dir.mkdir()

        (prompts_dir / "architect.md").write_text("architect {{STATE_DIR}}")
        plan_data = {"phases": [{"id": "phase-0", "epics": [{"id": "e0", "stories": [
            {"id": "s0", "tasks": [{"id": "t0", "description": "X", "acceptance_criteria": ["a"],
             "steps": [], "depends_on": [], "status": "pending", "attempts": 0, "blocked_reason": None}]}]}]}]}
        (state_dir / "draft_work_plan.json").write_text(json.dumps(plan_data))
        (state_dir / "spec.md").write_text("## Spec")

        # Write state WITHOUT planner_roles key
        atomic_write(state_dir / "state.json", {"planner_complete": False})
        state_mgr = StateManager(state_dir)

        run_calls = []

        async def mock_run(prompt, options, project_dir, **kwargs):
            run_calls.append(True)
            return {"status": "success", "output": "", "cost": 0.1}

        single_config = {"planner_roles": [
            {"name": "architect", "model": "opus", "prompt": "architect.md",
             "artifact": "draft_work_plan.json",
             "validation": {"type": "work_plan", "min_tasks": 1, "min_phases": 1}},
        ]}

        with patch("src.core.orchestrator.run_agent_session", new=mock_run), \
             patch("src.core.planner_pipeline.create_client_options", return_value={}):
            asyncio.run(run_planner_pipeline(
                strategy_config=single_config,
                prompts_dir=prompts_dir,
                project_dir=project_dir,
                state_dir=state_dir,
                state_mgr=state_mgr,
                cost_tracker=MagicMock(),
                knowledge_section="",
                input_section="",
                config={"model": "test-sonnet", "planner_model": "test-opus",
                        "generator": {}, "security": {"sandbox": False}, "evaluator": {}},
            ))

        # architect ran (no planner_roles in old state → no skipping)
        assert len(run_calls) == 1

    def test_complete_pipeline_marks_planner_complete_true(self, harness_dir):
        """After all roles complete, state.json has planner_complete=True."""
        import asyncio
        import json
        from unittest.mock import AsyncMock, MagicMock, patch
        from src.core.planner_pipeline import run_planner_pipeline
        from src.core.state import StateManager

        state_dir = harness_dir / "state"
        state_dir.mkdir()
        prompts_dir = harness_dir / "prompts"
        prompts_dir.mkdir()
        project_dir = harness_dir / "project"
        project_dir.mkdir()

        (prompts_dir / "architect.md").write_text("architect {{STATE_DIR}}")
        plan_data = {"phases": [{"id": "phase-0", "epics": [{"id": "e0", "stories": [
            {"id": "s0", "tasks": [{"id": "t0", "description": "X", "acceptance_criteria": ["a"],
             "steps": [], "depends_on": [], "status": "pending", "attempts": 0, "blocked_reason": None}]}]}]}]}
        (state_dir / "draft_work_plan.json").write_text(json.dumps(plan_data))

        state_mgr = StateManager(state_dir)

        async def mock_run(prompt, options, project_dir, **kwargs):
            return {"status": "success", "output": "", "cost": 0.0}

        with patch("src.core.orchestrator.run_agent_session", new=mock_run), \
             patch("src.core.planner_pipeline.create_client_options", return_value={}):
            asyncio.run(run_planner_pipeline(
                strategy_config={"planner_roles": [
                    {"name": "architect", "model": "opus", "prompt": "architect.md",
                     "artifact": "draft_work_plan.json",
                     "validation": {"type": "work_plan", "min_tasks": 1, "min_phases": 1}},
                ]},
                prompts_dir=prompts_dir,
                project_dir=project_dir,
                state_dir=state_dir,
                state_mgr=state_mgr,
                cost_tracker=MagicMock(),
                knowledge_section="",
                input_section="",
                config={"model": "test-sonnet", "planner_model": "test-opus",
                        "generator": {}, "security": {"sandbox": False}, "evaluator": {}},
            ))

        state = state_mgr.load_state()
        assert state["planner_complete"] is True
