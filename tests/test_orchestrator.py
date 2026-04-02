"""Tests for orchestrator.py — 3-agent loop logic."""

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.core.orchestrator import (
    load_prompt,
    process_conditionals,
    build_planner_prompt,
    validate_planner_output,
    ensure_browser_tools,
    check_evaluator_used_browser,
    _perform_sweep,
    _perform_suspicion_check,
    _perform_browser_check,
)
from src.core.control_plane import ControlPlaneClient


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


class TestLoadPrompt:
    def test_loads_file(self, tmp_dir):
        path = tmp_dir / "test.md"
        path.write_text("Hello {{NAME}}, welcome to {{PROJECT}}")
        result = load_prompt(path, {"NAME": "world", "PROJECT": "harness"})
        assert result == "Hello world, welcome to harness"

    def test_no_replacements(self, tmp_dir):
        path = tmp_dir / "test.md"
        path.write_text("No replacements here")
        result = load_prompt(path)
        assert result == "No replacements here"


class TestProcessConditionals:
    def test_true_condition_kept(self):
        text = "before {{#IF_WEB}}web content{{/IF_WEB}} after"
        result = process_conditionals(text, {"IF_WEB": True})
        assert "web content" in result
        assert "{{" not in result

    def test_false_condition_removed(self):
        text = "before {{#IF_WEB}}web content{{/IF_WEB}} after"
        result = process_conditionals(text, {"IF_WEB": False})
        assert "web content" not in result
        assert "before" in result
        assert "after" in result

    def test_missing_condition_removed(self):
        text = "before {{#IF_WEB}}web content{{/IF_WEB}} after"
        result = process_conditionals(text, {})
        assert "web content" not in result

    def test_multiline_conditional(self):
        text = """start
{{#IF_WEB_PROJECT}}
line 1
line 2
{{/IF_WEB_PROJECT}}
end"""
        result = process_conditionals(text, {"IF_WEB_PROJECT": True})
        assert "line 1" in result
        assert "line 2" in result

    def test_multiple_conditionals(self):
        text = "{{#IF_A}}aaa{{/IF_A}} {{#IF_B}}bbb{{/IF_B}}"
        result = process_conditionals(text, {"IF_A": True, "IF_B": False})
        assert "aaa" in result
        assert "bbb" not in result


class TestBuildPlannerPrompt:
    def test_prompt_mode(self, tmp_dir):
        prompts_dir = tmp_dir / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "planner.md").write_text("Base prompt\n{{STATE_DIR}}")
        state_dir = tmp_dir / "state"

        result = build_planner_prompt(prompts_dir, "Build a todo app", None, None, state_dir)
        assert "Build a todo app" in result
        assert "Project Description" in result

    def test_spec_mode(self, tmp_dir):
        prompts_dir = tmp_dir / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "planner.md").write_text("Base prompt")
        spec_file = tmp_dir / "spec.md"
        spec_file.write_text("# My Spec\nBuild something cool")
        state_dir = tmp_dir / "state"

        result = build_planner_prompt(prompts_dir, None, spec_file, None, state_dir)
        assert "My Spec" in result
        assert "Existing Specification" in result

    def test_plan_mode(self, tmp_dir):
        prompts_dir = tmp_dir / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "planner.md").write_text("Base prompt")
        plan_file = tmp_dir / "plan.md"
        plan_file.write_text("# Phase 1\n- Task A\n- Task B")
        state_dir = tmp_dir / "state"

        result = build_planner_prompt(prompts_dir, None, None, plan_file, state_dir)
        assert "Task A" in result
        assert "Existing Plan" in result

    def test_no_input_raises(self, tmp_dir):
        prompts_dir = tmp_dir / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "planner.md").write_text("Base prompt")
        state_dir = tmp_dir / "state"

        with pytest.raises(ValueError, match="One of"):
            build_planner_prompt(prompts_dir, None, None, None, state_dir)


