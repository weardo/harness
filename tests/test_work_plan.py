"""Tests for src/core/work_plan.py — WorkPlan data model."""

import json
import tempfile
from pathlib import Path

import pytest

from src.core.work_plan import WorkPlan


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def make_task(task_id, status="pending", depends_on=None, attempts=0, with_ac=True):
    return {
        "id": task_id,
        "description": f"Task {task_id}",
        "acceptance_criteria": ["criterion"] if with_ac else [],
        "steps": [],
        "depends_on": depends_on or [],
        "status": status,
        "attempts": attempts,
        "blocked_reason": None,
    }


def make_plan(phases_tasks):
    """phases_tasks: list of lists of task dicts per phase."""
    phases = []
    for i, tasks in enumerate(phases_tasks):
        phases.append({
            "id": f"phase-{i}",
            "name": f"Phase {i}",
            "epics": [
                {
                    "id": f"epic-{i}",
                    "name": f"Epic {i}",
                    "stories": [
                        {
                            "id": f"story-{i}",
                            "name": f"Story {i}",
                            "tasks": tasks,
                        }
                    ],
                }
            ],
        })
    return WorkPlan({"phases": phases})


class TestWorkPlanLoadSave:
    def test_load_nonexistent_raises(self, tmp_dir):
        with pytest.raises(FileNotFoundError):
            WorkPlan.load(tmp_dir / "missing.json")

    def test_save_then_load_round_trip(self, tmp_dir):
        wp = make_plan([[make_task("task-001")]])
        path = tmp_dir / "work_plan.json"
        wp.save(path)
        wp2 = WorkPlan.load(path)
        assert wp2.data == wp.data

    def test_save_uses_atomic_write(self, tmp_dir):
        """atomic_write creates the file; verify it ends up correctly."""
        wp = make_plan([[make_task("task-001")]])
        path = tmp_dir / "subdir" / "work_plan.json"
        wp.save(path)
        assert path.exists()


class TestGetQueryHelpers:
    def setup_method(self):
        self.wp = make_plan([
            [make_task("task-001"), make_task("task-002")],
            [make_task("task-003")],
        ])

    def test_get_task_found(self):
        t = self.wp.get_task("task-001")
        assert t is not None
        assert t["id"] == "task-001"

    def test_get_task_not_found(self):
        assert self.wp.get_task("nonexistent") is None

    def test_get_story_found(self):
        s = self.wp.get_story("story-0")
        assert s is not None
        assert s["id"] == "story-0"

    def test_get_story_not_found(self):
        assert self.wp.get_story("nonexistent") is None

    def test_get_epic_found(self):
        e = self.wp.get_epic("epic-1")
        assert e is not None
        assert e["id"] == "epic-1"

    def test_get_epic_not_found(self):
        assert self.wp.get_epic("nonexistent") is None


class TestGetNextTask:
    def test_returns_first_pending_no_deps(self):
        wp = make_plan([[make_task("t1"), make_task("t2")]])
        assert wp.get_next_task()["id"] == "t1"

    def test_skips_done_tasks(self):
        wp = make_plan([[make_task("t1", status="done"), make_task("t2")]])
        assert wp.get_next_task()["id"] == "t2"

    def test_skips_blocked_tasks(self):
        wp = make_plan([[make_task("t1", status="blocked"), make_task("t2")]])
        assert wp.get_next_task()["id"] == "t2"

    def test_returns_none_when_all_done(self):
        wp = make_plan([[make_task("t1", status="done"), make_task("t2", status="done")]])
        assert wp.get_next_task() is None

    def test_returns_none_when_deps_unsatisfied(self):
        wp = make_plan([[
            make_task("t1", status="pending"),
            make_task("t2", depends_on=["t1"]),
        ]])
        # t1 is pending (eligible), get_next_task returns t1
        assert wp.get_next_task()["id"] == "t1"
        # mark t1 as pending with unsatisfied dep scenario: t2 depends on t1
        # if t1 is also blocked, t2 has unsatisfied dep (blocked != done)
        wp2 = make_plan([[
            make_task("t1", status="blocked"),
            make_task("t2", depends_on=["t1"]),
        ]])
        # t1 blocked, t2 dep not done — no eligible task
        assert wp2.get_next_task() is None

    def test_empty_depends_on_always_eligible(self):
        wp = make_plan([[make_task("t1", depends_on=[])]])
        assert wp.get_next_task()["id"] == "t1"

    def test_phase_gate_blocks_next_phase(self):
        """Phase 1 tasks not returned while phase 0 has pending tasks."""
        wp = make_plan([
            [make_task("t0")],          # phase 0: pending
            [make_task("t1")],          # phase 1
        ])
        assert wp.get_next_task()["id"] == "t0"

    def test_phase_gate_opens_when_phase_complete(self):
        """Phase 1 tasks returned when phase 0 all done."""
        wp = make_plan([
            [make_task("t0", status="done")],
            [make_task("t1")],
        ])
        assert wp.get_next_task()["id"] == "t1"

    def test_phase_gate_opens_when_all_done_or_blocked(self):
        """Phase 1 opens if phase 0 is all done+blocked (GAP-5)."""
        wp = make_plan([
            [make_task("t0a", status="done"), make_task("t0b", status="blocked")],
            [make_task("t1")],
        ])
        assert wp.get_next_task()["id"] == "t1"


