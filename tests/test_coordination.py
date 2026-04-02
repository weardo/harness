"""Tests for coordination.py -- file-based scope claims and instance registry."""

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.core.coordination import (
    STALE_INSTANCE_HOURS,
    ScopeOverlapError,
    _coordination_paths,
    _is_read_only,
    _normalize_scope_entry,
    claim_scope,
    generate_instance_id,
    get_swept_feature_ids,
    heartbeat_instance,
    list_claims,
    list_instances,
    register_instance,
    release_claim,
    scopes_overlap,
    sweep_stale_instances,
    unregister_instance,
)


@pytest.fixture
def state_dir(tmp_path):
    """Create a state_dir that mimics .harness/state/."""
    sd = tmp_path / ".harness" / "state"
    sd.mkdir(parents=True)
    return sd


# ---------------------------------------------------------------------------
# generate_instance_id
# ---------------------------------------------------------------------------

class TestGenerateInstanceId:
    def test_format(self):
        iid = generate_instance_id()
        assert iid.startswith("agent-")
        assert len(iid) == len("agent-") + 8  # 4 hex bytes = 8 chars

    def test_unique(self):
        ids = {generate_instance_id() for _ in range(50)}
        assert len(ids) == 50


# ---------------------------------------------------------------------------
# scopes_overlap
# ---------------------------------------------------------------------------

class TestScopesOverlap:
    def test_parent_child_overlap(self):
        assert scopes_overlap(["src/api/"], ["src/api/auth/"]) is True

    def test_child_parent_overlap(self):
        assert scopes_overlap(["src/api/auth/"], ["src/api/"]) is True

    def test_siblings_no_overlap(self):
        assert scopes_overlap(["src/api/"], ["src/ui/"]) is False

    def test_exact_match_overlaps(self):
        assert scopes_overlap(["src/api/"], ["src/api/"]) is True

    def test_read_only_never_conflicts(self):
        assert scopes_overlap(["src/api/ (read-only)"], ["src/api/"]) is False

    def test_both_read_only_no_conflict(self):
        assert scopes_overlap(
            ["src/api/ (read-only)"], ["src/api/ (read-only)"]
        ) is False

    def test_empty_scopes_no_overlap(self):
        assert scopes_overlap([], ["src/api/"]) is False
        assert scopes_overlap(["src/api/"], []) is False
        assert scopes_overlap([], []) is False

    def test_mixed_read_only_and_writable(self):
        # One read-only entry and one writable in each scope
        scope_a = ["src/api/ (read-only)", "src/ui/"]
        scope_b = ["src/api/", "tests/"]
        # src/api/(read-only) vs src/api/ => no conflict (read-only skipped)
        # src/ui/ vs src/api/ => no prefix match
        # src/ui/ vs tests/ => no prefix match
        assert scopes_overlap(scope_a, scope_b) is False


# ---------------------------------------------------------------------------
# _normalize_scope_entry / _is_read_only
# ---------------------------------------------------------------------------

class TestScopeHelpers:
    def test_normalize_strips_read_only(self):
        assert _normalize_scope_entry("src/api/ (read-only)") == "src/api/"

    def test_normalize_plain_entry(self):
        assert _normalize_scope_entry("src/api/") == "src/api/"

    def test_is_read_only_true(self):
        assert _is_read_only("src/api/ (read-only)") is True

    def test_is_read_only_false(self):
        assert _is_read_only("src/api/") is False


# ---------------------------------------------------------------------------
# _coordination_paths
# ---------------------------------------------------------------------------

class TestCoordinationPaths:
    def test_paths_are_siblings_of_state(self, state_dir):
        paths = _coordination_paths(state_dir)
        assert paths["instances_dir"] == state_dir.parent / "coordination" / "instances"
        assert paths["claims_dir"] == state_dir.parent / "coordination" / "claims"


# ---------------------------------------------------------------------------
# register_instance / list_instances
# ---------------------------------------------------------------------------

