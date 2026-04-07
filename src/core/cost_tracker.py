"""
Cost Tracker — Per-Agent Cost Accumulation
============================================

Tracks costs from SDK ResultMessage per agent type (planner, generator, evaluator).
Optionally appends a JSONL log of every record() call for offline token analysis.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class CostTracker:
    """Tracks cumulative costs and token usage across all harness agents."""

    planner: float = 0.0
    generator: float = 0.0
    evaluator: float = 0.0
    input_tokens: int = 0          # uncached input only
    output_tokens: int = 0
    cache_read_tokens: int = 0     # input tokens read from cache
    cache_creation_tokens: int = 0  # input tokens written to cache
    total_duration_ms: int = 0
    total_api_ms: int = 0
    total_turns: int = 0
    log_path: Optional[Path] = None  # if set, each record() appends a JSONL line here
    _session_costs: list = field(default_factory=list)
    _feature_costs: dict = field(default_factory=dict)

    def _append_log(
        self,
        agent_type: str,
        phase: str,
        cost_usd: float,
        usage: Optional[dict],
        duration_ms: int,
        num_turns: int,
    ) -> None:
        """Append one JSONL entry to log_path. Silent on I/O errors —
        telemetry must never break a run."""
        if self.log_path is None:
            return
        usage = usage or {}
        entry = {
            "ts": time.time(),
            "agent": agent_type,
            "phase": phase,
            "cost_usd": cost_usd,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read": usage.get("cache_read_input_tokens", 0),
            "cache_creation": usage.get("cache_creation_input_tokens", 0),
            "duration_ms": duration_ms,
            "turns": num_turns,
        }
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    @property
    def total(self) -> float:
        return self.planner + self.generator + self.evaluator

    @property
    def total_input_tokens(self) -> int:
        """Total input = uncached + cache_read + cache_creation."""
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens

    def record(self, agent_type: str, cost_usd: float, usage: Optional[dict] = None,
               duration_ms: int = 0, duration_api_ms: int = 0, num_turns: int = 0,
               phase: str = "") -> None:
        """Record cost, tokens, and timing for an agent session.

        phase: optional sub-category label (e.g. "generator-retry-2", "planner-architect",
               "evaluator-resume"). Written to the JSONL log if log_path is set. Default
               empty keeps existing call sites working untouched.
        """
        if agent_type == "planner":
            self.planner += cost_usd
        elif agent_type == "generator":
            self.generator += cost_usd
        elif agent_type == "evaluator":
            self.evaluator += cost_usd

        if usage:
            self.input_tokens += usage.get("input_tokens", 0)
            self.output_tokens += usage.get("output_tokens", 0)
            self.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
            self.cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
        self.total_duration_ms += duration_ms
        self.total_api_ms += duration_api_ms
        self.total_turns += num_turns

        self._session_costs.append({
            "agent": agent_type,
            "cost_usd": cost_usd,
            "usage": usage,
            "duration_ms": duration_ms,
            "num_turns": num_turns,
            "phase": phase,
        })

        self._append_log(agent_type, phase, cost_usd, usage, duration_ms, num_turns)

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
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "total_input_tokens": self.total_input_tokens,
            "total_tokens": self.total_input_tokens + self.output_tokens,
            "total_duration_ms": self.total_duration_ms,
            "total_api_ms": self.total_api_ms,
            "total_turns": self.total_turns,
            "feature_costs": {k: round(v, 4) for k, v in self._feature_costs.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CostTracker":
        """Restore from state dict."""
        tracker = cls()
        tracker.planner = data.get("planner", 0.0)
        tracker.generator = data.get("generator", 0.0)
        tracker.evaluator = data.get("evaluator", 0.0)
        tracker.input_tokens = data.get("input_tokens", 0)
        tracker.output_tokens = data.get("output_tokens", 0)
        tracker.cache_read_tokens = data.get("cache_read_tokens", 0)
        tracker.cache_creation_tokens = data.get("cache_creation_tokens", 0)
        tracker.total_duration_ms = data.get("total_duration_ms", 0)
        tracker.total_api_ms = data.get("total_api_ms", 0)
        tracker.total_turns = data.get("total_turns", 0)
        tracker._feature_costs = data.get("feature_costs", {})
        return tracker

    def format_summary(self) -> str:
        """Format a human-readable cost and token summary."""
        total_all = self.total_input_tokens + self.output_tokens
        lines = [
            f"Cost: ${self.total:.2f}",
            f"  Planner:   ${self.planner:.2f}",
            f"  Generator: ${self.generator:.2f}",
            f"  Evaluator: ${self.evaluator:.2f}",
            f"Tokens: {total_all:,} total",
            f"  Input:  {self.total_input_tokens:,} (uncached: {self.input_tokens:,} / cache read: {self.cache_read_tokens:,} / cache write: {self.cache_creation_tokens:,})",
            f"  Output: {self.output_tokens:,}",
        ]
        if self.cache_read_tokens or self.cache_creation_tokens:
            cache_total = self.cache_read_tokens + self.cache_creation_tokens
            hit_rate = (self.cache_read_tokens / cache_total * 100) if cache_total else 0
            lines.append(f"  Cache hit rate: {hit_rate:.0f}%")
        lines.append(f"API time: {self.total_api_ms / 1000:.1f}s across {self.total_turns} turns")
        if self._feature_costs:
            lines.append("Per-feature costs:")
            for fid, cost in sorted(self._feature_costs.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"  {fid}: ${cost:.4f}")
        return "\n".join(lines)
