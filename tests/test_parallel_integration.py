"""
Integration tests for the parallel fleet wave execution.

Tests run_parallel_wave() end-to-end with mock run_agent_session.
Does NOT call real Claude API — uses canned generator output with HANDOFF blocks.
"""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest


def run_async(coro):
    """Run an async coroutine synchronously for tests."""
    return asyncio.run(coro)

from src.core.orchestrator import run_parallel_wave, load_prompt
from src.core.state import StateManager, atomic_write
from src.core.work_plan import WorkPlan
from src.core.cost_tracker import CostTracker
from src.core.event_tracker import EventTracker
from src.core.control_plane import ControlPlaneClient
from src.core import fleet_session
from src.core.coordination import list_claims, list_instances


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MOCK_GENERATOR_OUTPUT = """\
Implemented the feature successfully.
All tests passing.

---HARNESS_STATUS---
STATUS: COMPLETE
FEATURES_COMPLETED_THIS_SESSION: 1
FEATURES_REMAINING: 0
FILES_MODIFIED: src/api/auth.py, src/api/tokens.py
TESTS_STATUS: 12/12 passing
EXIT_SIGNAL: false
RECOMMENDATION: proceed to next feature
---END_HARNESS_STATUS---

---HANDOFF---
- Built: JWT middleware using jose library
- Decisions: Chose 15min token expiry for security
- Discoveries: Found undocumented config at .config/app.json
- Failures: none
---
"""

MOCK_EVALUATOR_PASS = "All criteria met. VERDICT: PASS"
MOCK_EVALUATOR_FAIL = "Test suite fails on auth. VERDICT: FAIL"


def make_work_plan(tasks):
    """Build a minimal WorkPlan from a list of task dicts."""
    return WorkPlan({
        "phases": [{
            "id": "phase-0",
            "name": "Build",
            "epics": [{
                "id": "epic-001",
                "name": "Core",
                "stories": [{
                    "id": "story-001",
                    "name": "Features",
                    "tasks": tasks,
                }],
            }],
        }],
    })


def setup_harness_dirs(tmp_path):
    """Create .harness/state and .harness/fleet directories."""
    state_dir = tmp_path / ".harness" / "state"
    state_dir.mkdir(parents=True)
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    # Write minimal generator.md and evaluator.md
    (prompts_dir / "generator.md").write_text("Generate code for the feature.\n")
    (prompts_dir / "evaluator.md").write_text("Evaluate the feature.\n")
    return state_dir, prompts_dir


