"""Tests for worker_assignments live_state tracking."""

import json
import tempfile
from pathlib import Path

import pytest

from src.core import worker_assignments as wa


@pytest.fixture
def state_dir():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "fleet").mkdir()
        yield d


def _make_assignment(state_dir, worker_id="worker-1", feature_id="task-001"):
    wa.add_assignment(
        state_dir=state_dir,
        worker_id=worker_id,
        feature_id=feature_id,
        branch=f"harness/{worker_id}",
        worktree_dir=f"/tmp/{worker_id}",
        agent_id="agent-abc",
        wave=1,
        scope=["src/foo.py"],
    )


class TestLiveState:
    def test_add_assignment_initializes_live_state(self, state_dir):
        _make_assignment(state_dir)
        data = wa.load_assignments(state_dir)
        a = data["assignments"]["worker-1"]
        assert a["live_state"] == "assigned"
        assert "live_state_at" in a and a["live_state_at"]

    def test_update_live_state_persists_immediately(self, state_dir):
        _make_assignment(state_dir)
        wa.update_assignment_live_state(state_dir, "worker-1", "gen-running")

        # Read the file directly — verify the on-disk state is current,
        # not waiting for some batched flush.
        path = state_dir / "fleet" / "worker_assignments.json"
        on_disk = json.loads(path.read_text())
        assert on_disk["assignments"]["worker-1"]["live_state"] == "gen-running"

    def test_update_live_state_updates_timestamp(self, state_dir):
        import time
        _make_assignment(state_dir)
        ts1 = wa.load_assignments(state_dir)["assignments"]["worker-1"]["live_state_at"]
        time.sleep(0.01)
        wa.update_assignment_live_state(state_dir, "worker-1", "gen-done")
        ts2 = wa.load_assignments(state_dir)["assignments"]["worker-1"]["live_state_at"]
        assert ts2 > ts1, "live_state_at must advance on every transition"

    def test_update_live_state_does_not_change_status_or_phase(self, state_dir):
        """live_state is observational — must not affect recovery state machine."""
        _make_assignment(state_dir)
        before = wa.load_assignments(state_dir)["assignments"]["worker-1"]
        wa.update_assignment_live_state(state_dir, "worker-1", "eval-running")
        after = wa.load_assignments(state_dir)["assignments"]["worker-1"]
        assert before["status"] == after["status"]
        assert before["phase"] == after["phase"]

    def test_update_live_state_for_unknown_worker_is_noop(self, state_dir):
        """Updating a worker that doesn't exist must not raise or create entries."""
        wa.update_assignment_live_state(state_dir, "worker-nonexistent", "gen-running")
        data = wa.load_assignments(state_dir)
        assert "worker-nonexistent" not in data["assignments"]

    def test_full_lifecycle_progression(self, state_dir):
        """Walk the live_state through a typical successful lifecycle."""
        _make_assignment(state_dir)
        states = ["gen-running", "gen-done", "eval-running", "eval-done", "merging", "merge-success"]
        for s in states:
            wa.update_assignment_live_state(state_dir, "worker-1", s)
            current = wa.load_assignments(state_dir)["assignments"]["worker-1"]
            assert current["live_state"] == s, f"expected {s}, got {current['live_state']}"

    def test_get_resumable_ignores_live_state(self, state_dir):
        """get_resumable_assignments must filter on status, not live_state."""
        _make_assignment(state_dir)
        wa.update_assignment_live_state(state_dir, "worker-1", "eval-done")
        # Status is still "running" — must be resumable regardless of live_state
        resumable = wa.get_resumable_assignments(state_dir)
        assert "worker-1" in resumable


class TestMergeFailedStatus:
    """Tests for the merge_failed status value added to distinguish
    eval failures from merge failures in the recovery path."""

    def test_merge_failed_is_resumable(self, state_dir):
        """Workers in merge_failed state must be included by get_resumable_assignments."""
        _make_assignment(state_dir)
        wa.update_assignment_status(state_dir, "worker-1", "merge_failed")
        resumable = wa.get_resumable_assignments(state_dir)
        assert "worker-1" in resumable
        assert resumable["worker-1"]["status"] == "merge_failed"

    def test_merge_failed_distinct_from_failed(self, state_dir):
        """merge_failed and failed are distinct values that coexist."""
        _make_assignment(state_dir, worker_id="worker-eval-fail")
        _make_assignment(state_dir, worker_id="worker-merge-fail")
        wa.update_assignment_status(state_dir, "worker-eval-fail", "failed")
        wa.update_assignment_status(state_dir, "worker-merge-fail", "merge_failed")

        data = wa.load_assignments(state_dir)
        assert data["assignments"]["worker-eval-fail"]["status"] == "failed"
        assert data["assignments"]["worker-merge-fail"]["status"] == "merge_failed"

        # Both are resumable but carry different semantics
        resumable = wa.get_resumable_assignments(state_dir)
        assert set(resumable.keys()) == {"worker-eval-fail", "worker-merge-fail"}

    def test_merge_failed_persists_across_reloads(self, state_dir):
        """merge_failed survives serialization to disk."""
        _make_assignment(state_dir)
        wa.update_assignment_status(state_dir, "worker-1", "merge_failed")
        wa.update_assignment_live_state(state_dir, "worker-1", "merge-failed")

        # Re-read from disk
        data = wa.load_assignments(state_dir)
        assert data["assignments"]["worker-1"]["status"] == "merge_failed"
        assert data["assignments"]["worker-1"]["live_state"] == "merge-failed"
