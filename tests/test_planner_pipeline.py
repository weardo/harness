"""Tests for src/core/planner_pipeline.py — validation functions and pipeline."""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from src.core.planner_pipeline import (
    validate_json_array,
    validate_markdown,
    validate_work_plan,
    run_planner_pipeline,
    _resolve_model,
    _build_role_context,
)
from src.core.state import StateManager


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def make_work_plan(n_phases=1, n_tasks=3, with_ac=True):
    """Helper: build a valid work_plan dict."""
    tasks = [
        {
            "id": f"task-{i:03d}",
            "description": f"Task {i}",
            "acceptance_criteria": ["ac"] if with_ac else [],
            "steps": [],
            "depends_on": [],
            "status": "pending",
            "attempts": 0,
            "blocked_reason": None,
        }
        for i in range(n_tasks)
    ]
    phases = [
        {
            "id": f"phase-{p}",
            "name": f"Phase {p}",
            "epics": [
                {
                    "id": f"epic-{p}",
                    "name": f"Epic {p}",
                    "stories": [
                        {
                            "id": f"story-{p}",
                            "name": f"Story {p}",
                            "tasks": tasks,
                        }
                    ],
                }
            ],
        }
        for p in range(n_phases)
    ]
    return {"phases": phases}


# ---------------------------------------------------------------------------
# Validation tests (features 006-008)
# ---------------------------------------------------------------------------

class TestValidateJsonArray:
    def test_valid_array_returns_true(self, tmp_dir):
        f = tmp_dir / "valid.json"
        f.write_text(
            json.dumps([{"id": str(i), "acceptance_criteria": ["ac"]} for i in range(10)])
        )
        r = validate_json_array(f, min_items=5, required_fields=["id"])
        assert r == {"valid": True}

    def test_too_few_items_returns_false(self, tmp_dir):
        f = tmp_dir / "few.json"
        f.write_text(json.dumps([{"id": "1"}, {"id": "2"}]))
        r = validate_json_array(f, min_items=5)
        assert r["valid"] is False
        assert "2" in r["reason"]
        assert "5" in r["reason"]

    def test_missing_required_field_returns_false(self, tmp_dir):
        f = tmp_dir / "missing.json"
        f.write_text(json.dumps([{"id": "1"}, {"id": "2"}]))
        r = validate_json_array(f, min_items=1, required_fields=["acceptance_criteria"])
        assert r["valid"] is False
        assert "acceptance_criteria" in r["reason"]

    def test_nonexistent_file_returns_false(self, tmp_dir):
        r = validate_json_array(tmp_dir / "noexist.json")
        assert r["valid"] is False
        assert "not found" in r["reason"].lower()

    def test_invalid_json_returns_false(self, tmp_dir):
        f = tmp_dir / "bad.json"
        f.write_text("not json {{")
        r = validate_json_array(f)
        assert r["valid"] is False
        assert "JSON" in r["reason"]

    def test_json_object_not_array_returns_false(self, tmp_dir):
        f = tmp_dir / "obj.json"
        f.write_text(json.dumps({"key": "value"}))
        r = validate_json_array(f)
        assert r["valid"] is False
        assert "array" in r["reason"].lower()

    def test_empty_array_below_min_items(self, tmp_dir):
        f = tmp_dir / "empty.json"
        f.write_text("[]")
        r = validate_json_array(f, min_items=1)
        assert r["valid"] is False

    def test_multiple_required_fields_all_present(self, tmp_dir):
        f = tmp_dir / "multi.json"
        items = [{"id": str(i), "desc": "x", "steps": []} for i in range(3)]
        f.write_text(json.dumps(items))
        r = validate_json_array(f, min_items=1, required_fields=["id", "desc", "steps"])
        assert r == {"valid": True}


