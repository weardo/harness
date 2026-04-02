"""Tests for completion.py — shared completion logic."""

import tempfile
from pathlib import Path

import pytest

from src.core.completion import check_completion, check_exit_conditions, check_suspicion
from src.core.state import StateManager


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def harness_dir(tmp_dir):
    hd = tmp_dir / "harness"
    hd.mkdir()
    (hd / "state").mkdir()
    return hd


class TestCheckCompletion:
    def test_no_features_yet(self, harness_dir):
        result = check_completion(harness_dir)
        assert result["complete"] is False
        assert "No features" in result["reason"]

    def test_features_remaining(self, harness_dir):
        mgr = StateManager(harness_dir / "state")
        mgr.save_feature_list([
            {"id": "001", "passes": True, "blocked": False},
            {"id": "002", "passes": False, "blocked": False},
            {"id": "003", "passes": False, "blocked": False},
        ])
        result = check_completion(harness_dir)
        assert result["complete"] is False
        assert "2 features remaining" in result["reason"]
        assert result["progress"] is True  # 1 passing = progress

    def test_all_passing_but_no_signals(self, harness_dir):
        mgr = StateManager(harness_dir / "state")
        mgr.save_feature_list([
            {"id": "001", "passes": True, "blocked": False},
            {"id": "002", "passes": True, "blocked": False},
        ])
        result = check_completion(harness_dir)
        assert result["complete"] is False
        assert "completion signals" in result["reason"]

    def test_all_passing_with_signals(self, harness_dir):
        mgr = StateManager(harness_dir / "state")
        mgr.save_feature_list([
            {"id": "001", "passes": True, "blocked": False},
            {"id": "002", "passes": True, "blocked": False},
        ])
        mgr.record_exit_signal("completion", 5)
        mgr.record_exit_signal("completion", 6)
        result = check_completion(harness_dir)
        assert result["complete"] is True

    def test_all_blocked_counts_as_complete(self, harness_dir):
        mgr = StateManager(harness_dir / "state")
        mgr.save_feature_list([
            {"id": "001", "passes": True, "blocked": False},
            {"id": "002", "passes": False, "blocked": True},
        ])
        mgr.record_exit_signal("completion", 5)
        mgr.record_exit_signal("completion", 6)
        # remaining = 0 (1 passing + 1 blocked)
        result = check_completion(harness_dir)
        assert result["complete"] is True

    def test_no_progress_when_zero_passing(self, harness_dir):
        mgr = StateManager(harness_dir / "state")
        mgr.save_feature_list([
            {"id": "001", "passes": False, "blocked": False},
        ])
        result = check_completion(harness_dir)
        assert result["progress"] is False


class TestCheckExitConditions:
    def test_budget_exceeded(self):
        state = {"total_cost_usd": 55.0, "iteration": 5}
        config = {"max_cost_usd": 50.0}
        result = check_exit_conditions(state, config)
        assert result is not None
        assert result["should_exit"] is True
        assert "Budget" in result["reason"]

    def test_budget_not_exceeded(self):
        state = {"total_cost_usd": 30.0, "iteration": 5}
        config = {"max_cost_usd": 50.0}
        result = check_exit_conditions(state, config)
        assert result is None

    def test_time_exceeded(self):
        state = {"total_cost_usd": 0, "iteration": 5}
        config = {"max_duration_minutes": 60}
        result = check_exit_conditions(state, config, elapsed_seconds=3700)
        assert result is not None
        assert "Duration" in result["reason"]

    def test_time_not_exceeded(self):
        state = {"total_cost_usd": 0, "iteration": 5}
        config = {"max_duration_minutes": 60}
        result = check_exit_conditions(state, config, elapsed_seconds=1800)
        assert result is None

    def test_iterations_exceeded(self):
        state = {"total_cost_usd": 0, "iteration": 50}
        config = {"max_iterations": 50}
        result = check_exit_conditions(state, config)
        assert result is not None
        assert "Iteration" in result["reason"]

    def test_no_limits_set(self):
        state = {"total_cost_usd": 999, "iteration": 999}
        config = {}
        result = check_exit_conditions(state, config, elapsed_seconds=999999)
        assert result is None

    def test_zero_limits_mean_unlimited(self):
        state = {"total_cost_usd": 999, "iteration": 999}
        config = {"max_cost_usd": 0, "max_iterations": 0, "max_duration_minutes": 0}
        result = check_exit_conditions(state, config, elapsed_seconds=999999)
        assert result is None


