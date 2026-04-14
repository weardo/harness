"""
Completion Detection — Shared by Mode A + Mode B
==================================================

Determines whether the harness should continue or allow exit.
Used by both the SDK orchestrator (Mode A) and Stop hook (Mode B).
"""

import subprocess
from pathlib import Path
from typing import Optional

from .state import StateManager
from .run_registry import RunRegistry


def check_completion(harness_dir: Path, test_command: Optional[str] = None, state_dir: Optional[Path] = None) -> dict:
    """Check whether all completion criteria are met.

    Returns {complete: bool, reason: str, progress: bool}.
    Used by both Mode A orchestrator and Mode B Stop hook.

    If state_dir is provided, uses it directly. Otherwise finds the latest
    run via RunRegistry, falling back to legacy .harness/state/.
    """
    harness_dir = Path(harness_dir)
    if state_dir is None:
        state_dir = _find_active_state_dir(harness_dir)
    if state_dir is None:
        return {"complete": False, "reason": "No harness state found", "progress": False}
    state_mgr = StateManager(state_dir)

    # Check 1: Feature list — all features must be passing or blocked
    counts = state_mgr.count_features()
    if counts["total"] == 0:
        return {
            "complete": False,
            "reason": "No features in feature_list.json yet (planner hasn't run)",
            "progress": False,
        }

    if counts["remaining"] > 0:
        return {
            "complete": False,
            "reason": f"{counts['remaining']} features remaining ({counts['passing']}/{counts['total']} passing, {counts['blocked']} blocked)",
            "progress": counts["passing"] > 0,
        }

    # Check 2: Test suite — must pass (if configured)
    if test_command:
        test_result = run_test_suite(test_command, harness_dir.parent)
        if not test_result["passed"]:
            return {
                "complete": False,
                "reason": f"Test suite failing: {test_result['output'][:200]}",
                "progress": True,  # Features are done but tests fail
            }

    # Check 3: Rolling window — need >= 2 completion signals in last 5
    signals = state_mgr.load_exit_signals()
    completion_count = len(signals.get("completion_signals", []))
    if completion_count < 2:
        return {
            "complete": False,
            "reason": f"Need >= 2 completion signals in rolling window (have {completion_count})",
            "progress": True,
        }

    # All checks passed
    return {
        "complete": True,
        "reason": f"All {counts['total']} features complete ({counts['passing']} passing, {counts['blocked']} blocked), tests passing, {completion_count} completion signals",
        "progress": True,
    }


def check_exit_conditions(
    state: dict,
    config: dict,
    elapsed_seconds: float = 0,
) -> Optional[dict]:
    """Check budget/time/iteration limits.

    Returns {should_exit: True, reason: str} if any limit hit, else None.
    """
    # Budget limit
    max_cost = config.get("max_cost_usd", 0)
    if max_cost > 0 and state.get("total_cost_usd", 0) >= max_cost:
        return {
            "should_exit": True,
            "reason": f"Budget exceeded: ${state['total_cost_usd']:.2f} >= ${max_cost:.2f}",
        }

    # Time limit
    max_duration = config.get("max_duration_minutes", 0) * 60
    if max_duration > 0 and elapsed_seconds >= max_duration:
        minutes = int(elapsed_seconds / 60)
        return {
            "should_exit": True,
            "reason": f"Duration exceeded: {minutes}m >= {config['max_duration_minutes']}m",
        }

    # Iteration limit
    max_iter = config.get("max_iterations", 0)
    current_iter = state.get("iteration", 0)
    if max_iter > 0 and current_iter >= max_iter:
        return {
            "should_exit": True,
            "reason": f"Iteration limit: {current_iter} >= {max_iter}",
        }

    return None


def check_suspicion(
    state_mgr,
    config: dict,
    cb_total_opens: int = 0,
    elapsed_seconds: float = 0,
) -> dict:
    """Check for suspiciously clean runs that may indicate evaluation theatre.

    Returns {suspicious: bool, reasons: list[str]}.
    Reads all data from state_mgr to avoid relying on global state.
    """
    suspicion_config = config.get("suspicion", {})

    # If suspicion checking is disabled, return clean immediately
    if not suspicion_config.get("enabled", True):
        return {"suspicious": False, "reasons": []}

    min_avg_feature_seconds = suspicion_config.get("min_avg_feature_seconds", 30)

    # Load feature list and state from state_mgr
    features = state_mgr.load_feature_list()
    state = state_mgr.load_state()

    total_features = len(features)
    total_retries = sum(f.get("retries", 0) for f in features)
    features_passing = sum(1 for f in features if f.get("passes"))
    evaluator_sessions = state.get("evaluator_sessions", 0)

    reasons = []

    # Check 1: zero retries AND zero circuit-breaker opens across all features
    if total_features > 0 and total_retries == 0 and cb_total_opens == 0:
        reasons.append(
            f"Suspiciously high first-attempt pass rate: 0 retries and 0 circuit-breaker opens across {total_features} features"
        )

    # Check 2: fewer evaluator sessions than total features
    if total_features > 0 and evaluator_sessions < total_features:
        reasons.append(
            f"Insufficient evaluator sessions: {evaluator_sessions} evaluator sessions for {total_features} features"
        )

    # Check 3: average time per feature below minimum threshold
    if features_passing > 0:
        avg_feature_seconds = elapsed_seconds / max(features_passing, 1)
        if avg_feature_seconds < min_avg_feature_seconds:
            reasons.append(
                f"Suspiciously fast average feature time: {avg_feature_seconds:.1f}s per feature < {min_avg_feature_seconds}s minimum"
            )

    return {
        "suspicious": len(reasons) > 0,
        "reasons": reasons,
    }


def run_test_suite(command: str, cwd: Path) -> dict:
    """Run the test suite command and return results."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout for test suite
        )
        return {
            "passed": result.returncode == 0,
            "output": result.stdout + result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "passed": False,
            "output": "Test suite timed out after 300 seconds",
            "returncode": -1,
        }
    except FileNotFoundError:
        return {
            "passed": True,  # No test command found = skip
            "output": "Test command not found, skipping",
            "returncode": 0,
        }


def _find_active_state_dir(harness_dir: Path) -> Optional[Path]:
    """Find the state directory for the active run.

    Checks RunRegistry first (new format), falls back to legacy state/ dir.
    """
    try:
        registry = RunRegistry(harness_dir)
        resumable = registry.find_resumable()
        if resumable:
            return registry.run_dir(resumable)
        # No in_progress run — try the most recent one
        runs = registry.list_runs()
        if runs:
            return registry.run_dir(runs[-1]["run_id"])
    except Exception:
        pass

    # Fall back to legacy path
    legacy = harness_dir / "state"
    if legacy.is_dir():
        return legacy
    return None
