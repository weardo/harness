"""Tests for src/core/event_tracker.py — EventTracker lifecycle events."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from src.core.event_tracker import EventTracker
from src.core.control_plane import ControlPlaneClient


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def mock_cp():
    """Mock ControlPlaneClient with a spy on post_event."""
    cp = MagicMock(spec=ControlPlaneClient)
    return cp


@pytest.fixture
def tracker(mock_cp):
    return EventTracker(mock_cp)


class TestEventTrackerSetup:
    def test_run_started_calls_post_event(self, tracker, mock_cp):
        tracker.run_started(features_planned=50)
        mock_cp.post_event.assert_called_once()
        kwargs = mock_cp.post_event.call_args
        assert "setup-run-start" in str(kwargs)

    def test_worktree_sweep_calls_post_event(self, tracker, mock_cp):
        tracker.worktree_sweep(phase="start", removed_count=3)
        mock_cp.post_event.assert_called_once()
        kwargs = mock_cp.post_event.call_args
        assert "setup-worktree-sweep" in str(kwargs)

    def test_browser_preflight_calls_post_event(self, tracker, mock_cp):
        tracker.browser_preflight(result="ok")
        mock_cp.post_event.assert_called_once()
        kwargs = mock_cp.post_event.call_args
        assert "setup-browser-preflight" in str(kwargs)

    def test_knowledge_query_calls_post_event(self, tracker, mock_cp):
        tracker.knowledge_query(chunks_found=10)
        mock_cp.post_event.assert_called_once()
        kwargs = mock_cp.post_event.call_args
        assert "setup-knowledge-query" in str(kwargs)

    def test_setup_methods_never_raise_on_exception(self, mock_cp):
        mock_cp.post_event.side_effect = Exception("cp is down")
        t = EventTracker(mock_cp)
        t.run_started(10)       # must not raise
        t.worktree_sweep("end", 0)
        t.browser_preflight("timeout")
        t.knowledge_query(0)


class TestEventTrackerPlanner:
    def test_planner_role_start_uses_feature_start_type(self, tracker, mock_cp):
        tracker.planner_role_start("architect", "Designing spec")
        mock_cp.post_event.assert_called_once()
        kwargs = mock_cp.post_event.call_args.kwargs
        assert kwargs.get("event_type") == "feature_start"
        assert kwargs.get("feature_id") == "planner-architect"

    def test_planner_role_pass_uses_feature_pass_type(self, tracker, mock_cp):
        tracker.planner_role_pass("architect", "draft_work_plan.json", 1200)
        kwargs = mock_cp.post_event.call_args.kwargs
        assert kwargs.get("event_type") == "feature_pass"
        assert kwargs.get("feature_id") == "planner-architect"

    def test_planner_role_fail_uses_feature_fail_type(self, tracker, mock_cp):
        tracker.planner_role_fail("adversary", "too few gaps")
        kwargs = mock_cp.post_event.call_args.kwargs
        assert kwargs.get("event_type") == "feature_fail"
        assert kwargs.get("feature_id") == "planner-adversary"

    def test_planner_role_retry_calls_post_event(self, tracker, mock_cp):
        tracker.planner_role_retry("refiner")
        mock_cp.post_event.assert_called_once()
        assert "planner-refiner-retry" in str(mock_cp.post_event.call_args)

    def test_planner_validation_summary_calls_post_event(self, tracker, mock_cp):
        tracker.planner_validation_summary(["architect", "adversary"], 1.50)
        mock_cp.post_event.assert_called_once()
        assert "planner-summary" in str(mock_cp.post_event.call_args)

    def test_planner_methods_never_raise(self, mock_cp):
        mock_cp.post_event.side_effect = Exception("network error")
        t = EventTracker(mock_cp)
        t.planner_role_start("architect", "desc")
        t.planner_role_pass("architect", "art", 100)
        t.planner_role_fail("architect", "reason")
        t.planner_role_retry("architect")
        t.planner_validation_summary([], 0.0)


class TestEventTrackerFeatures:
    def test_feature_start_calls_post_event(self, tracker, mock_cp):
        tracker.feature_start("001", "Build login")
        mock_cp.post_event.assert_called_once()
        kwargs = mock_cp.post_event.call_args.kwargs
        assert kwargs.get("event_type") == "feature_start"
        assert kwargs.get("feature_id") == "001"

    def test_feature_pass_calls_post_event(self, tracker, mock_cp):
        tracker.feature_pass("001", "Build login", 3000)
        kwargs = mock_cp.post_event.call_args.kwargs
        assert kwargs.get("event_type") == "feature_pass"

    def test_feature_fail_calls_post_event(self, tracker, mock_cp):
        tracker.feature_fail("001", "Build login", "timeout")
        kwargs = mock_cp.post_event.call_args.kwargs
        assert kwargs.get("event_type") == "feature_fail"

    def test_feature_retry_calls_post_event(self, tracker, mock_cp):
        tracker.feature_retry("001", "Build login", 2)
        mock_cp.post_event.assert_called_once()

    def test_cb_open_calls_post_event(self, tracker, mock_cp):
        tracker.cb_open("no progress")
        mock_cp.post_event.assert_called_once()

    def test_cb_close_calls_post_event(self, tracker, mock_cp):
        tracker.cb_close()
        mock_cp.post_event.assert_called_once()

    def test_suspicion_warning_uses_completion_prefix(self, tracker, mock_cp):
        tracker.suspicion_warning(["too fast", "no output"])
        assert "completion-suspicion" in str(mock_cp.post_event.call_args)

    def test_run_complete_uses_completion_run(self, tracker, mock_cp):
        tracker.run_complete(42, 2.50)
        assert "completion-run" in str(mock_cp.post_event.call_args)

    def test_run_failed_calls_post_event(self, tracker, mock_cp):
        tracker.run_failed()
        mock_cp.post_event.assert_called_once()
        assert "completion-failed" in str(mock_cp.post_event.call_args)

    def test_retro_generated_uses_completion_retro(self, tracker, mock_cp):
        tracker.retro_generated()
        assert "completion-retro" in str(mock_cp.post_event.call_args)

    def test_knowledge_ingested_uses_completion_knowledge(self, tracker, mock_cp):
        tracker.knowledge_ingested(5)
        assert "completion-knowledge" in str(mock_cp.post_event.call_args)


class TestEventTrackerFireAndForget:
    """Verify ALL methods are truly fire-and-forget — no exception ever propagates."""

    def setup_method(self):
        self.mock_cp = MagicMock(spec=ControlPlaneClient)
        self.mock_cp.post_event.side_effect = RuntimeError("control plane exploded")
        self.tracker = EventTracker(self.mock_cp)

    def test_run_started_fire_and_forget(self):
        self.tracker.run_started(100)  # must not raise

    def test_planner_role_start_fire_and_forget(self):
        self.tracker.planner_role_start("architect", "desc")

    def test_feature_start_fire_and_forget(self):
        self.tracker.feature_start("001", "desc")

    def test_run_complete_fire_and_forget(self):
        self.tracker.run_complete(10, 1.0)

    def test_worktree_sweep_fire_and_forget(self):
        self.tracker.worktree_sweep("start", 5)

    def test_suspicion_warning_fire_and_forget(self):
        self.tracker.suspicion_warning(["reason"])

    def test_knowledge_ingested_fire_and_forget(self):
        self.tracker.knowledge_ingested(3)

    def test_feature_fail_fire_and_forget(self):
        self.tracker.feature_fail("001", "desc", "error")

    def test_run_failed_fire_and_forget(self):
        self.tracker.run_failed()

    def test_retro_generated_fire_and_forget(self):
        self.tracker.retro_generated()

    def test_planner_role_reject_fire_and_forget(self):
        self.tracker.planner_role_reject("validator", issues_count=4, cost_usd=0.67)


class TestEventTrackerEnhancedTelemetry:
    """Tests for enhanced planner role events with cost, duration, attempt."""

    @pytest.fixture(autouse=True)
    def setup(self, mock_cp, tracker):
        self.mock_cp = mock_cp
        self.tracker = tracker

    def test_planner_role_pass_includes_cost(self):
        self.tracker.planner_role_pass("architect", "draft_work_plan.json", 960000, cost_usd=1.23)
        desc = self.mock_cp.post_event.call_args.kwargs.get(
            "feature_desc", str(self.mock_cp.post_event.call_args)
        )
        assert "$1.23" in desc

    def test_planner_role_pass_includes_attempt(self):
        self.tracker.planner_role_pass("validator", "validation.json", 500000, attempt=2)
        desc = self.mock_cp.post_event.call_args.kwargs.get(
            "feature_desc", str(self.mock_cp.post_event.call_args)
        )
        assert "attempt 2" in desc

    def test_planner_role_pass_no_attempt_when_1(self):
        self.tracker.planner_role_pass("architect", "draft.json", 100000, attempt=1)
        desc = self.mock_cp.post_event.call_args.kwargs.get(
            "feature_desc", str(self.mock_cp.post_event.call_args)
        )
        assert "attempt" not in desc

    def test_planner_role_fail_includes_cost(self):
        self.tracker.planner_role_fail("refiner", "timeout", cost_usd=0.45)
        desc = self.mock_cp.post_event.call_args.kwargs.get(
            "feature_desc", str(self.mock_cp.post_event.call_args)
        )
        assert "$0.45" in desc

    def test_planner_role_reject_includes_issues(self):
        self.tracker.planner_role_reject("validator", issues_count=4, cost_usd=0.67, attempt=1)
        desc = self.mock_cp.post_event.call_args.kwargs.get(
            "feature_desc", str(self.mock_cp.post_event.call_args)
        )
        assert "REJECTED" in desc
        assert "4 issues" in desc
        assert "$0.67" in desc

    def test_planner_role_pass_backward_compat(self):
        """Old callers passing only 3 args still work."""
        self.tracker.planner_role_pass("architect", "draft.json", 1000)
        self.mock_cp.post_event.assert_called_once()