class TestValidatePlannerOutput:
    def test_passes_when_both_files_exist(self, tmp_dir):
        state_dir = tmp_dir / "state"
        state_dir.mkdir()
        (state_dir / "feature_list.json").write_text('[{"id": "001", "passes": false}]')
        (state_dir / "SPEC_GAPS.md").write_text(
            "## Pass 1: Technical Gaps\n- None found\n## Pass 2\n- OK"
        )
        result = validate_planner_output(state_dir)
        assert result["valid"] is True

    def test_fails_when_feature_list_missing(self, tmp_dir):
        state_dir = tmp_dir / "state"
        state_dir.mkdir()
        (state_dir / "SPEC_GAPS.md").write_text("## Pass 1\n- gap")
        result = validate_planner_output(state_dir)
        assert result["valid"] is False
        assert "feature_list.json" in result["reason"]

    def test_fails_when_feature_list_empty(self, tmp_dir):
        state_dir = tmp_dir / "state"
        state_dir.mkdir()
        (state_dir / "feature_list.json").write_text("[]")
        (state_dir / "SPEC_GAPS.md").write_text(
            "## Pass 1\n- gap\n## Pass 2\n- another gap section"
        )
        result = validate_planner_output(state_dir)
        assert result["valid"] is False
        assert "empty" in result["reason"]

    def test_fails_when_spec_gaps_missing(self, tmp_dir):
        state_dir = tmp_dir / "state"
        state_dir.mkdir()
        (state_dir / "feature_list.json").write_text('[{"id": "001", "passes": false}]')
        result = validate_planner_output(state_dir)
        assert result["valid"] is False
        assert "SPEC_GAPS.md" in result["reason"]

    def test_fails_when_spec_gaps_too_short(self, tmp_dir):
        state_dir = tmp_dir / "state"
        state_dir.mkdir()
        (state_dir / "feature_list.json").write_text('[{"id": "001", "passes": false}]')
        (state_dir / "SPEC_GAPS.md").write_text("ok")  # < 50 chars = probably skipped
        result = validate_planner_output(state_dir)
        assert result["valid"] is False
        assert "too short" in result["reason"]


class TestBuildPlannerPromptKnowledge:
    def test_appends_knowledge_when_control_plane_has_chunks(self, tmp_dir):
        prompts_dir = tmp_dir / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "planner.md").write_text("PLANNER PROMPT {{STATE_DIR}}")
        state_dir = tmp_dir / "state"
        state_dir.mkdir()

        fake_chunks = [
            {"chunk_type": "failure_mode", "content": "FTS triggers omitted"},
        ]

        with patch("src.core.orchestrator.ControlPlaneClient") as MockCP:
            instance = MockCP.return_value
            instance.get_all_chunks.return_value = fake_chunks
            result = build_planner_prompt(
                prompts_dir, "build a dashboard", None, None, state_dir
            )
            assert "LEARNINGS FROM PREVIOUS RUNS" in result
            assert "FTS triggers omitted" in result

    def test_works_without_knowledge(self, tmp_dir):
        prompts_dir = tmp_dir / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "planner.md").write_text("PLANNER PROMPT {{STATE_DIR}}")
        state_dir = tmp_dir / "state"
        state_dir.mkdir()

        with patch("src.core.orchestrator.ControlPlaneClient") as MockCP:
            instance = MockCP.return_value
            instance.get_all_chunks.return_value = []
            result = build_planner_prompt(
                prompts_dir, "build a dashboard", None, None, state_dir
            )
            assert "LEARNINGS FROM PREVIOUS RUNS" not in result
            assert "build a dashboard" in result


class TestEnsureBrowserTools:
    def test_skips_when_browser_verification_never(self, tmp_dir):
        config = {"evaluator": {"browser_verification": "never"}}
        with patch("subprocess.run") as mock_run:
            ensure_browser_tools(tmp_dir, config)
            mock_run.assert_not_called()

    def test_skips_when_browser_tool_not_playwright(self, tmp_dir):
        config = {"evaluator": {"browser_verification": "auto", "browser_tool": "puppeteer"}}
        with patch("subprocess.run") as mock_run:
            ensure_browser_tools(tmp_dir, config)
            mock_run.assert_not_called()

    def test_skips_install_when_npx_available(self, tmp_dir):
        config = {"evaluator": {"browser_verification": "auto", "browser_tool": "playwright"}}
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            ensure_browser_tools(tmp_dir, config)
            # Only the check call, not the install call
            assert mock_run.call_count == 1
            assert "@playwright/mcp" in mock_run.call_args[0][0]

    def test_installs_when_npx_not_available(self, tmp_dir):
        config = {"evaluator": {"browser_verification": "auto", "browser_tool": "playwright"}}
        check_result = MagicMock()
        check_result.returncode = 1
        install_result = MagicMock()
        install_result.returncode = 0

        with patch("subprocess.run", side_effect=[check_result, install_result]) as mock_run:
            ensure_browser_tools(tmp_dir, config)
            assert mock_run.call_count == 2
            # Second call should be npm install
            install_cmd = mock_run.call_args_list[1][0][0]
            assert "npm" in install_cmd
            assert "@playwright/mcp" in install_cmd

    def test_does_not_crash_on_exception(self, tmp_dir):
        config = {"evaluator": {"browser_verification": "auto", "browser_tool": "playwright"}}

        with patch("subprocess.run", side_effect=Exception("npx not found")):
            # Should not raise
            ensure_browser_tools(tmp_dir, config)

    def test_defaults_to_playwright_when_no_browser_tool(self, tmp_dir):
        config = {"evaluator": {"browser_verification": "auto"}}
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            ensure_browser_tools(tmp_dir, config)
            assert mock_run.call_count == 1


