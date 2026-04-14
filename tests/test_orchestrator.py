"""Tests for orchestrator.py — 3-agent loop logic."""

import asyncio
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from src.core.orchestrator import (
    load_prompt,
    process_conditionals,
    _apply_execution_recipe_config,
    build_planner_prompt,
    validate_planner_output,
    ensure_claude_auth,
    ensure_browser_tools,
    check_evaluator_used_browser,
    _perform_sweep,
    _perform_suspicion_check,
    _perform_browser_check,
    _setup_run,
    run_agent_session_cli,
)
from src.core.control_plane import ControlPlaneClient
from src.core.state import RunRegistry, StateManager


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


class TestExecutionRecipeConfig:
    def test_scoped_recipe_disables_browser_verification(self):
        config = {
            "evaluator": {
                "browser_verification": "auto",
                "browser_tool": "playwright",
            }
        }
        recipe = {"evaluation_policy": "scoped_tests"}

        adjusted = _apply_execution_recipe_config(config, recipe)

        assert adjusted["evaluator"]["browser_verification"] == "never"
        assert config["evaluator"]["browser_verification"] == "auto"

    def test_full_suite_recipe_preserves_browser_verification(self):
        config = {
            "evaluator": {
                "browser_verification": "auto",
                "browser_tool": "playwright",
            }
        }
        recipe = {"evaluation_policy": "full_suite"}

        adjusted = _apply_execution_recipe_config(config, recipe)

        assert adjusted["evaluator"]["browser_verification"] == "auto"