class TestMarkTaskDone:
    def test_marks_done_and_saves(self, tmp_dir):
        wp = make_plan([[make_task("task-001")]])
        path = tmp_dir / "wp.json"
        wp.save(path)
        wp.mark_task_done("task-001", path)
        wp2 = WorkPlan.load(path)
        assert wp2.get_task("task-001")["status"] == "done"

    def test_mark_nonexistent_is_noop(self, tmp_dir):
        wp = make_plan([[make_task("task-001")]])
        path = tmp_dir / "wp.json"
        wp.save(path)
        wp.mark_task_done("nonexistent", path)  # no exception


class TestMarkTaskBlocked:
    def test_marks_blocked_with_reason(self, tmp_dir):
        wp = make_plan([[make_task("task-001")]])
        path = tmp_dir / "wp.json"
        wp.save(path)
        wp.mark_task_blocked("task-001", "max retries", path)
        wp2 = WorkPlan.load(path)
        t = wp2.get_task("task-001")
        assert t["status"] == "blocked"
        assert t["blocked_reason"] == "max retries"

    def test_mark_blocked_nonexistent_is_noop(self, tmp_dir):
        wp = make_plan([[make_task("task-001")]])
        path = tmp_dir / "wp.json"
        wp.save(path)
        wp.mark_task_blocked("nonexistent", "reason", path)  # no exception


class TestIncrementTaskAttempts:
    def test_increments_from_zero(self, tmp_dir):
        wp = make_plan([[make_task("task-001")]])
        path = tmp_dir / "wp.json"
        wp.save(path)
        result = wp.increment_task_attempts("task-001", path)
        assert result == 1

    def test_increments_again(self, tmp_dir):
        wp = make_plan([[make_task("task-001")]])
        path = tmp_dir / "wp.json"
        wp.save(path)
        wp.increment_task_attempts("task-001", path)
        result = wp.increment_task_attempts("task-001", path)
        assert result == 2

    def test_persisted_after_increment(self, tmp_dir):
        wp = make_plan([[make_task("task-001")]])
        path = tmp_dir / "wp.json"
        wp.save(path)
        wp.increment_task_attempts("task-001", path)
        wp2 = WorkPlan.load(path)
        assert wp2.get_task("task-001")["attempts"] == 1

    def test_nonexistent_returns_zero(self, tmp_dir):
        wp = make_plan([[make_task("task-001")]])
        path = tmp_dir / "wp.json"
        wp.save(path)
        result = wp.increment_task_attempts("nonexistent", path)
        assert result == 0


class TestCountMethods:
    def test_count_tasks_mixed_statuses(self):
        wp = make_plan([[
            make_task("t1", status="done"),
            make_task("t2", status="blocked"),
            make_task("t3"),
            make_task("t4"),
        ]])
        c = wp.count_tasks()
        assert c["total"] == 4
        assert c["done"] == 1
        assert c["blocked"] == 1
        assert c["pending"] == 2

    def test_count_tasks_empty(self):
        wp = WorkPlan({"phases": []})
        c = wp.count_tasks()
        assert c == {"total": 0, "done": 0, "blocked": 0, "pending": 0}

    def test_count_stories(self):
        wp = make_plan([[make_task("t1")], [make_task("t2")]])
        c = wp.count_stories()
        assert c["total"] == 2  # one story per phase

    def test_count_epics(self):
        wp = make_plan([[make_task("t1")], [make_task("t2")], [make_task("t3")]])
        c = wp.count_epics()
        assert c["total"] == 3

    def test_counts_across_phases(self):
        """2 phases, 3 tasks each = 6 total."""
        wp = make_plan([
            [make_task("t1"), make_task("t2", status="done"), make_task("t3")],
            [make_task("t4"), make_task("t5"), make_task("t6", status="blocked")],
        ])
        c = wp.count_tasks()
        assert c["total"] == 6
        assert c["done"] == 1
        assert c["blocked"] == 1
        assert c["pending"] == 4


