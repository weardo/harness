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

    def planner_role_pass(self, role_name: str, artifact: str, duration_ms: int) -> None:
        """Emit planner role pass event with artifact name and duration."""
        try:
            self.cp.post_event(
                event_type="feature_pass",
                feature_id=f"planner-{role_name}",
                feature_desc=f"Planning: {role_name.capitalize()} — artifact: {artifact} ({duration_ms}ms)",
            )
        except Exception:
            pass

    def planner_role_fail(self, role_name: str, reason: str) -> None:
        """Emit planner role fail event with reason."""
        try:
            self.cp.post_event(
                event_type="feature_fail",
                feature_id=f"planner-{role_name}",
                feature_desc=f"Planning: {role_name.capitalize()} failed — {reason}",
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
                event_type="feature_start",
                feature_id=f"wave-{wave}-start",
                feature_desc=f"Wave {wave}: {agent_count} agents — {', '.join(feature_ids[:5])}",
            )
        except Exception:
            pass

    def wave_complete(self, wave: int, completed: int, failed: int, conflicts: int) -> None:
        """Emit wave completion event."""
        try:
            self.cp.post_event(
                event_type="feature_pass",
                feature_id=f"wave-{wave}-complete",
                feature_desc=f"Wave {wave}: {completed} done, {failed} failed, {conflicts} conflicts",
            )
        except Exception:
            pass

    def agent_timeout(self, agent_id: str, feature_id: str, elapsed_s: float) -> None:
        """Emit agent timeout event."""
        try:
            self.cp.post_event(
                event_type="feature_fail",
                feature_id=f"timeout-{feature_id}",
                feature_desc=f"Agent {agent_id} timed out on {feature_id} after {elapsed_s:.0f}s",
            )
        except Exception:
            pass

    def merge_conflict(self, agent_id: str, feature_id: str, error: str) -> None:
        """Emit merge conflict event."""
        try:
            self.cp.post_event(
                event_type="feature_fail",
                feature_id=f"conflict-{feature_id}",
                feature_desc=f"Merge conflict: {feature_id} by {agent_id} — requeued",
                error=error[:200],
            )
        except Exception:
            pass

    def wave_all_failed(self, wave: int, reasons: list) -> None:
        """Emit wave total failure escalation event."""
        try:
            self.cp.post_event(
                event_type="feature_fail",
                feature_id=f"wave-{wave}-all-failed",
                feature_desc=f"Wave {wave}: ALL agents failed — falling back to sequential",
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

    def run_complete(self, features_done: int, cost_usd: float) -> None:
        """Emit run complete event."""
        try:
            self.cp.post_event(
                event_type="run_complete",
                feature_id="completion-run",
                feature_desc=f"Completion: Run complete — {features_done} features, ${cost_usd:.2f}",
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
