"""Tests for fleet_session.py -- fleet wave execution state management."""

import json
import tempfile
from pathlib import Path

import pytest

from src.core.fleet_session import (
    _fleet_dir,
    _session_path,
    load_session,
    save_session,
    init_session,
    add_to_queue,
    start_wave,
    record_agent_result,
    complete_wave,
    mark_feature_complete,
    mark_feature_conflict,
    mark_feature_failed,
    requeue_feature,
    add_discovery,
    get_requeued_features,
    clear_requeued,
    complete_session,
    is_wave_all_failed,
    get_session_summary,
)


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def state_dir(tmp_dir):
    """Simulate .harness/state/ directory so fleet/ becomes a sibling."""
    sd = tmp_dir / ".harness" / "state"
    sd.mkdir(parents=True)
    return sd


class TestFleetDir:
    def test_fleet_dir_is_sibling_of_state(self, state_dir):
        fd = _fleet_dir(state_dir)
        assert fd == state_dir.parent / "fleet"
        assert fd.name == "fleet"

    def test_session_path(self, state_dir):
        sp = _session_path(state_dir)
        assert sp == state_dir.parent / "fleet" / "session.json"


class TestInitSession:
    def test_creates_proper_structure(self, state_dir):
        session = init_session(state_dir, total_layers=5)
        assert session["status"] == "active"
        assert session["current_wave"] == 0
        assert session["total_layers"] == 5
        assert session["work_queue"] == []
        assert session["requeued_features"] == []
        assert session["discoveries"] == []
        assert session["wave_results"] == {}
        assert session["completed_features"] == []
        assert session["failed_features"] == []
        assert session["conflict_features"] == []
        assert session["started_at"] is not None

    def test_persists_to_disk(self, state_dir):
        init_session(state_dir, total_layers=3)
        path = _session_path(state_dir)
        assert path.exists()
        with open(path) as f:
            data = json.load(f)
        assert data["status"] == "active"
        assert data["total_layers"] == 3


class TestLoadSaveSession:
    def test_load_missing_returns_default(self, state_dir):
        session = load_session(state_dir)
        assert session["status"] == "inactive"
        assert session["work_queue"] == []
        assert session["total_layers"] == 0

    def test_roundtrip(self, state_dir):
        original = init_session(state_dir, total_layers=4)
        original["current_wave"] = 2
        save_session(original, state_dir)
        loaded = load_session(state_dir)
        assert loaded["current_wave"] == 2
        assert loaded["total_layers"] == 4
        assert loaded["status"] == "active"

    def test_save_creates_fleet_dir(self, state_dir):
        session = {"status": "test", "work_queue": []}
        save_session(session, state_dir)
        assert _fleet_dir(state_dir).exists()


class TestAddToQueue:
    def test_adds_entry_with_correct_fields(self, state_dir):
        session = init_session(state_dir, total_layers=3)
        add_to_queue(session, "001", "Build API auth", ["src/api/"], wave=1)
        assert len(session["work_queue"]) == 1
        entry = session["work_queue"][0]
        assert entry["feature_id"] == "001"
        assert entry["desc"] == "Build API auth"
        assert entry["scope"] == ["src/api/"]
        assert entry["wave"] == 1
        assert entry["agent_id"] is None
        assert entry["status"] == "pending"

    def test_multiple_items(self, state_dir):
        session = init_session(state_dir, total_layers=3)
        add_to_queue(session, "001", "API", ["src/api/"], wave=1)
        add_to_queue(session, "002", "Frontend", ["src/ui/"], wave=1)
        add_to_queue(session, "003", "Integration", ["src/"], wave=2)
        assert len(session["work_queue"]) == 3