class TestRegisterInstance:
    def test_register_creates_file(self, state_dir):
        data = register_instance("agent-aaa", "feat-1", os.getpid(), 1, state_dir)
        assert data["instanceId"] == "agent-aaa"
        assert data["featureId"] == "feat-1"
        assert data["pid"] == os.getpid()
        assert data["wave"] == 1
        assert data["status"] == "active"

        # File should exist
        paths = _coordination_paths(state_dir)
        assert (paths["instances_dir"] / "agent-aaa.json").exists()

    def test_list_instances_empty(self, state_dir):
        assert list_instances(state_dir) == []

    def test_list_instances_populated(self, state_dir):
        register_instance("agent-aaa", "feat-1", os.getpid(), 1, state_dir)
        register_instance("agent-bbb", "feat-2", os.getpid(), 1, state_dir)
        instances = list_instances(state_dir)
        assert len(instances) == 2
        ids = {i["instanceId"] for i in instances}
        assert ids == {"agent-aaa", "agent-bbb"}


# ---------------------------------------------------------------------------
# unregister_instance
# ---------------------------------------------------------------------------

class TestUnregisterInstance:
    def test_removes_instance_file(self, state_dir):
        register_instance("agent-aaa", "feat-1", os.getpid(), 1, state_dir)
        unregister_instance("agent-aaa", state_dir)
        assert list_instances(state_dir) == []

    def test_removes_both_instance_and_claim(self, state_dir):
        register_instance("agent-aaa", "feat-1", os.getpid(), 1, state_dir)
        claim_scope("agent-aaa", ["src/api/"], "feat-1", state_dir)

        # Both files exist
        paths = _coordination_paths(state_dir)
        assert (paths["instances_dir"] / "agent-aaa.json").exists()
        assert (paths["claims_dir"] / "agent-aaa.json").exists()

        unregister_instance("agent-aaa", state_dir)

        # Both files gone
        assert not (paths["instances_dir"] / "agent-aaa.json").exists()
        assert not (paths["claims_dir"] / "agent-aaa.json").exists()

    def test_unregister_nonexistent_is_safe(self, state_dir):
        # Should not raise
        unregister_instance("agent-nonexistent", state_dir)


# ---------------------------------------------------------------------------
# heartbeat_instance
# ---------------------------------------------------------------------------

class TestHeartbeatInstance:
    def test_updates_last_seen(self, state_dir):
        data = register_instance("agent-aaa", "feat-1", os.getpid(), 1, state_dir)
        original_seen = data["lastSeen"]

        time.sleep(0.01)  # tiny gap to ensure timestamp differs
        updated = heartbeat_instance("agent-aaa", state_dir)
        assert updated["lastSeen"] >= original_seen

    def test_heartbeat_missing_instance_raises(self, state_dir):
        with pytest.raises(FileNotFoundError, match="Instance not found"):
            heartbeat_instance("agent-ghost", state_dir)


# ---------------------------------------------------------------------------
# claim_scope / list_claims
# ---------------------------------------------------------------------------

class TestClaimScope:
    def test_claim_success(self, state_dir):
        register_instance("agent-aaa", "feat-1", os.getpid(), 1, state_dir)
        data = claim_scope("agent-aaa", ["src/api/"], "feat-1", state_dir)
        assert data["instanceId"] == "agent-aaa"
        assert data["scope"] == ["src/api/"]

    def test_claim_overlap_raises(self, state_dir):
        register_instance("agent-aaa", "feat-1", os.getpid(), 1, state_dir)
        register_instance("agent-bbb", "feat-2", os.getpid(), 2, state_dir)
        claim_scope("agent-aaa", ["src/api/"], "feat-1", state_dir)

        with pytest.raises(ScopeOverlapError) as exc_info:
            claim_scope("agent-bbb", ["src/api/auth/"], "feat-2", state_dir)
        assert exc_info.value.overlap["instanceId"] == "agent-aaa"

    def test_non_overlapping_claims_ok(self, state_dir):
        register_instance("agent-aaa", "feat-1", os.getpid(), 1, state_dir)
        register_instance("agent-bbb", "feat-2", os.getpid(), 2, state_dir)
        claim_scope("agent-aaa", ["src/api/"], "feat-1", state_dir)
        claim_scope("agent-bbb", ["src/ui/"], "feat-2", state_dir)
        assert len(list_claims(state_dir)) == 2

    def test_list_claims_empty(self, state_dir):
        assert list_claims(state_dir) == []

    def test_same_instance_can_reclaim(self, state_dir):
        register_instance("agent-aaa", "feat-1", os.getpid(), 1, state_dir)
        claim_scope("agent-aaa", ["src/api/"], "feat-1", state_dir)
        # Same instance re-claiming (update) should not conflict with itself
        data = claim_scope("agent-aaa", ["src/api/", "src/ui/"], "feat-1", state_dir)
        assert data["scope"] == ["src/api/", "src/ui/"]