class TestValidateMarkdown:
    def test_valid_file_with_all_constraints_returns_true(self, tmp_dir):
        content = (
            "x" * 500
            + "\n## Technical Gaps\n## AI Failure Modes\n## Community Alignment\n"
            + "### GAP-1: First\n### GAP-2: Second\n### GAP-3: Third\n"
        )
        f = tmp_dir / "valid.md"
        f.write_text(content)
        r = validate_markdown(
            f,
            min_length=200,
            required_headings=["Technical Gaps", "AI Failure Modes"],
            min_gaps=3,
        )
        assert r == {"valid": True}

    def test_too_short_returns_false(self, tmp_dir):
        f = tmp_dir / "short.md"
        f.write_text("x" * 50)
        r = validate_markdown(f, min_length=200)
        assert r["valid"] is False
        assert "50" in r["reason"] or "short" in r["reason"].lower()

    def test_missing_required_heading_returns_false(self, tmp_dir):
        f = tmp_dir / "noheading.md"
        f.write_text("x" * 500)
        r = validate_markdown(f, min_length=100, required_headings=["AI Failure Modes"])
        assert r["valid"] is False
        assert "AI Failure Modes" in r["reason"]

    def test_insufficient_gaps_returns_false(self, tmp_dir):
        f = tmp_dir / "fewgaps.md"
        f.write_text("x" * 500 + "\n### GAP-1: Only one\n")
        r = validate_markdown(f, min_length=100, min_gaps=3)
        assert r["valid"] is False
        assert "1" in r["reason"]

    def test_nonexistent_file_returns_false(self, tmp_dir):
        r = validate_markdown(tmp_dir / "noexist.md")
        assert r["valid"] is False
        assert "not found" in r["reason"].lower()

    def test_gap_dash_prefix_pattern_counted(self, tmp_dir):
        """Lines starting with ### GAP- are counted as gaps."""
        f = tmp_dir / "gaps.md"
        f.write_text(
            "x" * 200
            + "\n### GAP-1: First\n### GAP-2: Second\n### GAP-3: Third\n"
        )
        r = validate_markdown(f, min_length=100, min_gaps=3)
        assert r == {"valid": True}

    def test_gap_without_dash_not_counted(self, tmp_dir):
        """### GAPS or ### GAP (no dash) should NOT be counted."""
        f = tmp_dir / "nogapdash.md"
        f.write_text("x" * 200 + "\n### GAPS section\n### GAP without dash\n")
        r = validate_markdown(f, min_length=100, min_gaps=1)
        assert r["valid"] is False

    def test_no_required_headings_passes(self, tmp_dir):
        f = tmp_dir / "noheadings.md"
        f.write_text("x" * 300)
        r = validate_markdown(f, min_length=100)
        assert r == {"valid": True}


class TestValidateWorkPlan:
    def test_valid_plan_returns_true(self, tmp_dir):
        f = tmp_dir / "plan.json"
        f.write_text(json.dumps(make_work_plan(n_phases=2, n_tasks=5)))
        r = validate_work_plan(f, min_tasks=5, min_phases=1)
        assert r == {"valid": True}

    def test_too_few_tasks_returns_false(self, tmp_dir):
        f = tmp_dir / "few.json"
        f.write_text(json.dumps(make_work_plan(n_tasks=2)))
        r = validate_work_plan(f, min_tasks=5)
        assert r["valid"] is False
        assert "2" in r["reason"]

    def test_no_phases_returns_false(self, tmp_dir):
        f = tmp_dir / "nophases.json"
        f.write_text(json.dumps({"phases": []}))
        r = validate_work_plan(f, min_phases=1)
        assert r["valid"] is False
        assert "phase" in r["reason"].lower()

    def test_missing_acceptance_criteria_returns_false(self, tmp_dir):
        f = tmp_dir / "noac.json"
        f.write_text(json.dumps(make_work_plan(n_tasks=3, with_ac=False)))
        r = validate_work_plan(f, min_tasks=1, require_acceptance_criteria=True)
        assert r["valid"] is False

    def test_nonexistent_file_returns_false(self, tmp_dir):
        r = validate_work_plan(tmp_dir / "noexist.json")
        assert r["valid"] is False
        assert "not found" in r["reason"].lower()

    def test_invalid_json_returns_false(self, tmp_dir):
        f = tmp_dir / "bad.json"
        f.write_text("not json")
        r = validate_work_plan(f)
        assert r["valid"] is False

    def test_counts_tasks_across_all_phases(self, tmp_dir):
        """2 phases * 3 tasks each = 6 total tasks."""
        f = tmp_dir / "multi.json"
        f.write_text(json.dumps(make_work_plan(n_phases=2, n_tasks=3)))
        r = validate_work_plan(f, min_tasks=6)
        assert r == {"valid": True}

    def test_missing_phases_key_returns_false(self, tmp_dir):
        f = tmp_dir / "nophasekey.json"
        f.write_text(json.dumps({"tasks": []}))
        r = validate_work_plan(f)
        assert r["valid"] is False
        assert "phases" in r["reason"]