class TestFeatureDescInEvents:
    """Tests verifying feature_desc is included in control plane event bodies."""

    def _make_cp(self, monkeypatch):
        monkeypatch.setenv("HARNESS_CONTROL_PLANE_URL", "http://localhost:7842")
        cp = ControlPlaneClient()
        cp.run_id = "run-test"
        return cp

    def _mock_response(self):
        resp = MagicMock()
        resp.read.return_value = b"{}"
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_post_event_passes_feature_desc_kwarg(self, monkeypatch):
        """Mock post_event and verify feature_desc kwarg is captured."""
        monkeypatch.setenv("HARNESS_CONTROL_PLANE_URL", "http://localhost:7842")
        cp = ControlPlaneClient()
        cp.run_id = "run-test"

        with patch.object(cp, "post_event") as mock_post:
            cp.post_event("feature_pass", feature_id="001", feature_desc="Add auth module", duration_ms=200)
            mock_post.assert_called_once()
            _, kwargs = mock_post.call_args
            assert "feature_desc" in kwargs
            assert kwargs["feature_desc"] == "Add auth module"

    def test_feature_pass_event_has_feature_id_and_feature_desc(self, monkeypatch):
        """Verify feature_pass HTTP body contains both feature_id and feature_desc."""
        cp = self._make_cp(monkeypatch)
        bodies = []

        def capture(req, timeout=None):
            bodies.append(json.loads(req.data.decode()))
            return self._mock_response()

        with patch("urllib.request.urlopen", side_effect=capture):
            cp.post_event("feature_pass", feature_id="007", feature_desc="Add worktree sweep", duration_ms=1500)
            time.sleep(0.1)

        assert len(bodies) == 1
        body = bodies[0]
        assert body["event_type"] == "feature_pass"
        assert body["feature_id"] == "007"
        assert body["feature_desc"] == "Add worktree sweep"

    def test_feature_fail_event_has_feature_id_and_feature_desc(self, monkeypatch):
        """Verify feature_fail HTTP body contains both feature_id and feature_desc."""
        cp = self._make_cp(monkeypatch)
        bodies = []

        def capture(req, timeout=None):
            bodies.append(json.loads(req.data.decode()))
            return self._mock_response()

        with patch("urllib.request.urlopen", side_effect=capture):
            cp.post_event("feature_fail", feature_id="007", feature_desc="Add worktree sweep", error="VERDICT: FAIL")
            time.sleep(0.1)

        assert len(bodies) == 1
        body = bodies[0]
        assert body["event_type"] == "feature_fail"
        assert body["feature_id"] == "007"
        assert body["feature_desc"] == "Add worktree sweep"

    def test_empty_description_does_not_cause_errors(self, monkeypatch):
        """Verify empty feature_desc does not raise and is forwarded as empty string."""
        cp = self._make_cp(monkeypatch)
        bodies = []

        def capture(req, timeout=None):
            bodies.append(json.loads(req.data.decode()))
            return self._mock_response()

        with patch("urllib.request.urlopen", side_effect=capture):
            cp.post_event("feature_pass", feature_id="001", feature_desc="", duration_ms=100)
            time.sleep(0.1)

        assert len(bodies) == 1
        assert bodies[0]["feature_desc"] == ""