class TestCurrentPhase:
    def test_returns_first_phase_with_pending(self):
        wp = make_plan([[make_task("t1")], [make_task("t2")]])
        assert wp.current_phase()["id"] == "phase-0"

    def test_advances_to_next_when_phase_complete(self):
        wp = make_plan([
            [make_task("t1", status="done")],
            [make_task("t2")],
        ])
        assert wp.current_phase()["id"] == "phase-1"

    def test_returns_none_when_all_done(self):
        wp = make_plan([
            [make_task("t1", status="done")],
            [make_task("t2", status="blocked")],
        ])
        assert wp.current_phase() is None


class TestIsPhaseComplete:
    def test_true_when_all_done(self):
        wp = make_plan([[make_task("t1", status="done"), make_task("t2", status="done")]])
        assert wp.is_phase_complete("phase-0") is True

    def test_true_when_all_done_or_blocked(self):
        wp = make_plan([[make_task("t1", status="done"), make_task("t2", status="blocked")]])
        assert wp.is_phase_complete("phase-0") is True

    def test_false_when_pending(self):
        wp = make_plan([[make_task("t1", status="done"), make_task("t2")]])
        assert wp.is_phase_complete("phase-0") is False

    def test_false_for_nonexistent_phase(self):
        wp = make_plan([[make_task("t1")]])
        assert wp.is_phase_complete("nonexistent") is False


class TestValidateDag:
    def test_valid_dag_returns_true(self):
        wp = make_plan([[
            make_task("t1"),
            make_task("t2", depends_on=["t1"]),
        ]])
        r = wp.validate_dag()
        assert r["valid"] is True
        assert r["errors"] == []

    def test_cycle_returns_false(self):
        # t1 → t2 → t1
        wp = WorkPlan({
            "phases": [{
                "id": "phase-0",
                "epics": [{
                    "id": "epic-0",
                    "stories": [{
                        "id": "story-0",
                        "tasks": [
                            {**make_task("t1"), "depends_on": ["t2"]},
                            {**make_task("t2"), "depends_on": ["t1"]},
                        ],
                    }],
                }],
            }],
        })
        r = wp.validate_dag()
        assert r["valid"] is False
        assert any("t1" in str(e) or "t2" in str(e) for e in r["errors"])

    def test_nonexistent_dep_returns_false(self):
        wp = make_plan([[make_task("t1", depends_on=["nonexistent"])]])
        r = wp.validate_dag()
        assert r["valid"] is False
        assert any("nonexistent" in e for e in r["errors"])

    def test_cross_phase_backward_dep_returns_false(self):
        """Phase-0 task depending on Phase-1 task is backward (invalid)."""
        wp = WorkPlan({
            "phases": [
                {
                    "id": "phase-0",
                    "epics": [{
                        "id": "epic-0",
                        "stories": [{
                            "id": "story-0",
                            "tasks": [{**make_task("t0"), "depends_on": ["t1"]}],
                        }],
                    }],
                },
                {
                    "id": "phase-1",
                    "epics": [{
                        "id": "epic-1",
                        "stories": [{
                            "id": "story-1",
                            "tasks": [make_task("t1")],
                        }],
                    }],
                },
            ]
        })
        r = wp.validate_dag()
        assert r["valid"] is False
        assert any("phase" in e.lower() or "backward" in e.lower() for e in r["errors"])

    def test_forward_cross_phase_dep_is_valid(self):
        """Phase-1 task depending on Phase-0 task is forward (valid)."""
        wp = WorkPlan({
            "phases": [
                {
                    "id": "phase-0",
                    "epics": [{
                        "id": "epic-0",
                        "stories": [{"id": "story-0", "tasks": [make_task("t0")]}],
                    }],
                },
                {
                    "id": "phase-1",
                    "epics": [{
                        "id": "epic-1",
                        "stories": [{
                            "id": "story-1",
                            "tasks": [{**make_task("t1"), "depends_on": ["t0"]}],
                        }],
                    }],
                },
            ]
        })
        r = wp.validate_dag()
        assert r["valid"] is True

    def test_no_deps_returns_true(self):
        wp = make_plan([[make_task("t1"), make_task("t2"), make_task("t3")]])
        r = wp.validate_dag()
        assert r["valid"] is True


