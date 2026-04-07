"""Tests for cost_tracker.py."""

import json

from src.core.cost_tracker import CostTracker


class TestCostTracker:
    def test_initial_zero(self):
        ct = CostTracker()
        assert ct.total == 0.0

    def test_record_planner(self):
        ct = CostTracker()
        ct.record("planner", 0.46)
        assert ct.planner == 0.46
        assert ct.total == 0.46

    def test_record_multiple_agents(self):
        ct = CostTracker()
        ct.record("planner", 0.50)
        ct.record("generator", 71.08)
        ct.record("evaluator", 3.24)
        assert ct.total == 74.82

    def test_accumulates(self):
        ct = CostTracker()
        ct.record("generator", 10.0)
        ct.record("generator", 20.0)
        assert ct.generator == 30.0

    def test_check_budget_ok(self):
        ct = CostTracker()
        ct.record("generator", 30.0)
        assert ct.check_budget(100.0) is True

    def test_check_budget_exceeded(self):
        ct = CostTracker()
        ct.record("generator", 100.0)
        assert ct.check_budget(100.0) is False

    def test_check_budget_zero_means_unlimited(self):
        ct = CostTracker()
        ct.record("generator", 9999.0)
        assert ct.check_budget(0) is True

    def test_to_dict(self):
        ct = CostTracker()
        ct.record("planner", 0.46)
        ct.record("generator", 71.08)
        d = ct.to_dict()
        assert d["planner"] == 0.46
        assert d["generator"] == 71.08
        assert d["total"] == 71.54

    def test_from_dict(self):
        ct = CostTracker.from_dict({"planner": 1.0, "generator": 50.0, "evaluator": 5.0})
        assert ct.total == 56.0

    def test_format_summary(self):
        ct = CostTracker()
        ct.record("planner", 0.46)
        ct.record("generator", 71.08)
        ct.record("evaluator", 3.24)
        summary = ct.format_summary()
        assert "$74.78" in summary
        assert "Planner" in summary
        assert "Generator" in summary


class TestPerFeatureCosts:
    def test_record_feature_accumulates(self):
        ct = CostTracker()
        ct.record_feature("001", "generator", 1.50)
        ct.record_feature("001", "generator", 1.50)
        ct.record_feature("001", "evaluator", 0.50)
        assert ct._feature_costs["001"] == 3.50

    def test_feature_costs_property(self):
        ct = CostTracker()
        ct.record_feature("001", "generator", 1.0)
        ct.record_feature("002", "evaluator", 2.0)
        costs = ct.feature_costs
        assert costs == {"001": 1.0, "002": 2.0}
        # verify it's a copy — mutations don't affect tracker
        costs["003"] = 99.0
        assert "003" not in ct._feature_costs

    def test_most_expensive_features(self):
        ct = CostTracker()
        ct.record_feature("001", "generator", 5.0)
        ct.record_feature("002", "generator", 1.0)
        ct.record_feature("003", "generator", 3.0)
        ct.record_feature("004", "generator", 0.5)
        ct.record_feature("005", "generator", 4.0)
        ct.record_feature("006", "generator", 2.0)
        top3 = ct.most_expensive_features(n=3)
        assert len(top3) == 3
        assert top3[0] == ("001", 5.0)
        assert top3[1] == ("005", 4.0)
        assert top3[2] == ("003", 3.0)

    def test_most_expensive_features_default_n(self):
        ct = CostTracker()
        for i in range(10):
            ct.record_feature(str(i).zfill(3), "generator", float(i))
        result = ct.most_expensive_features()
        assert len(result) == 5

    def test_to_dict_includes_feature_costs(self):
        ct = CostTracker()
        ct.record("generator", 10.0)
        ct.record_feature("001", "generator", 3.0)
        ct.record_feature("002", "evaluator", 7.0)
        d = ct.to_dict()
        assert "feature_costs" in d
        assert d["feature_costs"]["001"] == 3.0
        assert d["feature_costs"]["002"] == 7.0

    def test_from_dict_backward_compat(self):
        # old state.json without feature_costs key must still load
        old_dict = {"planner": 1.0, "generator": 50.0, "evaluator": 5.0}
        ct = CostTracker.from_dict(old_dict)
        assert ct.feature_costs == {}
        assert ct.total == 56.0

    def test_from_dict_restores_feature_costs(self):
        data = {
            "planner": 1.0,
            "generator": 10.0,
            "evaluator": 2.0,
            "feature_costs": {"001": 3.0, "002": 7.0},
        }
        ct = CostTracker.from_dict(data)
        assert ct.feature_costs == {"001": 3.0, "002": 7.0}

    def test_format_summary_with_feature_costs(self):
        ct = CostTracker()
        ct.record("generator", 10.0)
        ct.record_feature("001", "generator", 7.0)
        ct.record_feature("002", "evaluator", 3.0)
        summary = ct.format_summary()
        assert "Per-feature costs:" in summary
        assert "001" in summary
        assert "002" in summary
        # highest cost first
        idx_001 = summary.index("001")
        idx_002 = summary.index("002")
        assert idx_001 < idx_002

    def test_format_summary_no_feature_costs(self):
        ct = CostTracker()
        ct.record("generator", 10.0)
        summary = ct.format_summary()
        assert "Per-feature" not in summary