@pytest.fixture
def harness_env(tmp_path):
    """Set up a complete harness environment for integration tests."""
    state_dir, prompts_dir = setup_harness_dirs(tmp_path)

    # Create work plan with 2 non-overlapping scoped features
    tasks = [
        {
            "id": "001",
            "description": "Build API auth",
            "acceptance_criteria": ["JWT works"],
            "steps": [],
            "depends_on": [],
            "scope": ["src/api/"],
            "status": "pending",
            "attempts": 0,
            "blocked_reason": None,
        },
        {
            "id": "002",
            "description": "Build frontend UI",
            "acceptance_criteria": ["Login form works"],
            "steps": [],
            "depends_on": [],
            "scope": ["src/ui/"],
            "status": "pending",
            "attempts": 0,
            "blocked_reason": None,
        },
    ]
    work_plan = make_work_plan(tasks)
    wp_path = state_dir / "work_plan.json"
    work_plan.save(wp_path)
    work_plan.sync_feature_list(state_dir / "feature_list.json")

    state_mgr = StateManager(state_dir)
    cost_tracker = CostTracker()
    cp = ControlPlaneClient()  # disabled by default (no URL)
    tracker = EventTracker(cp)

    config = {
        "parallel": {
            "enabled": True,
            "max_workers": 3,
            "agent_timeout_minutes": 1,
            "discovery_relay": True,
            "stale_instance_hours": 2,
        },
        "evaluator": {
            "test_suite_command": "echo ok",
            "browser_verification": "never",
        },
        "generator": {
            "max_retries_per_feature": 3,
        },
    }

    session = fleet_session.init_session(state_dir, 1)

    return {
        "tmp_path": tmp_path,
        "state_dir": state_dir,
        "prompts_dir": prompts_dir,
        "work_plan": work_plan,
        "wp_path": wp_path,
        "state_mgr": state_mgr,
        "cost_tracker": cost_tracker,
        "tracker": tracker,
        "config": config,
        "session": session,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestParallelWaveBasic:
    """Test basic parallel wave execution with mock agents."""

    def test_two_features_run_in_parallel_both_pass(self, harness_env):
        """Two non-overlapping features should both run and merge."""
        env = harness_env
        layer = env["work_plan"].flatten_for_grouping()

        mock_gen = AsyncMock(return_value={
            "status": "ok",
            "output": MOCK_GENERATOR_OUTPUT,
            "cost": 0.01,
            "usage": {},
            "session_id": "test",
        })
        mock_eval = AsyncMock(return_value={
            "status": "ok",
            "output": MOCK_EVALUATOR_PASS,
            "cost": 0.005,
            "usage": {},
            "session_id": "test",
        })

        # Mock worktree ops (no real git)
        with patch("src.core.orchestrator.create_worktree") as mock_wt, \
             patch("src.core.orchestrator.merge_worktree") as mock_merge, \
             patch("src.core.orchestrator.cleanup_worktree"), \
             patch("src.core.orchestrator.run_agent_session") as mock_session:

            mock_wt.return_value = (env["tmp_path"] / "wt", "branch-test")
            mock_merge.return_value = {"success": True, "conflict": False, "error": ""}

            # First 2 calls = generators, next 2 = evaluators
            mock_session.side_effect = [
                {"output": MOCK_GENERATOR_OUTPUT, "cost": 0.01},
                {"output": MOCK_GENERATOR_OUTPUT, "cost": 0.01},
                {"output": MOCK_EVALUATOR_PASS, "cost": 0.005},
                {"output": MOCK_EVALUATOR_PASS, "cost": 0.005},
            ]

            result = run_async(run_parallel_wave(
                layer=layer,
                wave_num=1,
                project_dir=env["tmp_path"],
                state_dir=env["state_dir"],
                config=env["config"],
                prompts_dir=env["prompts_dir"],
                cost_tracker=env["cost_tracker"],
                tracker=env["tracker"],
                state_mgr=env["state_mgr"],
                work_plan=env["work_plan"],
                work_plan_path=env["wp_path"],
                session=env["session"],
            ))

        assert len(result["completed"]) == 2
        assert len(result["failed"]) == 0
        assert len(result["conflicts"]) == 0

    def test_overlapping_scope_requeues_second_feature(self, harness_env):
        """Two features with overlapping scope: second should be requeued."""
        env = harness_env

        # Override with overlapping scopes
        tasks = [
            {"id": "001", "description": "A", "scope": ["src/api/"], "depends_on": [],
             "status": "pending", "passes": False, "blocked": False},
            {"id": "002", "description": "B", "scope": ["src/api/auth/"], "depends_on": [],
             "status": "pending", "passes": False, "blocked": False},
        ]

        with patch("src.core.orchestrator.create_worktree") as mock_wt, \
             patch("src.core.orchestrator.merge_worktree") as mock_merge, \
             patch("src.core.orchestrator.cleanup_worktree"), \
             patch("src.core.orchestrator.run_agent_session") as mock_session:

            mock_wt.return_value = (env["tmp_path"] / "wt", "branch-test")
            mock_merge.return_value = {"success": True, "conflict": False, "error": ""}
            mock_session.side_effect = [
                {"output": MOCK_GENERATOR_OUTPUT, "cost": 0.01},
                {"output": MOCK_EVALUATOR_PASS, "cost": 0.005},
            ]

            result = run_async(run_parallel_wave(
                layer=tasks,
                wave_num=1,
                project_dir=env["tmp_path"],
                state_dir=env["state_dir"],
                config=env["config"],
                prompts_dir=env["prompts_dir"],
                cost_tracker=env["cost_tracker"],
                tracker=env["tracker"],
                state_mgr=env["state_mgr"],
                work_plan=env["work_plan"],
                work_plan_path=env["wp_path"],
                session=env["session"],
            ))

        # First feature should complete, second requeued due to overlap
        assert "001" in result["completed"]
        assert "002" in result["conflicts"]

    def test_merge_conflict_requeues_feature(self, harness_env):
        """Merge conflict should requeue the conflicting feature."""
        env = harness_env
        layer = env["work_plan"].flatten_for_grouping()

        with patch("src.core.orchestrator.create_worktree") as mock_wt, \
             patch("src.core.orchestrator.merge_worktree") as mock_merge, \
             patch("src.core.orchestrator.cleanup_worktree"), \
             patch("src.core.orchestrator.run_agent_session") as mock_session:

            mock_wt.return_value = (env["tmp_path"] / "wt", "branch-test")
            # First merge succeeds, second conflicts
            mock_merge.side_effect = [
                {"success": True, "conflict": False, "error": ""},
                {"success": False, "conflict": True, "error": "CONFLICT in src/shared.py"},
            ]
            mock_session.side_effect = [
                {"output": MOCK_GENERATOR_OUTPUT, "cost": 0.01},
                {"output": MOCK_GENERATOR_OUTPUT, "cost": 0.01},
                {"output": MOCK_EVALUATOR_PASS, "cost": 0.005},
            ]

            result = run_async(run_parallel_wave(
                layer=layer,
                wave_num=1,
                project_dir=env["tmp_path"],
                state_dir=env["state_dir"],
                config=env["config"],
                prompts_dir=env["prompts_dir"],
                cost_tracker=env["cost_tracker"],
                tracker=env["tracker"],
                state_mgr=env["state_mgr"],
                work_plan=env["work_plan"],
                work_plan_path=env["wp_path"],
                session=env["session"],
            ))

        assert "001" in result["completed"]
        assert "002" in result["conflicts"]


class TestParallelWaveFailure:
    """Test failure and timeout handling."""

    def test_all_agents_fail_triggers_fallback(self, harness_env):
        """All agents failing should trigger fallback to sequential."""
        env = harness_env
        layer = env["work_plan"].flatten_for_grouping()

        with patch("src.core.orchestrator.create_worktree") as mock_wt, \
             patch("src.core.orchestrator.cleanup_worktree"), \
             patch("src.core.orchestrator.run_agent_session") as mock_session:

            mock_wt.return_value = (env["tmp_path"] / "wt", "branch-test")
            mock_session.side_effect = Exception("API error")

            result = run_async(run_parallel_wave(
                layer=layer,
                wave_num=1,
                project_dir=env["tmp_path"],
                state_dir=env["state_dir"],
                config=env["config"],
                prompts_dir=env["prompts_dir"],
                cost_tracker=env["cost_tracker"],
                tracker=env["tracker"],
                state_mgr=env["state_mgr"],
                work_plan=env["work_plan"],
                work_plan_path=env["wp_path"],
                session=env["session"],
            ))

        assert len(result["completed"]) == 0
        assert len(result["failed"]) == 2

    def test_no_scope_features_excluded_from_parallel(self, harness_env):
        """Features without scope should be excluded and returned as failed."""
        env = harness_env

        # Features with no scope
        layer = [
            {"id": "001", "description": "A", "scope": [], "depends_on": [],
             "passes": False, "blocked": False},
            {"id": "002", "description": "B", "scope": [], "depends_on": [],
             "passes": False, "blocked": False},
        ]

        result = run_async(run_parallel_wave(
            layer=layer,
            wave_num=1,
            project_dir=env["tmp_path"],
            state_dir=env["state_dir"],
            config=env["config"],
            prompts_dir=env["prompts_dir"],
            cost_tracker=env["cost_tracker"],
            tracker=env["tracker"],
            state_mgr=env["state_mgr"],
            work_plan=env["work_plan"],
            work_plan_path=env["wp_path"],
            session=env["session"],
        ))

        assert len(result["completed"]) == 0
        assert len(result["failed"]) == 2


class TestDiscoveryRelay:
    """Test that discovery briefs are written and injected."""

    def test_briefs_written_after_wave(self, harness_env):
        """After a wave, brief files should exist in .harness/fleet/briefs/."""
        env = harness_env
        layer = env["work_plan"].flatten_for_grouping()

        with patch("src.core.orchestrator.create_worktree") as mock_wt, \
             patch("src.core.orchestrator.merge_worktree") as mock_merge, \
             patch("src.core.orchestrator.cleanup_worktree"), \
             patch("src.core.orchestrator.run_agent_session") as mock_session:

            mock_wt.return_value = (env["tmp_path"] / "wt", "branch-test")
            mock_merge.return_value = {"success": True, "conflict": False, "error": ""}
            mock_session.side_effect = [
                {"output": MOCK_GENERATOR_OUTPUT, "cost": 0.01},
                {"output": MOCK_GENERATOR_OUTPUT, "cost": 0.01},
                {"output": MOCK_EVALUATOR_PASS, "cost": 0.005},
                {"output": MOCK_EVALUATOR_PASS, "cost": 0.005},
            ]

            run_async(run_parallel_wave(
                layer=layer,
                wave_num=1,
                project_dir=env["tmp_path"],
                state_dir=env["state_dir"],
                config=env["config"],
                prompts_dir=env["prompts_dir"],
                cost_tracker=env["cost_tracker"],
                tracker=env["tracker"],
                state_mgr=env["state_mgr"],
                work_plan=env["work_plan"],
                work_plan_path=env["wp_path"],
                session=env["session"],
            ))

        briefs_dir = env["state_dir"].parent / "fleet" / "briefs"
        assert briefs_dir.exists()
        brief_files = list(briefs_dir.glob("w1-*.md"))
        assert len(brief_files) == 2

    def test_coordination_cleaned_up_after_wave(self, harness_env):
        """All claims and instances should be cleaned up after wave."""
        env = harness_env
        layer = env["work_plan"].flatten_for_grouping()

        with patch("src.core.orchestrator.create_worktree") as mock_wt, \
             patch("src.core.orchestrator.merge_worktree") as mock_merge, \
             patch("src.core.orchestrator.cleanup_worktree"), \
             patch("src.core.orchestrator.run_agent_session") as mock_session:

            mock_wt.return_value = (env["tmp_path"] / "wt", "branch-test")
            mock_merge.return_value = {"success": True, "conflict": False, "error": ""}
            mock_session.side_effect = [
                {"output": MOCK_GENERATOR_OUTPUT, "cost": 0.01},
                {"output": MOCK_GENERATOR_OUTPUT, "cost": 0.01},
                {"output": MOCK_EVALUATOR_PASS, "cost": 0.005},
                {"output": MOCK_EVALUATOR_PASS, "cost": 0.005},
            ]

            run_async(run_parallel_wave(
                layer=layer,
                wave_num=1,
                project_dir=env["tmp_path"],
                state_dir=env["state_dir"],
                config=env["config"],
                prompts_dir=env["prompts_dir"],
                cost_tracker=env["cost_tracker"],
                tracker=env["tracker"],
                state_mgr=env["state_mgr"],
                work_plan=env["work_plan"],
                work_plan_path=env["wp_path"],
                session=env["session"],
            ))

        # All claims and instances should be cleaned up
        assert len(list_claims(env["state_dir"])) == 0
        assert len(list_instances(env["state_dir"])) == 0