# ---------------------------------------------------------------------------
# release_claim
# ---------------------------------------------------------------------------

class TestReleaseClaim:
    def test_release_returns_data(self, state_dir):
        register_instance("agent-aaa", "feat-1", os.getpid(), 1, state_dir)
        claim_scope("agent-aaa", ["src/api/"], "feat-1", state_dir)
        released = release_claim("agent-aaa", state_dir)
        assert released is not None
        assert released["instanceId"] == "agent-aaa"
        assert list_claims(state_dir) == []

    def test_release_nonexistent_returns_none(self, state_dir):
        assert release_claim("agent-ghost", state_dir) is None


# ---------------------------------------------------------------------------
# sweep_stale_instances
# ---------------------------------------------------------------------------

class TestSweepStaleInstances:
    def test_sweep_dead_pid(self, state_dir):
        # Use a PID that almost certainly doesn't exist
        dead_pid = 2**20 + 99999
        register_instance("agent-dead", "feat-1", dead_pid, 1, state_dir)
        claim_scope("agent-dead", ["src/api/"], "feat-1", state_dir)

        swept = sweep_stale_instances(state_dir)
        assert len(swept) == 1
        assert swept[0]["instanceId"] == "agent-dead"
        assert swept[0]["reason"] == "dead process"

        # Both instance and claim removed
        assert list_instances(state_dir) == []
        assert list_claims(state_dir) == []

    def test_sweep_stale_age(self, state_dir):
        register_instance("agent-old", "feat-1", os.getpid(), 1, state_dir)

        # Sweep with a "now" that is 3 hours in the future
        future = datetime.now(timezone.utc) + timedelta(hours=3)
        swept = sweep_stale_instances(state_dir, _now=future)
        assert len(swept) == 1
        assert swept[0]["reason"] == "stale"

    def test_sweep_alive_and_fresh_skipped(self, state_dir):
        register_instance("agent-ok", "feat-1", os.getpid(), 1, state_dir)
        swept = sweep_stale_instances(state_dir)
        assert len(swept) == 0
        assert len(list_instances(state_dir)) == 1

    def test_sweep_mixed(self, state_dir):
        # One alive+fresh, one dead
        dead_pid = 2**20 + 99998
        register_instance("agent-ok", "feat-1", os.getpid(), 1, state_dir)
        register_instance("agent-dead", "feat-2", dead_pid, 1, state_dir)

        swept = sweep_stale_instances(state_dir)
        assert len(swept) == 1
        assert swept[0]["instanceId"] == "agent-dead"
        # The alive instance remains
        assert len(list_instances(state_dir)) == 1
        assert list_instances(state_dir)[0]["instanceId"] == "agent-ok"

    def test_sweep_removes_claim_for_dead_instance(self, state_dir):
        dead_pid = 2**20 + 99997
        register_instance("agent-dead", "feat-1", dead_pid, 1, state_dir)
        claim_scope("agent-dead", ["src/api/"], "feat-1", state_dir)

        assert len(list_claims(state_dir)) == 1
        sweep_stale_instances(state_dir)
        assert len(list_claims(state_dir)) == 0


# ---------------------------------------------------------------------------
# get_swept_feature_ids
# ---------------------------------------------------------------------------

class TestGetSweptFeatureIds:
    def test_extracts_unique_feature_ids(self):
        swept = [
            {"instanceId": "agent-a", "featureId": "feat-1", "reason": "dead process"},
            {"instanceId": "agent-b", "featureId": "feat-2", "reason": "stale"},
            {"instanceId": "agent-c", "featureId": "feat-1", "reason": "dead process"},
        ]
        ids = get_swept_feature_ids(swept)
        assert ids == ["feat-1", "feat-2"]

    def test_empty_swept(self):
        assert get_swept_feature_ids([]) == []

    def test_missing_feature_id_skipped(self):
        swept = [
            {"instanceId": "agent-a", "reason": "stale"},
            {"instanceId": "agent-b", "featureId": "feat-3", "reason": "stale"},
        ]
        assert get_swept_feature_ids(swept) == ["feat-3"]