class TestCheckEvaluatorUsedBrowser:
    """Tests for check_evaluator_used_browser: UI+browser, UI+no-browser, non-UI."""

    def test_ui_feature_with_browser(self):
        """UI feature desc + output containing browser_snapshot -> ui_feature=True, browser_used=True, warning=None."""
        result = check_evaluator_used_browser(
            "Add dashboard overview page",
            "I used browser_snapshot to capture the rendered page.",
        )
        assert result["ui_feature"] is True
        assert result["browser_used"] is True
        assert result["warning"] is None

    def test_ui_feature_without_browser(self):
        """UI feature desc + output with no browser mentions -> warning contains 'without browser verification'."""
        result = check_evaluator_used_browser(
            "Add dashboard overview page",
            "I ran pytest and all tests passed.",
        )
        assert result["ui_feature"] is True
        assert result["browser_used"] is False
        assert result["warning"] is not None
        assert "without browser verification" in result["warning"]

    def test_non_ui_feature(self):
        """Non-UI feature desc -> ui_feature=False, browser_used=False, warning=None regardless of output."""
        result = check_evaluator_used_browser(
            "Create database schema",
            "Ran migrations, all tables created. browser_snapshot also used.",
        )
        assert result["ui_feature"] is False
        assert result["browser_used"] is False
        assert result["warning"] is None

    def test_playwright_mcp_tool(self):
        """'Render user profile frontend' + output with 'mcp__playwright__' -> browser_used=True."""
        result = check_evaluator_used_browser(
            "Render user profile frontend",
            "Called mcp__playwright__browser_navigate to open the page.",
        )
        assert result["ui_feature"] is True
        assert result["browser_used"] is True
        assert result["warning"] is None

    def test_case_insensitive(self):
        """Case-insensitive matching: 'DASHBOARD' in feature desc triggers UI detection."""
        result = check_evaluator_used_browser(
            "DASHBOARD overview implementation",
            "Tests passed.",
        )
        assert result["ui_feature"] is True
        assert result["browser_used"] is False


class TestWorktreeSweepWiring:
    """Verify that orchestrator sweep wiring does not crash on exception."""

    def test_sweep_exception_does_not_propagate(self, tmp_dir):
        """sweep_merged_worktrees raising must not propagate — harness continues."""
        config = {"worktree": {"sweep_on_start": True}}
        with patch("src.core.orchestrator.sweep_merged_worktrees", side_effect=Exception("no git repo")):
            # Must not raise
            _perform_sweep(tmp_dir, config, "start")

    def test_sweep_failure_is_logged(self, tmp_dir, capsys):
        """Sweep failure must be printed as non-fatal so operator knows it happened."""
        config = {"worktree": {"sweep_on_start": True}}
        with patch("src.core.orchestrator.sweep_merged_worktrees", side_effect=Exception("not a git repo")):
            _perform_sweep(tmp_dir, config, "start")
        captured = capsys.readouterr()
        assert "non-fatal" in captured.out
        assert "not a git repo" in captured.out

    def test_sweep_disabled_skips_call(self, tmp_dir):
        """When sweep_on_start is False, sweep_merged_worktrees must not be called."""
        config = {"worktree": {"sweep_on_start": False}}
        with patch("src.core.orchestrator.sweep_merged_worktrees", side_effect=Exception("should not call")) as mock_sweep:
            _perform_sweep(tmp_dir, config, "start")
            mock_sweep.assert_not_called()

    def test_sweep_end_exception_does_not_propagate(self, tmp_dir):
        """sweep_on_end exception must also be caught — wiring is symmetric."""
        config = {"worktree": {"sweep_on_end": True}}
        with patch("src.core.orchestrator.sweep_merged_worktrees", side_effect=OSError("detached worktree")):
            _perform_sweep(tmp_dir, config, "end")


