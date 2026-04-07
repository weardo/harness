"""
EventTracker — Lifecycle Event Dispatcher
==========================================

Thin wrapper around ControlPlaneClient that provides named methods for
every harness lifecycle event. All methods are fire-and-forget — wrapped
in try/except so they NEVER crash the harness, even when the control plane
is not running.

Event feature_id conventions (for dashboard grouping):
  "setup-{step}"        → pre-flight events (worktree sweep, browser, knowledge)
  "planner-{role}"      → planner role events (architect, adversary, refiner, validator)
  "{NNN}"               → generator/evaluator feature events (001, 002, ...)
  "completion-{step}"   → completion events (suspicion, retro, knowledge ingest)

All events use the existing feature_start / feature_pass / feature_fail types
so they work with the current control plane without schema changes.
"""

from .control_plane import ControlPlaneClient


class EventTracker:
    """Posts lifecycle events to control plane. Fire-and-forget, never crashes."""

    def __init__(self, cp: ControlPlaneClient):
        self.cp = cp

    # -------------------------------------------------------------------------
    # Setup phase
    # -------------------------------------------------------------------------

    def push_work_plan(self, work_plan_data: dict) -> None:
        """Push hierarchical work plan to control plane for dashboard display."""
        try:
            self.cp.push_work_plan(work_plan_data)
        except Exception:
            pass

    def run_started(self, features_planned: int = 0) -> None:
        """Emit run_start event with planned feature count."""
        try:
            self.cp.post_event(
                event_type="run_start",
                feature_id="setup-run-start",
                feature_desc=f"Setup: Run started — {features_planned} features planned",
            )
        except Exception:
            pass

    def worktree_sweep(self, phase: str, removed_count: int) -> None:
        """Emit worktree sweep event (phase: 'start' or 'end')."""
        try:
            self.cp.post_event(
                event_type="feature_start",
                feature_id="setup-worktree-sweep",
                feature_desc=f"Setup: Worktree sweep ({phase}) — removed {removed_count}",
            )
        except Exception:
            pass

    def browser_preflight(self, result: str) -> None:
        """Emit browser pre-flight check event."""
        try:
            self.cp.post_event(
                event_type="feature_start",
                feature_id="setup-browser-preflight",
                feature_desc=f"Setup: Browser pre-flight — {result}",
            )
        except Exception:
            pass

    def knowledge_query(self, chunks_found: int) -> None:
        """Emit knowledge query event."""
        try:
            self.cp.post_event(
                event_type="feature_start",
                feature_id="setup-knowledge-query",
                feature_desc=f"Setup: Knowledge query — {chunks_found} chunks loaded",
            )
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Planner phase
    # -------------------------------------------------------------------------

    def planner_role_start(self, role_name: str, desc: str) -> None:
        """Emit planner role start event."""
        try:
            self.cp.post_event(
                event_type="feature_start",
                feature_id=f"planner-{role_name}",
                feature_desc=f"Planning: {role_name.capitalize()} — {desc}",
            )
        except Exception:
            pass

    def planner_role_pass(
        self, role_name: str, artifact: str, duration_ms: int,
        cost_usd: float = 0.0, attempt: int = 1,
    ) -> None:
        """Emit planner role pass event with artifact, duration, cost, and attempt."""
        try:
            parts = [f"artifact: {artifact}", f"{duration_ms}ms"]
            if cost_usd > 0:
                parts.append(f"${cost_usd:.2f}")
            if attempt > 1:
                parts.append(f"attempt {attempt}")
            self.cp.post_event(
                event_type="feature_pass",
                feature_id=f"planner-{role_name}",
                feature_desc=f"Planning: {role_name.capitalize()} — {', '.join(parts)}",
            )
        except Exception:
            pass

    def planner_role_fail(self, role_name: str, reason: str, cost_usd: float = 0.0, attempt: int = 1) -> None:
        """Emit planner role fail event with reason, cost, and attempt."""
        try:
            parts = [reason]
            if cost_usd > 0:
                parts.append(f"${cost_usd:.2f}")
            if attempt > 1:
                parts.append(f"attempt {attempt}")
            self.cp.post_event(
                event_type="feature_fail",
                feature_id=f"planner-{role_name}",
                feature_desc=f"Planning: {role_name.capitalize()} failed — {', '.join(parts)}",
            )
        except Exception:
            pass

    def planner_role_reject(self, role_name: str, issues_count: int = 0, cost_usd: float = 0.0, attempt: int = 1) -> None:
        """Emit planner role rejection event (validator rejected)."""
        try:
            self.cp.post_event(
                event_type="feature_fail",
                feature_id=f"planner-{role_name}-rejected",
                feature_desc=f"Planning: {role_name.capitalize()} REJECTED — {issues_count} issues, ${cost_usd:.2f}, attempt {attempt}",
            )
        except Exception:
            pass

    def planner_role_retry(self, role_name: str) -> None:
        """Emit planner role retry event."""
        try:
            self.cp.post_event(
                event_type="feature_start",
                feature_id=f"planner-{role_name}-retry",
                feature_desc=f"Planning: {role_name.capitalize()} — retrying with feedback",
            )
        except Exception:
            pass

    def planner_validation_summary(self, roles_completed: list, total_cost: float) -> None:
        """Emit planner pipeline summary event."""
        try:
            self.cp.post_event(
                event_type="feature_pass",
                feature_id="planner-summary",
                feature_desc=f"Planning: Complete — {len(roles_completed)} roles, ${total_cost:.2f}",
            )
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Generator / Evaluator phase
    # -------------------------------------------------------------------------

    def feature_start(self, feature_id: str, desc: str) -> None:
        """Emit feature start event."""
        try:
            self.cp.post_event(
                event_type="feature_start",
                feature_id=feature_id,
                feature_desc=desc,
            )
        except Exception:
            pass

    def feature_pass(self, feature_id: str, desc: str, duration_ms: int) -> None:
        """Emit feature pass event."""
        try:
            self.cp.post_event(
                event_type="feature_pass",
                feature_id=feature_id,
                feature_desc=desc,
            )
        except Exception:
            pass

    def feature_fail(self, feature_id: str, desc: str, error: str) -> None:
        """Emit feature fail event."""
        try:
            self.cp.post_event(
                event_type="feature_fail",
                feature_id=feature_id,
                feature_desc=f"{desc} — {error}",
            )
        except Exception:
            pass

    def feature_retry(self, feature_id: str, desc: str, attempt: int) -> None:
        """Emit feature retry event."""
        try:
            self.cp.post_event(
                event_type="feature_start",
                feature_id=feature_id,
                feature_desc=f"{desc} (attempt {attempt})",
            )
        except Exception:
            pass

    def cb_open(self, reason: str) -> None:
        """Emit circuit breaker open event."""
        try:
            self.cp.post_event(
                event_type="feature_fail",
                feature_id="circuit-breaker",
                feature_desc=f"Circuit breaker open — {reason}",
            )
        except Exception:
            pass

    def cb_close(self) -> None:
        """Emit circuit breaker close event."""
        try:
            self.cp.post_event(
                event_type="feature_pass",
                feature_id="circuit-breaker",
                feature_desc="Circuit breaker closed",
            )
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Parallel / Fleet phase
    # -------------------------------------------------------------------------

    def wave_start(self, wave: int, agent_count: int, feature_ids: list) -> None:
        """Emit wave start event."""
        try:
            self.cp.post_event(
                event_type="wave_start",
                wave_num=wave,
                agent_count=agent_count,
                feature_ids=feature_ids,
            )
        except Exception:
            pass

    def wave_complete(self, wave: int, completed: int, failed: int, conflicts: int) -> None:
        """Emit wave completion event."""
        try:
            self.cp.post_event(
                event_type="wave_complete",
                wave_num=wave,
                completed=completed,
                failed=failed,
                conflicts=conflicts,
            )
        except Exception:
            pass

    def agent_timeout(self, agent_id: str, feature_id: str, elapsed_s: float) -> None:
        """Emit agent timeout event."""
        try:
            self.cp.post_event(
                event_type="agent_timeout",
                wave_num=0,
                agent_id=agent_id,
                feature_id=feature_id,
                error=f"Agent {agent_id} timed out on {feature_id} after {elapsed_s:.0f}s",
            )
        except Exception:
            pass

    def merge_conflict(self, agent_id: str, feature_id: str, error: str) -> None:
        """Emit merge conflict event."""
        try:
            self.cp.post_event(
                event_type="merge_conflict",
                wave_num=0,
                agent_id=agent_id,
                feature_id=feature_id,
                error=error[:200],
            )
        except Exception:
            pass

    def wave_all_failed(self, wave: int, reasons: list) -> None:
        """Emit wave all-failed escalation event."""
        try:
            self.cp.post_event(
                event_type="wave_all_failed",
                wave_num=wave,
                error="; ".join(reasons[:3]),
            )
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Completion phase
    # -------------------------------------------------------------------------

    def suspicion_warning(self, reasons: list) -> None:
        """Emit suspicion warning event."""
        try:
            self.cp.post_event(
                event_type="feature_fail",
                feature_id="completion-suspicion",
                feature_desc=f"Completion: Suspicion check — {len(reasons)} warnings",
            )
        except Exception:
            pass

    def run_complete(self, features_done: int, cost_usd: float,
                     input_tokens: int = 0, output_tokens: int = 0,
                     total_turns: int = 0, api_time_ms: int = 0) -> None:
        """Emit run complete event with cost and token telemetry."""
        try:
            total_tokens = input_tokens + output_tokens
            self.cp.post_event(
                event_type="run_complete",
                feature_id="completion-run",
                feature_desc=f"Completion: Run complete — {features_done} features, ${cost_usd:.2f}, {total_tokens:,} tokens",
                cost_usd=cost_usd,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_turns=total_turns,
                api_time_ms=api_time_ms,
            )
        except Exception:
            pass

    def run_failed(self) -> None:
        """Emit run failed event."""
        try:
            self.cp.post_event(
                event_type="feature_fail",
                feature_id="completion-failed",
                feature_desc="Completion: Run failed",
            )
        except Exception:
            pass

    def retro_generated(self) -> None:
        """Emit retrospective generated event."""
        try:
            self.cp.post_event(
                event_type="feature_pass",
                feature_id="completion-retro",
                feature_desc="Completion: Retrospective generated",
            )
        except Exception:
            pass

    def knowledge_ingested(self, chunks: int) -> None:
        """Emit knowledge ingestion complete event."""
        try:
            self.cp.post_event(
                event_type="feature_pass",
                feature_id="completion-knowledge",
                feature_desc=f"Completion: Knowledge ingested — {chunks} chunks",
            )
        except Exception:
            pass