class TestAreDpsSatisfied:
    def test_empty_deps_satisfied(self):
        wp = make_plan([[make_task("t1")]])
        assert wp.are_deps_satisfied("t1") is True

    def test_dep_done_satisfied(self):
        wp = make_plan([[
            make_task("t1", status="done"),
            make_task("t2", depends_on=["t1"]),
        ]])
        assert wp.are_deps_satisfied("t2") is True

    def test_dep_pending_not_satisfied(self):
        wp = make_plan([[
            make_task("t1"),
            make_task("t2", depends_on=["t1"]),
        ]])
        assert wp.are_deps_satisfied("t2") is False

    def test_dep_blocked_not_satisfied(self):
        wp = make_plan([[
            make_task("t1", status="blocked"),
            make_task("t2", depends_on=["t1"]),
        ]])
        assert wp.are_deps_satisfied("t2") is False

    def test_nonexistent_task_returns_false(self):
        wp = make_plan([[make_task("t1")]])
        assert wp.are_deps_satisfied("nonexistent") is False


class TestFromFlatFeatures:
    def test_produces_single_phase(self):
        features = [{"id": "001", "description": "X", "acceptance_criteria": ["ac"],
                     "steps": [], "depends_on": [], "passes": False, "blocked": False, "retries": 0}]
        wp = WorkPlan.from_flat_features(features)
        assert len(wp.data["phases"]) == 1
        assert wp.data["phases"][0]["id"] == "phase-0"

    def test_single_epic_and_story(self):
        wp = WorkPlan.from_flat_features([
            {"id": "001", "description": "X", "acceptance_criteria": [], "steps": [],
             "depends_on": [], "passes": False, "blocked": False, "retries": 0}
        ])
        phase = wp.data["phases"][0]
        assert phase["epics"][0]["id"] == "epic-001"
        assert phase["epics"][0]["stories"][0]["id"] == "story-001"

    def test_passes_true_maps_to_status_done(self):
        wp = WorkPlan.from_flat_features([
            {"id": "001", "description": "X", "acceptance_criteria": [], "steps": [],
             "depends_on": [], "passes": True, "blocked": False, "retries": 0}
        ])
        assert wp.get_task("001")["status"] == "done"

    def test_blocked_true_maps_to_status_blocked(self):
        wp = WorkPlan.from_flat_features([
            {"id": "001", "description": "X", "acceptance_criteria": [], "steps": [],
             "depends_on": [], "passes": False, "blocked": True, "retries": 0}
        ])
        assert wp.get_task("001")["status"] == "blocked"

    def test_pending_feature_maps_to_pending(self):
        wp = WorkPlan.from_flat_features([
            {"id": "001", "description": "X", "acceptance_criteria": [], "steps": [],
             "depends_on": [], "passes": False, "blocked": False, "retries": 0}
        ])
        assert wp.get_task("001")["status"] == "pending"

    def test_retries_maps_to_attempts(self):
        wp = WorkPlan.from_flat_features([
            {"id": "001", "description": "X", "acceptance_criteria": [], "steps": [],
             "depends_on": [], "passes": False, "blocked": False, "retries": 3}
        ])
        assert wp.get_task("001")["attempts"] == 3

    def test_preserves_depends_on(self):
        features = [
            {"id": "001", "description": "X", "acceptance_criteria": [], "steps": [],
             "depends_on": [], "passes": False, "blocked": False, "retries": 0},
            {"id": "002", "description": "Y", "acceptance_criteria": [], "steps": [],
             "depends_on": ["001"], "passes": False, "blocked": False, "retries": 0},
        ]
        wp = WorkPlan.from_flat_features(features)
        assert wp.get_task("002")["depends_on"] == ["001"]

    def test_empty_feature_list(self):
        wp = WorkPlan.from_flat_features([])
        assert wp.count_tasks()["total"] == 0