class TestPerFeatureCostTrackingWiring:
    """Tests verifying per-feature cost tracking wiring produces correct state."""

    def test_feature_costs_populated_after_generator_and_evaluator(self):
        """After generator + evaluator record_feature calls, feature_costs has the feature entry."""
        from src.core.cost_tracker import CostTracker

        cost_tracker = CostTracker()
        feature_id = "042"

        # Simulate orchestrator wiring: generator session
        cost_tracker.record("generator", 1.25)
        cost_tracker.record_feature(feature_id, "generator", 1.25)

        # Simulate orchestrator wiring: evaluator session
        cost_tracker.record("evaluator", 0.75)
        cost_tracker.record_feature(feature_id, "evaluator", 0.75)

        assert feature_id in cost_tracker.feature_costs
        assert abs(cost_tracker.feature_costs[feature_id] - 2.0) < 1e-9

    def test_cost_breakdown_includes_feature_costs_after_to_dict(self):
        """cost_tracker.to_dict() includes feature_costs key — mirrors what state.json receives."""
        from src.core.cost_tracker import CostTracker

        cost_tracker = CostTracker()
        cost_tracker.record("generator", 0.50)
        cost_tracker.record_feature("007", "generator", 0.50)
        cost_tracker.record("evaluator", 0.30)
        cost_tracker.record_feature("007", "evaluator", 0.30)

        state_cost_breakdown = cost_tracker.to_dict()

        assert "feature_costs" in state_cost_breakdown
        assert "007" in state_cost_breakdown["feature_costs"]
        assert abs(state_cost_breakdown["feature_costs"]["007"] - 0.80) < 1e-4

    def test_multiple_features_each_have_independent_costs(self):
        """Recording costs for two features keeps their totals separate."""
        from src.core.cost_tracker import CostTracker

        cost_tracker = CostTracker()
        cost_tracker.record_feature("001", "generator", 1.00)
        cost_tracker.record_feature("001", "evaluator", 0.50)
        cost_tracker.record_feature("002", "generator", 2.00)

        breakdown = cost_tracker.to_dict()["feature_costs"]
        assert abs(breakdown["001"] - 1.50) < 1e-9
        assert abs(breakdown["002"] - 2.00) < 1e-9

    def test_most_expensive_features_appears_in_format_summary(self):
        """format_summary output includes per-feature cost lines when features are recorded."""
        from src.core.cost_tracker import CostTracker

        cost_tracker = CostTracker()
        cost_tracker.record("generator", 3.00)
        cost_tracker.record_feature("010", "generator", 3.00)
        cost_tracker.record("generator", 1.00)
        cost_tracker.record_feature("011", "generator", 1.00)

        summary = cost_tracker.format_summary()

        assert "Per-feature costs:" in summary
        assert "010" in summary
        assert "011" in summary

    def test_most_expensive_features_returns_sorted_descending(self):
        """most_expensive_features returns highest-cost features first."""
        from src.core.cost_tracker import CostTracker

        cost_tracker = CostTracker()
        cost_tracker.record_feature("001", "generator", 0.50)
        cost_tracker.record_feature("002", "generator", 2.00)
        cost_tracker.record_feature("003", "generator", 1.00)

        top = cost_tracker.most_expensive_features(5)

        assert top[0][0] == "002"
        assert top[1][0] == "003"
        assert top[2][0] == "001"


class TestSuspicionCheckWiring:
    """Test suspicion check wiring in orchestrator is informational only."""

    def test_check_suspicion_called_with_correct_params(self):
        """check_suspicion is called with state_mgr, config, cb_opens, elapsed_total."""
        state_mgr = MagicMock()
        config = {"suspicion": {"min_features": 5}}
        with patch("src.core.orchestrator.check_suspicion", return_value={"suspicious": False, "reasons": []}) as mock_cs:
            _perform_suspicion_check(state_mgr, config, 3, 250.0, 0)
        mock_cs.assert_called_once_with(state_mgr, config, 3, 250.0)

    def test_suspicious_result_does_not_prevent_completion(self):
        """Even when suspicious=True, no exception is raised — harness completes normally."""
        state_mgr = MagicMock()
        config = {}
        with patch("src.core.orchestrator.check_suspicion", return_value={"suspicious": True, "reasons": ["reason"]}):
            # Must not raise
            _perform_suspicion_check(state_mgr, config, 0, 100.0, 0)

    def test_suspicion_report_printed_when_suspicious(self, capsys):
        """When suspicious=True, the WARNING banner and reasons are printed."""
        state_mgr = MagicMock()
        config = {}
        reasons = ["Zero retries detected", "Elapsed time too short"]
        with patch("src.core.orchestrator.check_suspicion", return_value={"suspicious": True, "reasons": reasons}):
            _perform_suspicion_check(state_mgr, config, 0, 100.0, 0)
        captured = capsys.readouterr()
        assert "WARNING" in captured.out
        assert "Suspicious completion detected" in captured.out
        for reason in reasons:
            assert reason in captured.out

    def test_not_suspicious_no_warning_printed(self, capsys):
        """When suspicious=False, no warning is printed."""
        state_mgr = MagicMock()
        config = {}
        with patch("src.core.orchestrator.check_suspicion", return_value={"suspicious": False, "reasons": []}):
            _perform_suspicion_check(state_mgr, config, 5, 300.0, 0)
        captured = capsys.readouterr()
        assert "WARNING" not in captured.out

    def test_remaining_nonzero_skips_check(self):
        """When remaining != 0, check_suspicion is not called — run is incomplete."""
        state_mgr = MagicMock()
        config = {}
        with patch("src.core.orchestrator.check_suspicion") as mock_cs:
            _perform_suspicion_check(state_mgr, config, 0, 100.0, remaining=5)
        mock_cs.assert_not_called()


