"""Tests for state.py — atomic JSON state management."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from src.core.state import atomic_write, atomic_read, StateManager, RunRegistry


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def state_mgr(tmp_dir):
    return StateManager(tmp_dir / "state")


class TestAtomicWrite:
    def test_creates_file(self, tmp_dir):
        path = tmp_dir / "test.json"
        atomic_write(path, {"key": "value"})
        assert path.exists()
        with open(path) as f:
            assert json.load(f) == {"key": "value"}

    def test_overwrites_existing(self, tmp_dir):
        path = tmp_dir / "test.json"
        atomic_write(path, {"v": 1})
        atomic_write(path, {"v": 2})
        with open(path) as f:
            assert json.load(f) == {"v": 2}

    def test_creates_parent_dirs(self, tmp_dir):
        path = tmp_dir / "a" / "b" / "c.json"
        atomic_write(path, {"nested": True})
        assert path.exists()

    def test_no_temp_files_left_on_success(self, tmp_dir):
        path = tmp_dir / "test.json"
        atomic_write(path, {"clean": True})
        temps = list(tmp_dir.glob(".harness_tmp_*"))
        assert temps == []

    def test_no_temp_files_left_on_error(self, tmp_dir):
        path = tmp_dir / "test.json"
        # Write a non-serializable object to trigger error
        class BadObj:
            pass
        with pytest.raises(TypeError):
            atomic_write(path, {"bad": BadObj()})
        temps = list(tmp_dir.glob(".harness_tmp_*"))
        assert temps == []


class TestAtomicRead:
    def test_reads_existing(self, tmp_dir):
        path = tmp_dir / "test.json"
        path.write_text('{"hello": "world"}')
        assert atomic_read(path) == {"hello": "world"}

    def test_returns_none_for_missing(self, tmp_dir):
        assert atomic_read(tmp_dir / "nope.json") is None


class TestStateManager:
    def test_default_state(self, state_mgr):
        state = state_mgr.load_state()
        assert state["phase"] == "init"
        assert state["iteration"] == 0
        assert state["total_cost_usd"] == 0.0

    def test_save_and_load(self, state_mgr):
        state_mgr.save_state({"phase": "generator", "iteration": 5})
        loaded = state_mgr.load_state()
        assert loaded["phase"] == "generator"
        assert loaded["iteration"] == 5

    def test_update_state(self, state_mgr):
        state_mgr.load_state()  # init defaults
        updated = state_mgr.update_state(phase="evaluator", iteration=3)
        assert updated["phase"] == "evaluator"
        assert updated["iteration"] == 3
        # Other defaults preserved
        assert updated["total_cost_usd"] == 0.0

    def test_feature_list_empty_default(self, state_mgr):
        assert state_mgr.load_feature_list() == []

    def test_feature_list_save_load(self, state_mgr):
        features = [
            {"id": "001", "passes": False, "blocked": False, "priority": 1},
            {"id": "002", "passes": False, "blocked": False, "priority": 2},
        ]
        state_mgr.save_feature_list(features)
        loaded = state_mgr.load_feature_list()
        assert len(loaded) == 2
        assert loaded[0]["id"] == "001"

    def test_count_features(self, state_mgr):
        features = [
            {"id": "001", "passes": True, "blocked": False},
            {"id": "002", "passes": False, "blocked": True},
            {"id": "003", "passes": False, "blocked": False},
            {"id": "004", "passes": False, "blocked": False},
        ]
        state_mgr.save_feature_list(features)
        counts = state_mgr.count_features()
        assert counts == {"total": 4, "passing": 1, "blocked": 1, "remaining": 2}

    def test_get_next_feature(self, state_mgr):
        features = [
            {"id": "001", "passes": True, "blocked": False, "priority": 1},
            {"id": "002", "passes": False, "blocked": True, "priority": 2},
            {"id": "003", "passes": False, "blocked": False, "priority": 5},
            {"id": "004", "passes": False, "blocked": False, "priority": 3},
        ]
        state_mgr.save_feature_list(features)
        next_feat = state_mgr.get_next_feature()
        assert next_feat["id"] == "004"  # priority 3, lowest non-passing non-blocked

    def test_get_next_feature_none_remaining(self, state_mgr):
        features = [
            {"id": "001", "passes": True, "blocked": False},
        ]
        state_mgr.save_feature_list(features)
        assert state_mgr.get_next_feature() is None

    def test_mark_feature_passing(self, state_mgr):
        features = [
            {"id": "001", "passes": False, "blocked": False},
        ]
        state_mgr.save_feature_list(features)
        state_mgr.mark_feature_passing("001")
        loaded = state_mgr.load_feature_list()
        assert loaded[0]["passes"] is True

    def test_mark_feature_blocked(self, state_mgr):
        features = [
            {"id": "001", "passes": False, "blocked": False},
        ]
        state_mgr.save_feature_list(features)
        state_mgr.mark_feature_blocked("001", reason="merge_conflict")
        loaded = state_mgr.load_feature_list()
        assert loaded[0]["blocked"] is True
        assert loaded[0]["block_reason"] == "merge_conflict"

    def test_increment_retries(self, state_mgr):
        features = [
            {"id": "001", "passes": False, "retries": 0},
        ]
        state_mgr.save_feature_list(features)
        count = state_mgr.increment_retries("001")
        assert count == 1
        count = state_mgr.increment_retries("001")
        assert count == 2

    def test_exit_signals_rolling_window(self, state_mgr):
        for i in range(7):
            state_mgr.record_exit_signal("completion", i)
        signals = state_mgr.load_exit_signals()
        assert signals["completion_signals"] == [2, 3, 4, 5, 6]  # last 5

    def test_feedback_write_read_clear(self, state_mgr):
        assert state_mgr.read_feedback() is None
        state_mgr.write_feedback("## Feature 001 FAIL\n- Bug in auth")
        assert "FAIL" in state_mgr.read_feedback()
        state_mgr.clear_feedback()
        assert state_mgr.read_feedback() is None


class TestRunRegistry:
    @pytest.fixture
    def harness_dir(self, tmp_dir):
        d = tmp_dir / ".harness"
        d.mkdir()
        return d

    @pytest.fixture
    def registry(self, harness_dir):
        return RunRegistry(harness_dir)

    def test_create_run_returns_run_id(self, registry):
        run_id = registry.create_run(prompt="build auth")
        assert run_id.startswith("run-")
        assert "build-auth" in run_id

    def test_create_run_creates_directory(self, registry):
        run_id = registry.create_run(prompt="build auth")
        run_dir = registry.run_dir(run_id)
        assert run_dir.is_dir()

    def test_create_run_adds_to_index(self, registry):
        run_id = registry.create_run(prompt="build auth")
        runs = registry.list_runs()
        assert len(runs) == 1
        assert runs[0]["run_id"] == run_id
        assert runs[0]["prompt"] == "build auth"
        assert runs[0]["status"] == "in_progress"
        assert runs[0]["started_at"] is not None

    def test_create_multiple_runs(self, registry):
        id1 = registry.create_run(prompt="auth")
        id2 = registry.create_run(prompt="api")
        assert id1 != id2
        runs = registry.list_runs()
        assert len(runs) == 2

    def test_run_dir_returns_correct_path(self, registry, harness_dir):
        run_id = registry.create_run(prompt="test")
        expected = harness_dir / "runs" / run_id
        assert registry.run_dir(run_id) == expected

    def test_run_dir_raises_for_unknown_id(self, registry):
        with pytest.raises(ValueError, match="not found"):
            registry.run_dir("run-nonexistent")

    def test_update_run_status(self, registry):
        run_id = registry.create_run(prompt="test")
        registry.update_run(run_id, status="complete", total_cost_usd=3.24)
        runs = registry.list_runs()
        assert runs[0]["status"] == "complete"
        assert runs[0]["total_cost_usd"] == 3.24

    def test_update_run_preserves_other_fields(self, registry):
        run_id = registry.create_run(prompt="test")
        registry.update_run(run_id, status="complete")
        runs = registry.list_runs()
        assert runs[0]["prompt"] == "test"
        assert runs[0]["started_at"] is not None

    def test_find_resumable_returns_latest_incomplete(self, registry):
        id1 = registry.create_run(prompt="old")
        registry.update_run(id1, status="complete")
        id2 = registry.create_run(prompt="new")
        assert registry.find_resumable() == id2

    def test_find_resumable_returns_none_when_all_complete(self, registry):
        id1 = registry.create_run(prompt="done")
        registry.update_run(id1, status="complete")
        assert registry.find_resumable() is None

    def test_find_resumable_returns_none_when_empty(self, registry):
        assert registry.find_resumable() is None

    def test_find_resumable_skips_failed(self, registry):
        id1 = registry.create_run(prompt="failed")
        registry.update_run(id1, status="failed")
        id2 = registry.create_run(prompt="active")
        assert registry.find_resumable() == id2

    def test_slug_sanitizes_prompt(self, registry):
        run_id = registry.create_run(prompt="Build the AUTH system!!! @#$")
        # Should be lowercase, alphanumeric + hyphens only
        slug_part = run_id.split("-", 2)[-1]  # after "run-<timestamp>-"
        assert slug_part.replace("-", "").isalnum()

    def test_empty_prompt_still_works(self, registry):
        run_id = registry.create_run()
        assert run_id.startswith("run-")
        assert registry.run_dir(run_id).is_dir()

    def test_migrate_legacy_state_dir(self, harness_dir):
        """Old .harness/state/ layout migrates to .harness/runs/."""
        state_dir = harness_dir / "state"
        state_dir.mkdir()
        # Write some legacy state
        atomic_write(state_dir / "state.json", {"phase": "generator", "planner_complete": True})
        (state_dir / "work_plan.json").write_text('{"phases": []}')
        (state_dir / "spec.md").write_text("# Spec")

        registry = RunRegistry(harness_dir)
        run_id = registry.migrate_legacy()

        # Legacy dir should be gone
        assert not state_dir.exists()
        # Run dir should have the files
        run_dir = registry.run_dir(run_id)
        assert (run_dir / "state.json").exists()
        assert (run_dir / "work_plan.json").exists()
        assert (run_dir / "spec.md").exists()
        # Index should have the entry
        runs = registry.list_runs()
        assert len(runs) == 1
        assert runs[0]["status"] == "complete"  # planner_complete was True

    def test_migrate_legacy_noop_when_no_state_dir(self, harness_dir):
        registry = RunRegistry(harness_dir)
        result = registry.migrate_legacy()
        assert result is None

    def test_migrate_legacy_ignores_empty_state_dir(self, harness_dir):
        """Empty state/ dir (from old install.sh) should not create a ghost run."""
        state_dir = harness_dir / "state"
        state_dir.mkdir()
        # No state.json — just an empty directory

        registry = RunRegistry(harness_dir)
        result = registry.migrate_legacy()
        assert result is None
        assert registry.list_runs() == []
        # Empty dir should be cleaned up
        assert not state_dir.exists()


class TestPlannerStateMachine:
    """Tests for structured planner state tracking in StateManager."""

    @pytest.fixture
    def state_mgr(self, tmp_dir):
        return StateManager(tmp_dir / "state")

    def test_get_planner_state_returns_default(self, state_mgr):
        ps = state_mgr.get_planner_state()
        assert ps["status"] == "pending"
        assert ps["roles"] == []
        assert ps["fix_loops_completed"] == 0
        assert ps["max_fix_loops"] == 2
        assert ps["total_cost_usd"] == 0.0

    def test_start_planner_sets_in_progress(self, state_mgr):
        state_mgr.start_planner()
        ps = state_mgr.get_planner_state()
        assert ps["status"] == "in_progress"
        assert ps["started_at"] is not None

    def test_start_role_appends_entry(self, state_mgr):
        state_mgr.start_planner()
        state_mgr.start_role("architect", model="claude-opus-4-6")
        ps = state_mgr.get_planner_state()
        assert len(ps["roles"]) == 1
        role = ps["roles"][0]
        assert role["name"] == "architect"
        assert role["status"] == "running"
        assert role["model"] == "claude-opus-4-6"
        assert role["started_at"] is not None
        assert role["attempt"] == 1

    def test_complete_role_fills_fields(self, state_mgr):
        state_mgr.start_planner()
        state_mgr.start_role("architect", model="opus")
        state_mgr.complete_role(
            "architect",
            cost_usd=1.23,
            artifact="draft_work_plan.json",
            artifact_size_bytes=205662,
            validation={"valid": True},
        )
        ps = state_mgr.get_planner_state()
        role = ps["roles"][0]
        assert role["status"] == "complete"
        assert role["cost_usd"] == 1.23
        assert role["artifact"] == "draft_work_plan.json"
        assert role["artifact_size_bytes"] == 205662
        assert role["completed_at"] is not None
        assert role["duration_s"] >= 0
        assert role["validation"] == {"valid": True}

    def test_complete_role_accumulates_total_cost(self, state_mgr):
        state_mgr.start_planner()
        state_mgr.start_role("architect", model="opus")
        state_mgr.complete_role("architect", cost_usd=1.0)
        state_mgr.start_role("adversary", model="opus")
        state_mgr.complete_role("adversary", cost_usd=0.5)
        ps = state_mgr.get_planner_state()
        assert ps["total_cost_usd"] == pytest.approx(1.5)

    def test_reject_role_sets_status_and_issues(self, state_mgr):
        state_mgr.start_planner()
        state_mgr.start_role("validator", model="opus")
        state_mgr.reject_role(
            "validator",
            cost_usd=0.67,
            artifact="validation.json",
            validation={"valid": False, "sign_off": False, "issues_count": 4},
        )
        ps = state_mgr.get_planner_state()
        role = ps["roles"][0]
        assert role["status"] == "rejected"
        assert role["validation"]["issues_count"] == 4

    def test_fail_role_sets_status(self, state_mgr):
        state_mgr.start_planner()
        state_mgr.start_role("architect", model="opus")
        state_mgr.fail_role("architect", reason="timeout")
        ps = state_mgr.get_planner_state()
        role = ps["roles"][0]
        assert role["status"] == "failed"
        assert role["error"] == "timeout"

    def test_fix_loop_increments_counter(self, state_mgr):
        state_mgr.start_planner()
        state_mgr.increment_fix_loops()
        ps = state_mgr.get_planner_state()
        assert ps["fix_loops_completed"] == 1

    def test_fix_loop_tracks_triggered_by(self, state_mgr):
        state_mgr.start_planner()
        # Validator rejected at attempt 1
        state_mgr.start_role("validator", model="opus")
        state_mgr.reject_role("validator", cost_usd=0.5)
        # Refiner-fix triggered by that rejection
        state_mgr.start_role("refiner-fix", model="sonnet", triggered_by="validator:attempt:1")
        ps = state_mgr.get_planner_state()
        fix_role = ps["roles"][-1]
        assert fix_role["triggered_by"] == "validator:attempt:1"

    def test_complete_planner_sets_complete(self, state_mgr):
        state_mgr.start_planner()
        state_mgr.complete_planner()
        ps = state_mgr.get_planner_state()
        assert ps["status"] == "complete"
        assert ps["completed_at"] is not None

    def test_get_resume_point_finds_running_role(self, state_mgr):
        state_mgr.start_planner()
        state_mgr.start_role("architect", model="opus")
        state_mgr.complete_role("architect", cost_usd=1.0)
        state_mgr.start_role("adversary", model="opus")
        # adversary is still "running" — crashed mid-run
        point = state_mgr.get_resume_point()
        assert point["action"] == "rerun"
        assert point["role_name"] == "adversary"

    def test_get_resume_point_after_rejection(self, state_mgr):
        state_mgr.start_planner()
        state_mgr.start_role("validator", model="opus")
        state_mgr.reject_role("validator", cost_usd=0.5)
        point = state_mgr.get_resume_point()
        assert point["action"] == "fix_loop"
        assert point["role_name"] == "validator"

    def test_get_resume_point_after_max_fix_loops(self, state_mgr):
        state_mgr.start_planner()
        state_mgr.start_role("validator", model="opus")
        state_mgr.reject_role("validator", cost_usd=0.5)
        state_mgr.increment_fix_loops()
        state_mgr.increment_fix_loops()  # now at max (2)
        point = state_mgr.get_resume_point()
        assert point["action"] == "proceed_with_warning"

    def test_get_resume_point_all_complete(self, state_mgr):
        state_mgr.start_planner()
        state_mgr.start_role("architect", model="opus")
        state_mgr.complete_role("architect", cost_usd=1.0)
        point = state_mgr.get_resume_point()
        assert point["action"] == "next_role"

    def test_get_resume_point_planner_complete(self, state_mgr):
        state_mgr.start_planner()
        state_mgr.complete_planner()
        point = state_mgr.get_resume_point()
        assert point["action"] == "skip_planner"

    def test_multiple_attempts_same_role(self, state_mgr):
        """Validator can appear multiple times in roles[] with incrementing attempts."""
        state_mgr.start_planner()
        state_mgr.start_role("validator", model="opus")
        state_mgr.reject_role("validator", cost_usd=0.5)
        state_mgr.start_role("refiner-fix", model="sonnet", triggered_by="validator:attempt:1")
        state_mgr.complete_role("refiner-fix", cost_usd=0.1)
        state_mgr.start_role("validator", model="opus")
        ps = state_mgr.get_planner_state()
        validators = [r for r in ps["roles"] if r["name"] == "validator"]
        assert len(validators) == 2
        assert validators[0]["attempt"] == 1
        assert validators[1]["attempt"] == 2

    def test_migrate_legacy_planner_roles(self, state_mgr):
        """Old planner_roles: {architect: true} migrates to structured format."""
        state_mgr.save_state({
            "phase": "generator",
            "planner_complete": True,
            "planner_roles": {"architect": True, "adversary": True, "refiner": True, "validator": True},
        })
        state_mgr.migrate_legacy_planner_state()
        ps = state_mgr.get_planner_state()
        assert ps["status"] == "complete"
        assert len(ps["roles"]) == 4
        for role in ps["roles"]:
            assert role["status"] == "complete"
            assert role["attempt"] == 1
