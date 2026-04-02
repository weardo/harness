"""
Cost Tracker — Per-Agent Cost Accumulation
============================================

Tracks costs from SDK ResultMessage per agent type (planner, generator, evaluator).
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CostTracker:
    """Tracks cumulative costs across all harness agents."""

    planner: float = 0.0
    generator: float = 0.0
    evaluator: float = 0.0
    _session_costs: list = field(default_factory=list)
    _feature_costs: dict = field(default_factory=dict)

    @property
    def total(self) -> float:
        return self.planner + self.generator + self.evaluator

    def record(self, agent_type: str, cost_usd: float, usage: Optional[dict] = None) -> None:
        """Record cost for an agent session."""
        if agent_type == "planner":
            self.planner += cost_usd
        elif agent_type == "generator":
            self.generator += cost_usd
        elif agent_type == "evaluator":
            self.evaluator += cost_usd

        self._session_costs.append({
            "agent": agent_type,
            "cost_usd": cost_usd,
            "usage": usage,
        })

    def record_feature(self, feature_id: str, agent_type: str, cost_usd: float) -> None:
        """Accumulate cost for a specific feature."""
        self._feature_costs[feature_id] = self._feature_costs.get(feature_id, 0.0) + cost_usd

    @property
    def feature_costs(self) -> dict:
        """Return a copy of per-feature costs."""
        return dict(self._feature_costs)

    def most_expensive_features(self, n: int = 5) -> list:
        """Return the top-n most expensive features as (feature_id, cost) pairs."""
        sorted_items = sorted(self._feature_costs.items(), key=lambda x: x[1], reverse=True)
        return sorted_items[:n]

    def check_budget(self, max_cost_usd: float) -> bool:
        """Returns True if total cost is within budget."""
        if max_cost_usd <= 0:
            return True
        return self.total < max_cost_usd

    def to_dict(self) -> dict:
        """Export as dict for state persistence."""
        return {
            "planner": round(self.planner, 4),
            "generator": round(self.generator, 4),
            "evaluator": round(self.evaluator, 4),
            "total": round(self.total, 4),
            "feature_costs": {k: round(v, 4) for k, v in self._feature_costs.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CostTracker":
        """Restore from state dict."""
        tracker = cls()
        tracker.planner = data.get("planner", 0.0)
        tracker.generator = data.get("generator", 0.0)
        tracker.evaluator = data.get("evaluator", 0.0)
        tracker._feature_costs = data.get("feature_costs", {})
        return tracker

    def format_summary(self) -> str:
        """Format a human-readable cost summary."""
        lines = [
            f"Cost: ${self.total:.2f}",
            f"  Planner:   ${self.planner:.2f}",
            f"  Generator: ${self.generator:.2f}",
            f"  Evaluator: ${self.evaluator:.2f}",
        ]
        if self._feature_costs:
            lines.append("Per-feature costs:")
            for fid, cost in sorted(self._feature_costs.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"  {fid}: ${cost:.4f}")
        return "\n".join(lines)