class TestBrowserCheckWiring:
    """Test browser check wiring in orchestrator prints warning but does not fail feature."""

    def test_warning_printed_for_ui_feature_without_browser(self, capsys):
        """UI feature + no browser used -> warning is printed."""
        with patch(
            "src.core.orchestrator.check_evaluator_used_browser",
            return_value={"ui_feature": True, "browser_used": False, "warning": "Evaluator verified UI feature without browser verification"},
        ):
            _perform_browser_check("Implement dashboard UI page", "Tests all pass.")
        captured = capsys.readouterr()
        assert "WARNING" in captured.out
        assert "without browser verification" in captured.out

    def test_feature_still_passes_despite_warning(self, capsys):
        """_perform_browser_check does not raise — feature pass/fail is unaffected."""
        with patch(
            "src.core.orchestrator.check_evaluator_used_browser",
            return_value={"ui_feature": True, "browser_used": False, "warning": "Evaluator verified UI feature without browser verification"},
        ):
            # Must not raise — feature remains PASS
            _perform_browser_check("Render button component", "All assertions passed.")

    def test_no_warning_for_non_ui_feature(self, capsys):
        """Non-UI feature -> no warning printed."""
        with patch(
            "src.core.orchestrator.check_evaluator_used_browser",
            return_value={"ui_feature": False, "browser_used": False, "warning": None},
        ):
            _perform_browser_check("Add database migration script", "Migration ran OK.")
        captured = capsys.readouterr()
        assert "WARNING" not in captured.out


class TestValidatePlannerOutputWorkPlan:
    """Feature 046: validate_planner_output with use_work_plan=True."""

    def test_valid_work_plan_with_spec_gaps_passes(self, tmp_path):
        import json
        wp = {"phases": [{"id": "phase-0", "epics": []}]}
        (tmp_path / "work_plan.json").write_text(json.dumps(wp))
        (tmp_path / "SPEC_GAPS.md").write_text("x" * 100)
        r = validate_planner_output(tmp_path, use_work_plan=True)
        assert r["valid"] is True

    def test_missing_work_plan_fails(self, tmp_path):
        (tmp_path / "SPEC_GAPS.md").write_text("x" * 100)
        r = validate_planner_output(tmp_path, use_work_plan=True)
        assert r["valid"] is False
        assert "work_plan.json" in r["reason"]

    def test_work_plan_without_phases_key_fails(self, tmp_path):
        import json
        (tmp_path / "work_plan.json").write_text(json.dumps({"tasks": []}))
        (tmp_path / "SPEC_GAPS.md").write_text("x" * 100)
        r = validate_planner_output(tmp_path, use_work_plan=True)
        assert r["valid"] is False
        assert "phases" in r["reason"]

    def test_legacy_mode_unchanged_when_use_work_plan_false(self, tmp_path):
        import json
        # Legacy: needs feature_list.json
        (tmp_path / "feature_list.json").write_text(json.dumps([{"id": "001"}]))
        (tmp_path / "SPEC_GAPS.md").write_text("x" * 100)
        r = validate_planner_output(tmp_path, use_work_plan=False)
        assert r["valid"] is True

    def test_invalid_work_plan_json_fails(self, tmp_path):
        (tmp_path / "work_plan.json").write_text("not json")
        (tmp_path / "SPEC_GAPS.md").write_text("x" * 100)
        r = validate_planner_output(tmp_path, use_work_plan=True)
        assert r["valid"] is False


