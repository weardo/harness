"""Tests for product-facing event normalization."""

from src.adapters.events.event_normalizer import ProductEventNormalizer


class TestProductEventNormalizer:
    def setup_method(self):
        self.normalizer = ProductEventNormalizer()

    def test_normalizes_setup_event(self):
        event = {
            "event_type": "run_start",
            "feature_id": "setup-run-start",
            "feature_desc": "Setup: Run started — 5 features planned",
        }
        normalized = self.normalizer.normalize("run-1", event)
        assert normalized["phase"] == "setup"
        assert normalized["kind"] == "started"
        assert normalized["task_id"] is None
        assert "Run started" in normalized["message"]

    def test_normalizes_execution_task_event(self):
        event = {
            "event_type": "feature_pass",
            "feature_id": "task-001",
            "feature_desc": "Implemented auth flow",
            "duration_ms": 1234,
        }
        normalized = self.normalizer.normalize("run-2", event)
        assert normalized["phase"] == "execution"
        assert normalized["kind"] == "passed"
        assert normalized["task_id"] == "task-001"
        assert normalized["metadata"]["duration_ms"] == 1234

    def test_normalizes_parallel_event(self):
        event = {
            "event_type": "wave_complete",
            "wave_num": 2,
            "completed": 3,
            "failed": 1,
            "conflicts": 0,
        }
        normalized = self.normalizer.normalize("run-3", event)
        assert normalized["phase"] == "parallel"
        assert normalized["kind"] == "completed"
        assert normalized["metadata"]["wave_num"] == 2

    def test_normalizes_completion_event(self):
        event = {
            "type": "run_complete",
            "feature_id": "completion-run",
            "feature_desc": "Completion: Run complete — 10 features, $2.50, 100 tokens",
            "cost_usd": 2.5,
        }
        normalized = self.normalizer.normalize("run-4", event)
        assert normalized["phase"] == "completion"
        assert normalized["kind"] == "completed"
        assert normalized["metadata"]["cost_usd"] == 2.5