class TestCostTrackerLog:
    def test_record_without_log_path_does_not_write(self, tmp_path):
        tracker = CostTracker()  # no log_path
        tracker.record("generator", 0.12, usage={"input_tokens": 100, "output_tokens": 50})
        # No file should have been created anywhere in tmp_path
        assert list(tmp_path.iterdir()) == []

    def test_record_with_log_path_appends_jsonl(self, tmp_path):
        log = tmp_path / "token_log.jsonl"
        tracker = CostTracker(log_path=log)
        tracker.record(
            "generator", 0.12,
            usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 200,
                "cache_creation_input_tokens": 10,
            },
            duration_ms=1234,
            num_turns=5,
            phase="generator-first",
        )
        tracker.record(
            "evaluator", 0.05,
            usage={"input_tokens": 80, "output_tokens": 20},
            phase="evaluator",
        )

        lines = log.read_text().strip().split("\n")
        assert len(lines) == 2

        e1 = json.loads(lines[0])
        assert e1["agent"] == "generator"
        assert e1["phase"] == "generator-first"
        assert e1["cost_usd"] == 0.12
        assert e1["input_tokens"] == 100
        assert e1["output_tokens"] == 50
        assert e1["cache_read"] == 200
        assert e1["cache_creation"] == 10
        assert e1["duration_ms"] == 1234
        assert e1["turns"] == 5
        assert "ts" in e1

        e2 = json.loads(lines[1])
        assert e2["agent"] == "evaluator"
        assert e2["phase"] == "evaluator"
        assert e2["cache_read"] == 0
        assert e2["cache_creation"] == 0

    def test_record_phase_defaults_to_empty(self, tmp_path):
        log = tmp_path / "token_log.jsonl"
        tracker = CostTracker(log_path=log)
        tracker.record("planner", 0.30, usage={"input_tokens": 1000, "output_tokens": 200})
        entry = json.loads(log.read_text().strip())
        assert entry["phase"] == ""

    def test_from_dict_requires_log_path_reattach(self, tmp_path):
        log = tmp_path / "token_log.jsonl"
        src = CostTracker(log_path=log)
        src.record("generator", 0.1, usage={"input_tokens": 10, "output_tokens": 5})
        data = src.to_dict()

        # from_dict does NOT know about log_path — caller must re-attach
        restored = CostTracker.from_dict(data)
        assert restored.log_path is None

        # But re-attaching works and continues appending
        restored.log_path = log
        restored.record("evaluator", 0.05, usage={"input_tokens": 8, "output_tokens": 2})
        assert len(log.read_text().strip().split("\n")) == 2