class TestPipelineIntegration:
    """Feature 047: pipeline called when config has strategies."""

    def test_pipeline_called_when_strategy_exists(self, tmp_path):
        """When config has default_strategy + strategies, run_planner_pipeline is called."""
        import asyncio
        from src.core.orchestrator import run_harness

        config_path = tmp_path / "config.yaml"
        import yaml
        config = {
            "model": "test-sonnet",
            "planner_model": "test-opus",
            "default_strategy": "feature",
            "strategies": {
                "feature": {
                    "planner_roles": [
                        {
                            "name": "architect",
                            "model": "opus",
                            "prompt": "architect.md",
                            "artifact": "draft_work_plan.json",
                            "validation": {"type": "work_plan", "min_tasks": 1, "min_phases": 1},
                        }
                    ]
                }
            },
            "generator": {"max_turns_per_session": 1, "max_retries_per_feature": 3, "auto_continue_delay": 0},
            "evaluator": {"browser_verification": "never", "test_suite_command": None},
            "max_cost_usd": 10.0,
            "max_duration_minutes": 10,
            "max_iterations": 1,
            "security": {"sandbox": False},
            "circuit_breaker": {},
            "worktree": {"sweep_on_start": False, "sweep_on_end": False},
            "knowledge": {"inject_into_planner": False},
        }
        config_path.write_text(yaml.dump(config))

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        pipeline_calls = []

        async def mock_pipeline(**kwargs):
            pipeline_calls.append(kwargs)
            return {"success": True, "roles_completed": ["architect"], "total_cost": 0.1}

        def mock_session(prompt, options, project_dir):
            import asyncio as _asyncio
            async def _inner():
                return {"status": "success", "output": "---HARNESS_STATUS---\nSTATUS: IN_PROGRESS\n---END_HARNESS_STATUS---", "cost": 0.1}
            return _asyncio.ensure_future(_inner())

        with patch("src.core.orchestrator.run_planner_pipeline", wraps=mock_pipeline) as mock_pp, \
             patch("src.core.orchestrator.run_agent_session", side_effect=mock_session), \
             patch("src.core.orchestrator.ensure_browser_tools", return_value="skipped"), \
             patch("src.core.orchestrator.sweep_merged_worktrees", return_value=[]):
            try:
                asyncio.run(run_harness(
                    config_path=config_path,
                    project_dir=project_dir,
                    prompt="Build a test app",
                ))
            except Exception:
                pass  # run may fail due to missing artifacts — we just care about pipeline call

        assert len(pipeline_calls) >= 1 or mock_pp.called

    def test_legacy_flow_when_no_strategy_config(self, tmp_path):
        """When config lacks strategies, build_planner_prompt is called (legacy)."""
        import asyncio
        from src.core.orchestrator import run_harness

        config_path = tmp_path / "config.yaml"
        import yaml
        config = {
            "model": "test-sonnet",
            "generator": {"max_turns_per_session": 1, "max_retries_per_feature": 3, "auto_continue_delay": 0},
            "evaluator": {"browser_verification": "never"},
            "max_cost_usd": 10.0,
            "max_duration_minutes": 10,
            "max_iterations": 1,
            "security": {"sandbox": False},
            "circuit_breaker": {},
            "worktree": {"sweep_on_start": False, "sweep_on_end": False},
            "knowledge": {"inject_into_planner": False},
        }
        config_path.write_text(yaml.dump(config))

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        bpp_calls = []

        def mock_bpp(*args, **kwargs):
            bpp_calls.append(True)
            return "planner prompt"

        def mock_session(prompt, options, project_dir):
            import asyncio as _asyncio
            async def _inner():
                return {"status": "success", "output": "---HARNESS_STATUS---\nSTATUS: IN_PROGRESS\n---END_HARNESS_STATUS---", "cost": 0.1}
            return _asyncio.ensure_future(_inner())

        with patch("src.core.orchestrator.build_planner_prompt", side_effect=mock_bpp), \
             patch("src.core.orchestrator.run_agent_session", side_effect=mock_session), \
             patch("src.core.orchestrator.ensure_browser_tools", return_value="skipped"), \
             patch("src.core.orchestrator.sweep_merged_worktrees", return_value=[]):
            try:
                asyncio.run(run_harness(
                    config_path=config_path,
                    project_dir=project_dir,
                    prompt="Build a test app",
                ))
            except Exception:
                pass

        assert len(bpp_calls) >= 1