class TestEnsureClaudeAuth:
    def test_returns_api_key_when_env_present(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        assert ensure_claude_auth() == "api_key"

    def test_returns_cli_logged_in_when_status_reports_logged_in(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        mock_result = MagicMock()
        mock_result.stdout = '{"loggedIn": true, "authMethod": "oauth"}'

        with patch("subprocess.run", return_value=mock_result):
            assert ensure_claude_auth() == "cli_logged_in"

    def test_raises_clear_error_when_not_logged_in(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        mock_result = MagicMock()
        mock_result.stdout = '{"loggedIn": false, "authMethod": "none"}'

        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="not logged in"):
                ensure_claude_auth()


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
        assert body["type"] == "feature_pass"
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
        assert body["type"] == "feature_fail"
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
             patch("src.core.orchestrator.ensure_claude_auth", return_value="api_key"), \
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
             patch("src.core.orchestrator.ensure_claude_auth", return_value="api_key"), \
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


class TestSetupRun:
    """Tests for _setup_run() — run isolation and registry integration."""

    def test_new_run_creates_isolated_dir(self, tmp_dir):
        """New run creates .harness/runs/<run_id>/ and returns state_mgr scoped to it."""
        project_dir = tmp_dir / "project"
        project_dir.mkdir()
        run_id, state_mgr, registry = _setup_run(
            project_dir, resume=False, run_id=None, prompt="build auth"
        )
        assert run_id.startswith("run-")
        assert "build-auth" in run_id
        assert state_mgr.state_dir == registry.run_dir(run_id)
        assert state_mgr.state_dir.is_dir()

    def test_resume_finds_latest_incomplete(self, tmp_dir):
        """Resume without run_id finds the latest in_progress run."""
        project_dir = tmp_dir / "project"
        project_dir.mkdir()
        harness_dir = project_dir / ".harness"
        harness_dir.mkdir()

        # Create a completed run and an in-progress run
        reg = RunRegistry(harness_dir)
        id1 = reg.create_run(prompt="old")
        reg.update_run(id1, status="complete")
        id2 = reg.create_run(prompt="active")
        # Write some state so it looks like a real run
        sm = StateManager(reg.run_dir(id2))
        sm.update_state(phase="generator")

        run_id, state_mgr, registry = _setup_run(
            project_dir, resume=True, run_id=None, prompt=""
        )
        assert run_id == id2
        assert state_mgr.state_dir == registry.run_dir(id2)

    def test_resume_with_explicit_run_id(self, tmp_dir):
        """Resume with explicit run_id targets that specific run."""
        project_dir = tmp_dir / "project"
        project_dir.mkdir()
        harness_dir = project_dir / ".harness"
        harness_dir.mkdir()

        reg = RunRegistry(harness_dir)
        id1 = reg.create_run(prompt="target")

        run_id, state_mgr, registry = _setup_run(
            project_dir, resume=True, run_id=id1, prompt=""
        )
        assert run_id == id1

    def test_resume_no_incomplete_creates_new(self, tmp_dir):
        """Resume when all runs are complete creates a new run."""
        project_dir = tmp_dir / "project"
        project_dir.mkdir()
        harness_dir = project_dir / ".harness"
        harness_dir.mkdir()

        reg = RunRegistry(harness_dir)
        id1 = reg.create_run(prompt="done")
        reg.update_run(id1, status="complete")

        run_id, state_mgr, registry = _setup_run(
            project_dir, resume=True, run_id=None, prompt="new work"
        )
        assert run_id != id1
        assert len(registry.list_runs()) == 2

    def test_legacy_state_dir_migrates(self, tmp_dir):
        """Old .harness/state/ layout is migrated on first _setup_run."""
        project_dir = tmp_dir / "project"
        project_dir.mkdir()
        harness_dir = project_dir / ".harness"
        harness_dir.mkdir()
        legacy_dir = harness_dir / "state"
        legacy_dir.mkdir()
        # Write legacy state
        from src.core.state import atomic_write
        atomic_write(legacy_dir / "state.json", {
            "phase": "generator",
            "planner_complete": True,
        })
        (legacy_dir / "work_plan.json").write_text('{"phases": []}')

        run_id, state_mgr, registry = _setup_run(
            project_dir, resume=False, run_id=None, prompt="after migration"
        )
        runs = registry.list_runs()
        # Should have legacy migrated + new run
        assert len(runs) == 2
        assert runs[0]["run_id"] == "run-legacy-migrated"
        # Legacy dir should be gone
        assert not legacy_dir.exists()

    def test_two_runs_are_fully_isolated(self, tmp_dir):
        """Two runs have independent state — writing to one doesn't affect the other."""
        project_dir = tmp_dir / "project"
        project_dir.mkdir()

        id1, sm1, reg = _setup_run(project_dir, resume=False, prompt="run 1")
        id2, sm2, _ = _setup_run(project_dir, resume=False, prompt="run 2")

        sm1.update_state(phase="generator", iteration=5)
        sm2.update_state(phase="evaluator", iteration=10)

        assert sm1.load_state()["phase"] == "generator"
        assert sm2.load_state()["phase"] == "evaluator"
        assert sm1.state_dir != sm2.state_dir


class TestRunAgentSessionCli:
    """Tests for async CLI session runner."""

    def test_cli_session_is_async_and_nonblocking(self, tmp_dir):
        """Two CLI sessions run concurrently, not sequentially."""
        timestamps = []

        async def run_two():
            async def timed_session(label):
                # Use 'echo' as a fast CLI stand-in
                result = await run_agent_session_cli(
                    prompt="hello",
                    project_dir=tmp_dir,
                    options={"model": "echo"},  # model doesn't matter, we mock
                )
                timestamps.append((label, time.time()))
                return result

            # Patch to use 'echo' instead of 'claude'
            with patch("src.core.orchestrator.asyncio.create_subprocess_exec") as mock_exec:
                async def fake_proc(*args, **kwargs):
                    proc = AsyncMock()
                    proc.communicate = AsyncMock(return_value=(b"output", b""))
                    proc.returncode = 0
                    proc.kill = MagicMock()
                    proc.wait = AsyncMock()
                    # Small delay to simulate work
                    await asyncio.sleep(0.05)
                    return proc

                mock_exec.side_effect = fake_proc

                t0 = time.time()
                await asyncio.gather(
                    timed_session("a"),
                    timed_session("b"),
                )
                elapsed = time.time() - t0

            # Both should complete in ~0.05s (concurrent), not ~0.1s (sequential)
            assert elapsed < 0.15  # generous margin
            assert len(timestamps) == 2

        asyncio.run(run_two())

    def test_cli_session_returns_output(self, tmp_dir):
        """CLI session captures stdout."""
        async def run():
            with patch("src.core.orchestrator.asyncio.create_subprocess_exec") as mock_exec:
                proc = AsyncMock()
                proc.communicate = AsyncMock(return_value=(b"hello world", b""))
                proc.returncode = 0
                mock_exec.return_value = proc

                result = await run_agent_session_cli("test", tmp_dir)
                assert result["status"] == "continue"
                assert "hello world" in result["output"]

        asyncio.run(run())

    def test_cli_session_timeout_kills_process(self, tmp_dir):
        """CLI session kills process on timeout."""
        async def run():
            with patch("src.core.orchestrator.asyncio.create_subprocess_exec") as mock_exec:
                proc = AsyncMock()
                # communicate never returns — simulates hang
                proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
                proc.kill = MagicMock()
                proc.wait = AsyncMock()
                proc.returncode = -9
                mock_exec.return_value = proc

                # Patch wait_for timeout to something short
                result = await run_agent_session_cli("test", tmp_dir)
                assert result["status"] == "error"
                assert "timed out" in result["output"]
                assert "timed out" in result["error"]
                assert "timed out" in result["error_reason"]
                proc.kill.assert_called_once()

        asyncio.run(run())

    def test_cli_session_nonzero_exit_returns_error(self, tmp_dir):
        """Non-zero exit code is an error regardless of output."""
        async def run():
            with patch("src.core.orchestrator.asyncio.create_subprocess_exec") as mock_exec:
                proc = AsyncMock()
                proc.communicate = AsyncMock(return_value=(b"some output", b"rate limit exceeded"))
                proc.returncode = 1
                mock_exec.return_value = proc

                result = await run_agent_session_cli("test", tmp_dir)
                assert result["status"] == "error"
                assert result["error"] == "exit code 1"
                assert "exit code 1" in result["error_reason"]

        asyncio.run(run())

    def test_cli_session_surfaces_result_error_text(self, tmp_dir):
        """Claude CLI result errors should preserve the actual result text."""
        async def run():
            with patch("src.core.orchestrator.asyncio.create_subprocess_exec") as mock_exec:
                proc = AsyncMock()
                proc.communicate = AsyncMock(
                    return_value=(
                        b'{"type":"result","subtype":"success","is_error":true,"result":"Not logged in \\u00b7 Please run /login","duration_ms":12,"duration_api_ms":0,"num_turns":1}\n',
                        b"",
                    )
                )
                proc.returncode = 1
                mock_exec.return_value = proc

                result = await run_agent_session_cli("test", tmp_dir)
                assert result["status"] == "error"
                assert result["error"] == "Not logged in \u00b7 Please run /login"
                assert result["error_reason"] == "Not logged in \u00b7 Please run /login"
                assert "Not logged in" in result["output"]

        asyncio.run(run())

    def test_cli_session_zero_exit_returns_continue(self, tmp_dir):
        """Zero exit code with output returns continue."""
        async def run():
            with patch("src.core.orchestrator.asyncio.create_subprocess_exec") as mock_exec:
                proc = AsyncMock()
                proc.communicate = AsyncMock(return_value=(b"real output here", b""))
                proc.returncode = 0
                mock_exec.return_value = proc

                result = await run_agent_session_cli("test", tmp_dir)
                assert result["status"] == "continue"

        asyncio.run(run())


class TestRelayCache:
    def test_hits_when_briefs_unchanged(self, tmp_path):
        from src.core.discovery import write_brief
        from src.core.orchestrator import _get_cached_relay, _relay_cache

        _relay_cache.clear()
        write_brief("## Agent: a | Feature: f1\n**Files:** src/foo.go", 1, "a", tmp_path)

        r1 = _get_cached_relay(tmp_path, wave_num=1)
        r2 = _get_cached_relay(tmp_path, wave_num=1)
        assert r1 == r2
        assert r1 != ""
        assert 1 in _relay_cache  # wave cached

    def test_invalidates_when_new_brief_lands(self, tmp_path):
        import os

        from src.core.discovery import write_brief
        from src.core.orchestrator import _get_cached_relay, _relay_cache

        _relay_cache.clear()
        write_brief("## Agent: a | Feature: f1\n**Files:** src/foo.go", 1, "a", tmp_path)

        r1 = _get_cached_relay(tmp_path, wave_num=1)
        assert "src/foo.go" in r1
        assert "src/bar.go" not in r1

        # New brief lands. To survive 1-second mtime resolution on some
        # filesystems, explicitly bump the new brief's mtime forward instead
        # of sleeping.
        new_brief = write_brief(
            "## Agent: b | Feature: f2\n**Files:** src/bar.go", 1, "b", tmp_path)
        future = new_brief.stat().st_mtime + 5
        os.utime(new_brief, (future, future))

        r2 = _get_cached_relay(tmp_path, wave_num=1)
        assert "src/foo.go" in r2
        assert "src/bar.go" in r2

    def test_returns_empty_when_no_briefs(self, tmp_path):
        from src.core.orchestrator import _get_cached_relay, _relay_cache

        _relay_cache.clear()
        assert _get_cached_relay(tmp_path, wave_num=1) == ""
