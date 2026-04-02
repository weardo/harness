"""Tests for state.py — atomic JSON state management."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from src.core.state import atomic_write, atomic_read, StateManager


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