class TestCheckSuspicion:
    @pytest.fixture
    def tmp_state_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d) / "state"

    @pytest.fixture
    def state_mgr(self, tmp_state_dir):
        return StateManager(tmp_state_dir)

    def _make_features(self, state_mgr, count, passing=True, retries=0):
        """Helper: create `count` features all passing with given retries."""
        features = [
            {
                "id": str(i + 1).zfill(3),
                "passes": passing,
                "blocked": False,
                "retries": retries,
            }
            for i in range(count)
        ]
        state_mgr.save_feature_list(features)
        return features

    def test_suspicious_clean_run(self, state_mgr):
        """Zero retries + zero CB opens across all features → suspicious."""
        self._make_features(state_mgr, 3, passing=True, retries=0)
        state_mgr.update_state(evaluator_sessions=3)

        result = check_suspicion(state_mgr, config={}, cb_total_opens=0, elapsed_seconds=300)

        assert result["suspicious"] is True
        assert any("first-attempt pass rate" in r for r in result["reasons"])

    def test_not_suspicious_with_retries(self, state_mgr):
        """At least one retry present + sufficient evaluator sessions → not suspicious."""
        features = [
            {"id": "001", "passes": True, "blocked": False, "retries": 2},
            {"id": "002", "passes": True, "blocked": False, "retries": 0},
            {"id": "003", "passes": True, "blocked": False, "retries": 0},
        ]
        state_mgr.save_feature_list(features)
        state_mgr.update_state(evaluator_sessions=3)

        # cb_total_opens=0, but retries=2 prevents check 1 from triggering
        # elapsed_seconds=300 → 300/3=100s per feature > 30s min → check 3 ok
        result = check_suspicion(state_mgr, config={}, cb_total_opens=0, elapsed_seconds=300)

        assert result["suspicious"] is False
        assert result["reasons"] == []

    def test_suspicious_evaluator_count(self, state_mgr):
        """Fewer evaluator sessions than total features → suspicious."""
        self._make_features(state_mgr, 5, passing=True, retries=0)
        # evaluator_sessions=2 < total_features=5 → check 2 triggers
        state_mgr.update_state(evaluator_sessions=2)

        # Use cb_total_opens=1 to suppress check 1, elapsed=2000 to suppress check 3
        result = check_suspicion(state_mgr, config={}, cb_total_opens=1, elapsed_seconds=2000)

        assert result["suspicious"] is True
        assert any("evaluator sessions" in r for r in result["reasons"])

    def test_suspicious_fast_features(self, state_mgr):
        """Average time per feature below min threshold → suspicious."""
        features = [
            {"id": "001", "passes": True, "blocked": False, "retries": 1},
            {"id": "002", "passes": True, "blocked": False, "retries": 0},
            {"id": "003", "passes": True, "blocked": False, "retries": 0},
        ]
        state_mgr.save_feature_list(features)
        # evaluator_sessions=3 to avoid check 2
        state_mgr.update_state(evaluator_sessions=3)

        # 3 passing features, 15s total → 5s avg < 30s default min → check 3 triggers
        result = check_suspicion(state_mgr, config={}, cb_total_opens=0, elapsed_seconds=15)

        assert result["suspicious"] is True
        assert any("fast average feature time" in r for r in result["reasons"])

    def test_disabled(self, state_mgr):
        """When suspicion.enabled=False, always returns clean regardless of state."""
        self._make_features(state_mgr, 5, passing=True, retries=0)
        state_mgr.update_state(evaluator_sessions=0)

        config = {"suspicion": {"enabled": False}}
        result = check_suspicion(state_mgr, config=config, cb_total_opens=0, elapsed_seconds=1)

        assert result["suspicious"] is False
        assert result["reasons"] == []

    def test_custom_thresholds(self, state_mgr):
        """Custom min_avg_feature_seconds threshold controls check 3."""
        features = [
            {"id": "001", "passes": True, "blocked": False, "retries": 1},
        ]
        state_mgr.save_feature_list(features)
        state_mgr.update_state(evaluator_sessions=1)

        # 50s for 1 feature → avg=50s
        # Default threshold (30s): 50 >= 30 → NOT suspicious on check 3
        default_result = check_suspicion(state_mgr, config={}, cb_total_opens=0, elapsed_seconds=50)
        assert not any("fast average feature time" in r for r in default_result["reasons"])

        # Custom threshold (60s): 50 < 60 → suspicious on check 3
        custom_config = {"suspicion": {"min_avg_feature_seconds": 60}}
        custom_result = check_suspicion(state_mgr, config=custom_config, cb_total_opens=0, elapsed_seconds=50)
        assert any("fast average feature time" in r for r in custom_result["reasons"])