class TestStartAndCompleteWave:
    def test_start_wave(self, state_dir):
        session = init_session(state_dir, total_layers=3)
        start_wave(session, wave=1, agent_ids=["agent-a1b2", "agent-c3d4"])
        assert session["current_wave"] == 1
        wave_data = session["wave_results"]["1"]
        assert wave_data["status"] == "running"
        assert wave_data["started_at"] is not None
        assert wave_data["completed_at"] is None
        assert "agent-a1b2" in wave_data["agents"]
        assert "agent-c3d4" in wave_data["agents"]

    def test_complete_wave(self, state_dir):
        session = init_session(state_dir, total_layers=3)
        start_wave(session, wave=1, agent_ids=["agent-a1b2"])
        complete_wave(session, wave=1)
        wave_data = session["wave_results"]["1"]
        assert wave_data["status"] == "complete"
        assert wave_data["completed_at"] is not None

    def test_complete_wave_missing_is_noop(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        # No exception when completing a wave that was never started
        complete_wave(session, wave=99)
        assert "99" not in session["wave_results"]


class TestRecordAgentResult:
    def test_records_under_correct_wave(self, state_dir):
        session = init_session(state_dir, total_layers=3)
        start_wave(session, wave=1, agent_ids=["agent-a1b2"])
        record_agent_result(session, wave=1, agent_id="agent-a1b2",
                            feature_id="001", status="complete",
                            brief_path="briefs/w1-agent-a1b2.md")
        agent_data = session["wave_results"]["1"]["agents"]["agent-a1b2"]
        assert agent_data["feature_id"] == "001"
        assert agent_data["status"] == "complete"
        assert agent_data["brief_path"] == "briefs/w1-agent-a1b2.md"

    def test_records_without_prior_start(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        record_agent_result(session, wave=5, agent_id="agent-x",
                            feature_id="010", status="failed")
        assert "5" in session["wave_results"]
        assert session["wave_results"]["5"]["agents"]["agent-x"]["status"] == "failed"

    def test_records_conflict_status(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        start_wave(session, wave=1, agent_ids=["agent-c3d4"])
        record_agent_result(session, wave=1, agent_id="agent-c3d4",
                            feature_id="002", status="conflict")
        assert session["wave_results"]["1"]["agents"]["agent-c3d4"]["status"] == "conflict"


class TestMarkFeatureComplete:
    def test_adds_to_completed_list(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        add_to_queue(session, "001", "API", ["src/api/"], wave=1)
        mark_feature_complete(session, "001")
        assert "001" in session["completed_features"]

    def test_updates_work_queue_status(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        add_to_queue(session, "001", "API", ["src/api/"], wave=1)
        mark_feature_complete(session, "001")
        assert session["work_queue"][0]["status"] == "complete"

    def test_no_duplicates(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        mark_feature_complete(session, "001")
        mark_feature_complete(session, "001")
        assert session["completed_features"].count("001") == 1


class TestMarkFeatureConflict:
    def test_adds_to_conflict_and_requeued(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        add_to_queue(session, "002", "Frontend", ["src/ui/"], wave=1)
        mark_feature_conflict(session, "002")
        assert "002" in session["conflict_features"]
        assert "002" in session["requeued_features"]

    def test_updates_work_queue_status(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        add_to_queue(session, "002", "Frontend", ["src/ui/"], wave=1)
        mark_feature_conflict(session, "002")
        assert session["work_queue"][0]["status"] == "conflict"


class TestMarkFeatureFailed:
    def test_adds_to_failed_list(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        add_to_queue(session, "003", "Integration", ["src/"], wave=2)
        mark_feature_failed(session, "003", reason="tests_broken")
        assert "003" in session["failed_features"]

    def test_updates_work_queue_status_and_reason(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        add_to_queue(session, "003", "Integration", ["src/"], wave=2)
        mark_feature_failed(session, "003", reason="tests_broken")
        entry = session["work_queue"][0]
        assert entry["status"] == "failed"
        assert entry["fail_reason"] == "tests_broken"

    def test_no_duplicates(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        mark_feature_failed(session, "003")
        mark_feature_failed(session, "003")
        assert session["failed_features"].count("003") == 1


class TestRequeueFeature:
    def test_adds_to_requeued_and_conflict(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        requeue_feature(session, "002", reason="conflict")
        assert "002" in session["requeued_features"]
        assert "002" in session["conflict_features"]

    def test_non_conflict_reason_skips_conflict_list(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        requeue_feature(session, "005", reason="dependency")
        assert "005" in session["requeued_features"]
        assert "005" not in session["conflict_features"]

    def test_no_duplicates(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        requeue_feature(session, "002", reason="conflict")
        requeue_feature(session, "002", reason="conflict")
        assert session["requeued_features"].count("002") == 1
        assert session["conflict_features"].count("002") == 1


class TestDiscoveries:
    def test_add_discovery(self, state_dir):
        session = init_session(state_dir, total_layers=3)
        add_discovery(session, wave=1, discovery="API has rate limiting at 100 req/min")
        assert len(session["discoveries"]) == 1
        assert session["discoveries"][0]["wave"] == 1
        assert "rate limiting" in session["discoveries"][0]["discovery"]

    def test_multiple_discoveries(self, state_dir):
        session = init_session(state_dir, total_layers=3)
        add_discovery(session, wave=1, discovery="Discovery A")
        add_discovery(session, wave=1, discovery="Discovery B")
        add_discovery(session, wave=2, discovery="Discovery C")
        assert len(session["discoveries"]) == 3


class TestGetRequeuedAndClear:
    def test_get_requeued_features(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        requeue_feature(session, "002")
        requeue_feature(session, "005", reason="dependency")
        result = get_requeued_features(session)
        assert result == ["002", "005"]

    def test_returns_copy(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        requeue_feature(session, "002")
        result = get_requeued_features(session)
        result.append("999")
        assert "999" not in session["requeued_features"]

    def test_clear_requeued(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        requeue_feature(session, "002")
        requeue_feature(session, "005", reason="dependency")
        clear_requeued(session)
        assert session["requeued_features"] == []


class TestIsWaveAllFailed:
    def test_all_failed_returns_true(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        start_wave(session, wave=1, agent_ids=["a1", "a2"])
        record_agent_result(session, 1, "a1", "001", "failed")
        record_agent_result(session, 1, "a2", "002", "timeout")
        assert is_wave_all_failed(session, 1) is True

    def test_mixed_results_returns_false(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        start_wave(session, wave=1, agent_ids=["a1", "a2"])
        record_agent_result(session, 1, "a1", "001", "complete")
        record_agent_result(session, 1, "a2", "002", "failed")
        assert is_wave_all_failed(session, 1) is False

    def test_empty_wave_returns_true(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        # Wave never started -- no wave_results entry
        assert is_wave_all_failed(session, 1) is True

    def test_wave_with_no_agents_returns_true(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        start_wave(session, wave=1, agent_ids=[])
        assert is_wave_all_failed(session, 1) is True

    def test_all_complete_returns_false(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        start_wave(session, wave=1, agent_ids=["a1", "a2"])
        record_agent_result(session, 1, "a1", "001", "complete")
        record_agent_result(session, 1, "a2", "002", "complete")
        assert is_wave_all_failed(session, 1) is False

    def test_conflict_not_counted_as_failure(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        start_wave(session, wave=1, agent_ids=["a1"])
        record_agent_result(session, 1, "a1", "001", "conflict")
        assert is_wave_all_failed(session, 1) is False


class TestCompleteSession:
    def test_sets_status_and_timestamp(self, state_dir):
        session = init_session(state_dir, total_layers=2)
        complete_session(session)
        assert session["status"] == "completed"
        assert "completed_at" in session
        assert session["completed_at"] is not None


class TestGetSessionSummary:
    def test_correct_counts(self, state_dir):
        session = init_session(state_dir, total_layers=3)

        # Set up two waves
        start_wave(session, wave=1, agent_ids=["a1", "a2"])
        complete_wave(session, wave=1)
        start_wave(session, wave=2, agent_ids=["a3"])
        complete_wave(session, wave=2)

        # Features
        mark_feature_complete(session, "001")
        mark_feature_complete(session, "002")
        mark_feature_failed(session, "003")
        requeue_feature(session, "004")

        # Discoveries
        add_discovery(session, wave=1, discovery="Found config")
        add_discovery(session, wave=2, discovery="API uses rate limits")

        summary = get_session_summary(session)
        assert summary == {
            "waves_completed": 2,
            "features_done": 2,
            "features_failed": 1,
            "features_requeued": 1,
            "discoveries_count": 2,
        }

    def test_empty_session_summary(self, state_dir):
        session = init_session(state_dir, total_layers=1)
        summary = get_session_summary(session)
        assert summary == {
            "waves_completed": 0,
            "features_done": 0,
            "features_failed": 0,
            "features_requeued": 0,
            "discoveries_count": 0,
        }