# ---------------------------------------------------------------------------
# Pipeline tests (features 026-032)
# ---------------------------------------------------------------------------

def make_strategy_config():
    return {
        "planner_roles": [
            {
                "name": "architect",
                "model": "opus",
                "prompt": "architect.md",
                "artifact": "draft_work_plan.json",
                "validation": {"type": "work_plan", "min_tasks": 1, "min_phases": 1},
            },
            {
                "name": "adversary",
                "model": "opus",
                "prompt": "adversary.md",
                "artifact": "SPEC_GAPS.md",
                "validation": {"type": "markdown", "min_length": 10, "min_gaps": 1},
            },
            {
                "name": "refiner",
                "model": "sonnet",
                "prompt": "refiner.md",
                "artifact": "work_plan.json",
                "validation": {"type": "work_plan", "min_tasks": 1, "min_phases": 1},
            },
            {
                "name": "validator",
                "model": "opus",
                "prompt": "validator.md",
                "artifact": "VALIDATION.md",
                "validation": {"type": "markdown", "min_length": 10},
            },
        ]
    }


def make_config():
    return {
        "model": "test-sonnet",
        "planner_model": "test-opus",
        "generator": {"max_turns_per_session": 10},
        "security": {"sandbox": False},
        "evaluator": {},
    }


def make_mock_agent_result(cost=0.1):
    return {"status": "success", "output": "done", "cost": cost}


def write_valid_artifacts(state_dir: Path):
    """Pre-populate all artifacts that get validated so tests pass."""
    # draft_work_plan.json for architect
    (state_dir / "draft_work_plan.json").write_text(json.dumps(make_work_plan(n_tasks=2)))
    # SPEC_GAPS.md for adversary
    (state_dir / "SPEC_GAPS.md").write_text("x" * 50 + "\n### GAP-1: Test gap\n")
    # work_plan.json for refiner
    (state_dir / "work_plan.json").write_text(json.dumps(make_work_plan(n_tasks=2)))
    # VALIDATION.md for validator
    (state_dir / "VALIDATION.md").write_text("x" * 50)
    # spec.md used as context
    (state_dir / "spec.md").write_text("## Spec\nTest spec content")


