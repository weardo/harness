"""Tests for cost_tracker.py."""

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