class TestRunPlannerPipeline:
    """Feature 026: basic structure — iterate roles, call run_agent_session per role."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_calls_run_agent_session_4_times(self, tmp_dir):
        state_dir = tmp_dir / "state"
        state_dir.mkdir()
        prompts_dir = tmp_dir / "prompts"
        prompts_dir.mkdir()
        project_dir = tmp_dir / "project"
        project_dir.mkdir()

        # Create stub prompt files
        for name in ["architect.md", "adversary.md", "refiner.md", "validator.md"]:
            (prompts_dir / name).write_text(f"You are {name}. State dir: {{{{STATE_DIR}}}}")

        write_valid_artifacts(state_dir)

        state_mgr = StateManager(state_dir)
        mock_ct = MagicMock()
        mock_ct.record = MagicMock()

        with patch(
            "src.core.orchestrator.run_agent_session",
            new=AsyncMock(return_value=make_mock_agent_result()),
        ) as mock_run, patch(
            "src.core.planner_pipeline.create_client_options",
            return_value={"model": "test-opus"},
        ):
            result = self._run(
                run_planner_pipeline(
                    strategy_config=make_strategy_config(),
                    prompts_dir=prompts_dir,
                    project_dir=project_dir,
                    state_dir=state_dir,
                    state_mgr=state_mgr,
                    cost_tracker=mock_ct,
                    knowledge_section="",
                    input_section="",
                    config=make_config(),
                )
            )

        assert mock_run.call_count == 4
        assert result["success"] is True
        assert len(result["roles_completed"]) == 4
        assert result["total_cost"] == pytest.approx(0.4, abs=0.01)

    def test_returns_correct_structure(self, tmp_dir):
        state_dir = tmp_dir / "state"
        state_dir.mkdir()
        prompts_dir = tmp_dir / "prompts"
        prompts_dir.mkdir()
        project_dir = tmp_dir / "project"
        project_dir.mkdir()

        for name in ["architect.md", "adversary.md", "refiner.md", "validator.md"]:
            (prompts_dir / name).write_text("prompt")

        write_valid_artifacts(state_dir)
        state_mgr = StateManager(state_dir)
        mock_ct = MagicMock()

        with patch("src.core.orchestrator.run_agent_session", new=AsyncMock(return_value=make_mock_agent_result())), \
             patch("src.core.planner_pipeline.create_client_options", return_value={}):
            result = self._run(
                run_planner_pipeline(
                    strategy_config=make_strategy_config(),
                    prompts_dir=prompts_dir,
                    project_dir=project_dir,
                    state_dir=state_dir,
                    state_mgr=state_mgr,
                    cost_tracker=mock_ct,
                    knowledge_section="",
                    input_section="",
                    config=make_config(),
                )
            )

        assert "success" in result
        assert "roles_completed" in result
        assert "total_cost" in result


class TestPipelineContextAssembly:
    """Feature 027: per-role context assembly — FM-15 no context bleed."""

    def test_architect_gets_knowledge_and_input(self, tmp_dir):
        role = {"name": "architect"}
        ctx = _build_role_context(role, tmp_dir, "KNOWLEDGE", "INPUT")
        assert "KNOWLEDGE" in ctx
        assert "INPUT" in ctx

    def test_adversary_does_not_get_knowledge(self, tmp_dir):
        (tmp_dir / "spec.md").write_text("## Spec")
        (tmp_dir / "draft_work_plan.json").write_text("{}")
        role = {"name": "adversary"}
        ctx = _build_role_context(role, tmp_dir, "KNOWLEDGE_SECTION", "INPUT_SECTION")
        assert "KNOWLEDGE_SECTION" not in ctx
        assert "INPUT_SECTION" not in ctx

    def test_adversary_gets_spec_and_draft(self, tmp_dir):
        (tmp_dir / "spec.md").write_text("## Spec content")
        (tmp_dir / "draft_work_plan.json").write_text('{"test": true}')
        role = {"name": "adversary"}
        ctx = _build_role_context(role, tmp_dir, "", "")
        assert "## Spec content" in ctx
        assert '"test": true' in ctx

    def test_refiner_gets_spec_draft_and_gaps(self, tmp_dir):
        (tmp_dir / "spec.md").write_text("## Spec")
        (tmp_dir / "draft_work_plan.json").write_text("{}")
        (tmp_dir / "spec_gaps.json").write_text('{"gaps": []}')
        role = {"name": "refiner"}
        ctx = _build_role_context(role, tmp_dir, "", "")
        assert "## Spec" in ctx
        assert '"gaps"' in ctx

    def test_validator_gets_spec_plan_and_gaps(self, tmp_dir):
        (tmp_dir / "spec.md").write_text("## Spec")
        (tmp_dir / "work_plan.json").write_text("{}")
        (tmp_dir / "spec_gaps.json").write_text('{"gaps": []}')
        role = {"name": "validator"}
        ctx = _build_role_context(role, tmp_dir, "", "")
        assert "## Spec" in ctx
        assert '"gaps"' in ctx


class TestPipelineModelResolution:
    """Feature 028: model resolution."""

    def test_opus_maps_to_planner_model(self):
        config = {"planner_model": "test-opus", "model": "test-sonnet"}
        assert _resolve_model("opus", config) == "test-opus"

    def test_sonnet_maps_to_model(self):
        config = {"planner_model": "test-opus", "model": "test-sonnet"}
        assert _resolve_model("sonnet", config) == "test-sonnet"

    def test_concrete_model_id_unchanged(self):
        config = {"planner_model": "test-opus", "model": "test-sonnet"}
        assert _resolve_model("claude-opus-4-6", config) == "claude-opus-4-6"

    def test_architect_role_uses_planner_model(self, tmp_dir):
        """Architect role (model=opus) resolves to config planner_model."""
        state_dir = tmp_dir / "state"
        state_dir.mkdir()
        prompts_dir = tmp_dir / "prompts"
        prompts_dir.mkdir()
        project_dir = tmp_dir / "project"
        project_dir.mkdir()

        (prompts_dir / "architect.md").write_text("architect prompt")
        write_valid_artifacts(state_dir)
        state_mgr = StateManager(state_dir)

        calls_with_model = []

        async def mock_run(prompt, options, project_dir, **kwargs):
            calls_with_model.append(options.get("model"))
            return make_mock_agent_result()

        def mock_options(project_dir, config, model_override=None):
            return {"model": model_override}

        config = make_config()
        # only run architect role
        single_role_config = {"planner_roles": [make_strategy_config()["planner_roles"][0]]}
        # Pre-write artifact so validation passes
        (state_dir / "draft_work_plan.json").write_text(json.dumps(make_work_plan(n_tasks=2)))

        with patch("src.core.orchestrator.run_agent_session", new=mock_run), \
             patch("src.core.planner_pipeline.create_client_options", side_effect=mock_options):
            asyncio.run(
                run_planner_pipeline(
                    strategy_config=single_role_config,
                    prompts_dir=prompts_dir,
                    project_dir=project_dir,
                    state_dir=state_dir,
                    state_mgr=state_mgr,
                    cost_tracker=MagicMock(),
                    knowledge_section="",
                    input_section="",
                    config=config,
                )
            )

        assert calls_with_model[0] == "test-opus"


class TestPipelineResume:
    """Feature 030: resume support — pre-completed roles are skipped."""

    def test_skips_completed_role(self, tmp_dir):
        state_dir = tmp_dir / "state"
        state_dir.mkdir()
        prompts_dir = tmp_dir / "prompts"
        prompts_dir.mkdir()
        project_dir = tmp_dir / "project"
        project_dir.mkdir()

        for name in ["architect.md", "adversary.md", "refiner.md", "validator.md"]:
            (prompts_dir / name).write_text("prompt")
        write_valid_artifacts(state_dir)

        state_mgr = StateManager(state_dir)
        # Pre-mark architect as done
        state_mgr.update_state(planner_roles={"architect": True})

        run_calls = []

        async def mock_run(prompt, options, project_dir, **kwargs):
            run_calls.append(True)
            return make_mock_agent_result()

        with patch("src.core.orchestrator.run_agent_session", new=mock_run), \
             patch("src.core.planner_pipeline.create_client_options", return_value={}):
            result = asyncio.run(
                run_planner_pipeline(
                    strategy_config=make_strategy_config(),
                    prompts_dir=prompts_dir,
                    project_dir=project_dir,
                    state_dir=state_dir,
                    state_mgr=state_mgr,
                    cost_tracker=MagicMock(),
                    knowledge_section="",
                    input_section="",
                    config=make_config(),
                )
            )

        # Architect was skipped, so only 3 calls
        assert len(run_calls) == 3
        assert "architect" in result["roles_completed"]

    def test_all_roles_complete_sets_planner_complete(self, tmp_dir):
        state_dir = tmp_dir / "state"
        state_dir.mkdir()
        prompts_dir = tmp_dir / "prompts"
        prompts_dir.mkdir()
        project_dir = tmp_dir / "project"
        project_dir.mkdir()

        for name in ["architect.md", "adversary.md", "refiner.md", "validator.md"]:
            (prompts_dir / name).write_text("prompt")
        write_valid_artifacts(state_dir)

        state_mgr = StateManager(state_dir)

        with patch("src.core.orchestrator.run_agent_session", new=AsyncMock(return_value=make_mock_agent_result())), \
             patch("src.core.planner_pipeline.create_client_options", return_value={}):
            asyncio.run(
                run_planner_pipeline(
                    strategy_config=make_strategy_config(),
                    prompts_dir=prompts_dir,
                    project_dir=project_dir,
                    state_dir=state_dir,
                    state_mgr=state_mgr,
                    cost_tracker=MagicMock(),
                    knowledge_section="",
                    input_section="",
                    config=make_config(),
                )
            )

        state = state_mgr.load_state()
        assert state["planner_complete"] is True


class TestPipelineLegacyState:
    """Feature 031: old state.json without planner_roles field still works."""

    def test_missing_planner_roles_field_runs_all(self, tmp_dir):
        state_dir = tmp_dir / "state"
        state_dir.mkdir()
        prompts_dir = tmp_dir / "prompts"
        prompts_dir.mkdir()
        project_dir = tmp_dir / "project"
        project_dir.mkdir()

        for name in ["architect.md", "adversary.md", "refiner.md", "validator.md"]:
            (prompts_dir / name).write_text("prompt")
        write_valid_artifacts(state_dir)

        state_mgr = StateManager(state_dir)
        # Explicitly write state WITHOUT planner_roles key
        from src.core.state import atomic_write
        atomic_write(state_dir / "state.json", {"planner_complete": False})

        run_calls = []

        async def mock_run(prompt, options, project_dir, **kwargs):
            run_calls.append(True)
            return make_mock_agent_result()

        with patch("src.core.orchestrator.run_agent_session", new=mock_run), \
             patch("src.core.planner_pipeline.create_client_options", return_value={}):
            asyncio.run(
                run_planner_pipeline(
                    strategy_config=make_strategy_config(),
                    prompts_dir=prompts_dir,
                    project_dir=project_dir,
                    state_dir=state_dir,
                    state_mgr=state_mgr,
                    cost_tracker=MagicMock(),
                    knowledge_section="",
                    input_section="",
                    config=make_config(),
                )
            )

        assert len(run_calls) == 4  # all 4 roles ran


class TestPipelineRetry:
    """Feature 029: validation gate with retry."""

    def test_retry_on_validation_failure(self, tmp_dir):
        """If artifact validation fails, the role is retried once."""
        state_dir = tmp_dir / "state"
        state_dir.mkdir()
        prompts_dir = tmp_dir / "prompts"
        prompts_dir.mkdir()
        project_dir = tmp_dir / "project"
        project_dir.mkdir()

        # Only test adversary role (markdown validation)
        single_config = {
            "planner_roles": [
                {
                    "name": "adversary",
                    "model": "opus",
                    "prompt": "adversary.md",
                    "artifact": "SPEC_GAPS.md",
                    "validation": {"type": "markdown", "min_length": 100, "min_gaps": 3},
                }
            ]
        }
        (prompts_dir / "adversary.md").write_text("adversary prompt {{STATE_DIR}}")
        # Pre-write spec.md and draft
        (state_dir / "spec.md").write_text("## Spec")
        (state_dir / "draft_work_plan.json").write_text("{}")

        run_count = [0]

        async def mock_run(prompt, options, project_dir, **kwargs):
            run_count[0] += 1
            # After first run: write invalid artifact
            # After second run: write valid artifact
            if run_count[0] == 1:
                (state_dir / "SPEC_GAPS.md").write_text("too short")
            else:
                (state_dir / "SPEC_GAPS.md").write_text(
                    "x" * 200 + "\n### GAP-1: g1\n### GAP-2: g2\n### GAP-3: g3\n"
                )
            return make_mock_agent_result()

        state_mgr = StateManager(state_dir)

        with patch("src.core.orchestrator.run_agent_session", new=mock_run), \
             patch("src.core.planner_pipeline.create_client_options", return_value={}):
            asyncio.run(
                run_planner_pipeline(
                    strategy_config=single_config,
                    prompts_dir=prompts_dir,
                    project_dir=project_dir,
                    state_dir=state_dir,
                    state_mgr=state_mgr,
                    cost_tracker=MagicMock(),
                    knowledge_section="",
                    input_section="",
                    config=make_config(),
                )
            )

        # Should have run twice: initial + retry
        assert run_count[0] == 2


# ---------------------------------------------------------------------------
# Prompt integration tests (feature 037)
# ---------------------------------------------------------------------------

class TestPromptLoading:
    """Verify all 4 role prompts load correctly with {{STATE_DIR}} substitution."""

    PROMPTS_DIR = Path(__file__).parent.parent / "src" / "prompts"

    def _load(self, filename, state_dir):
        from src.core.orchestrator import load_prompt
        path = self.PROMPTS_DIR / filename
        return load_prompt(path, {"STATE_DIR": str(state_dir)})

    def test_architect_loads_without_error(self, tmp_dir):
        text = self._load("architect.md", tmp_dir)
        assert len(text) > 50

    def test_architect_state_dir_replaced(self, tmp_dir):
        text = self._load("architect.md", tmp_dir)
        assert "{{STATE_DIR}}" not in text
        assert str(tmp_dir) in text

    def test_adversary_loads_without_error(self, tmp_dir):
        text = self._load("adversary.md", tmp_dir)
        assert len(text) > 50

    def test_adversary_state_dir_replaced(self, tmp_dir):
        text = self._load("adversary.md", tmp_dir)
        assert "{{STATE_DIR}}" not in text
        assert str(tmp_dir) in text

    def test_adversary_says_did_not_write_spec(self, tmp_dir):
        text = self._load("adversary.md", tmp_dir)
        assert "did NOT write" in text or "You did NOT write" in text

    def test_refiner_loads_without_error(self, tmp_dir):
        text = self._load("refiner.md", tmp_dir)
        assert len(text) > 50

    def test_refiner_state_dir_replaced(self, tmp_dir):
        text = self._load("refiner.md", tmp_dir)
        assert "{{STATE_DIR}}" not in text

    def test_refiner_mentions_address_every_gap(self, tmp_dir):
        text = self._load("refiner.md", tmp_dir)
        assert "every gap" in text.lower() or "address" in text.lower()

    def test_validator_loads_without_error(self, tmp_dir):
        text = self._load("validator.md", tmp_dir)
        assert len(text) > 50

    def test_validator_state_dir_replaced(self, tmp_dir):
        text = self._load("validator.md", tmp_dir)
        assert "{{STATE_DIR}}" not in text

    def test_validator_includes_coverage(self, tmp_dir):
        text = self._load("validator.md", tmp_dir)
        assert "coverage" in text.lower()

    def test_validator_includes_sign_off(self, tmp_dir):
        text = self._load("validator.md", tmp_dir)
        assert "sign_off" in text or "sign-off" in text.lower()

    def test_architect_does_not_reference_spec_gaps(self, tmp_dir):
        text = self._load("architect.md", tmp_dir)
        assert "SPEC_GAPS.md" not in text

    def test_architect_does_not_reference_init_sh(self, tmp_dir):
        text = self._load("architect.md", tmp_dir)
        assert "init.sh" not in text
