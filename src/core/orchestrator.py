"""
Orchestrator — 3-Agent Loop
=============================

Planner → Generator ↔ Evaluator cycle with circuit breaker,
cost tracking, progress detection, and resume support.
"""

import asyncio
import json as json_mod
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .state import StateManager, RunRegistry
from .circuit_breaker import CircuitBreaker, hash_error
from .completion import check_completion, check_exit_conditions, check_suspicion
from .cost_tracker import CostTracker
from .progress import assess_progress, parse_status_block, extract_error_from_output
from .client import load_config, create_client_options
from .control_plane import ControlPlaneClient
from .worktree import sweep_merged_worktrees
from .planner_pipeline import run_planner_pipeline
from .work_plan import WorkPlan
from .event_tracker import EventTracker
from .parallel import (
    group_by_dependency, create_worktree, merge_worktree, cleanup_worktree,
    branch_has_commits, _init_submodules_local, write_task_brief,
)
from .coordination import (
    generate_instance_id, register_instance, unregister_instance,
    claim_scope, sweep_stale_instances, get_swept_feature_ids, ScopeOverlapError,
)
from .discovery import parse_handoff, compress_discovery, write_brief, get_relay_context
from . import fleet_session
from . import worker_assignments


# Delay between sessions
AUTO_CONTINUE_DELAY = 3

# Active worker subprocesses — signal handler uses this for hard stop
_active_worker_procs: set = set()

# Shutdown flag — set by signal handler or drain file
_shutdown_requested = False


def _check_drain(state_dir: Path) -> bool:
    """Check if a graceful drain has been requested via drain file.

    The drain file is written by /harness-stop to request a clean shutdown
    after current generators + evaluators finish. Returns True if draining.
    """
    drain_file = state_dir / "drain"
    return drain_file.exists()


def _clear_drain(state_dir: Path) -> None:
    """Remove drain file after shutdown completes."""
    drain_file = state_dir / "drain"
    drain_file.unlink(missing_ok=True)

# Delegation mode prompt mapping
_DELEGATION_PROMPTS = {
    "full-plan": {"architect": "architect.md", "generator": "generator.md"},
    "guided": {"architect": "architect-guided.md", "generator": "generator-guided.md"},
    "outcome-only": {"architect": "architect-outcome.md", "generator": "generator-outcome.md"},
}


def _metrics_from(result: dict) -> dict:
    """Extract token/timing metrics from an agent session result for cost_tracker.record()."""
    return {
        "usage": result.get("usage") or {},
        "duration_ms": result.get("duration_ms", 0),
        "duration_api_ms": result.get("duration_api_ms", 0),
        "num_turns": result.get("num_turns", 0),
    }


def _write_run_log(state_dir: Path, event: str, **data) -> None:
    """Append a timestamped JSON entry to logs/run.jsonl. Never raises."""
    try:
        log_dir = state_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **data,
        }
        with open(log_dir / "run.jsonl", "a") as f:
            f.write(json_mod.dumps(entry) + "\n")
    except Exception:
        pass


def _log_error(state_dir: Path, context: str, exc: Exception) -> None:
    """Append error event with full traceback to the unified run.jsonl. Never raises."""
    import traceback
    _write_run_log(state_dir, "error",
                   context=context,
                   error=str(exc),
                   error_type=type(exc).__name__,
                   traceback=traceback.format_exc())


# Per-wave relay cache — avoids re-reading + re-concatenating briefs for every
# parallel worker in the same wave. Keyed by wave number. Value is
# (newest_brief_mtime_seen_at_cache_time, rendered_text).
_relay_cache: dict[int, tuple[float, str]] = {}


def _get_cached_relay(state_dir: Path, wave_num: int) -> str:
    """Return the relay context for wave_num, building it once per wave and
    rebuilding only when a newer brief has landed. Safe to call from many
    concurrent workers — races only cause extra rebuilds, never wrong output.

    NOTE: state must be invalidated between runs (different state_dir) — the
    cache is keyed only by wave_num, so a new run with overlapping wave numbers
    must start from a fresh process. Harness already does this: each run is a
    fresh `python3 run.py` invocation.
    """
    briefs_dir = fleet_session.fleet_dir(state_dir) / "briefs"
    if not briefs_dir.exists():
        return ""
    try:
        mtimes = [p.stat().st_mtime for p in briefs_dir.glob("w*-*.md")]
    except OSError:
        mtimes = []
    newest = max(mtimes, default=0.0)

    cached = _relay_cache.get(wave_num)
    if cached is not None and cached[0] >= newest and newest > 0.0:
        return cached[1]

    text = get_relay_context(state_dir, wave_num, include_current_wave=True)
    _relay_cache[wave_num] = (newest, text)
    return text


def _build_eval_replacements(
    feature_id: str,
    state_dir: Path,
    config: dict,
    test_command: str,
    work_plan=None,
    retry_count: int = 1,
) -> tuple[dict, str, str, str]:
    """Build evaluator system prompt replacements and user message.

    Returns (system_replacements, preferred_file, fallback_file, user_message).
    System replacements are feature-independent AND retry-independent so the
    rendered system prompt is byte-stable across features and retries — this
    lets the Claude prompt cache hit on every evaluator call within its TTL.

    retry_count is accepted (for backward compatibility with call sites) but is
    deliberately NOT written into the system prompt. It is appended to the user
    message instead so the evaluator can still reason about retry context
    without busting the cache.
    """
    feature_desc = feature_id
    ac_text = ""
    if work_plan:
        for ph in work_plan.data.get("phases", []):
            for ep in ph.get("epics", []):
                for st in ep.get("stories", []):
                    for t in st.get("tasks", []):
                        if t.get("id") == feature_id:
                            feature_desc = t.get("description", feature_id)
                            ac = t.get("acceptance_criteria", t.get("ac", ""))
                            if isinstance(ac, list):
                                ac_text = "\n".join(f"- {a}" for a in ac)
                            elif ac:
                                ac_text = ac

    # System prompt replacements — feature-independent AND retry-independent.
    # Anything dynamic goes in the user message below so the system prompt caches.
    system_replacements = {
        "STATE_DIR": str(state_dir),
        "TEST_COMMAND": test_command,
        "IF_WEB_PROJECT": config.get("evaluator", {}).get("browser_verification") != "never",
    }

    # User message (dynamic, per-feature, per-retry)
    user_msg_parts = [
        f"Feature ID: {feature_id}",
        f"Description: {feature_desc}",
    ]
    if ac_text:
        user_msg_parts.append(f"\nAcceptance Criteria:\n{ac_text}")
    if retry_count > 1:
        max_retries = config.get("generator", {}).get("max_retries_per_feature", 3)
        user_msg_parts.append(
            f"\nRetry context: this is attempt {retry_count} of {max_retries}."
        )
    user_message = "\n".join(user_msg_parts)

    return system_replacements, "evaluator-v2.md", "evaluator.md", user_message


def _reset_feature_passes(state_dir: Path, feature_id: str) -> None:
    """Safety net: reset passes=True on gen/eval failure.

    Only the orchestrator should mark passes=True (after eval pass + merge).
    This catches edge cases where older generator prompts or race conditions
    leave orphaned passes=True that prevents re-dispatch.
    """
    fl_path = state_dir / "feature_list.json"
    try:
        fl = json_mod.loads(fl_path.read_text())
        for f in fl:
            if f.get("id") == feature_id and f.get("passes"):
                f["passes"] = False
                fl_path.write_text(json_mod.dumps(fl, indent=2))
                break
    except Exception:
        pass  # Non-fatal


def _build_qa_user_msg(feature_id: str, work_plan=None) -> str:
    """Build QA user message with feature details from work_plan."""
    feature_desc = feature_id
    ac_text = ""
    if work_plan:
        for ph in work_plan.data.get("phases", []):
            for ep in ph.get("epics", []):
                for st in ep.get("stories", []):
                    for t in st.get("tasks", []):
                        if t.get("id") == feature_id:
                            feature_desc = t.get("description", feature_id)
                            ac = t.get("acceptance_criteria", t.get("ac", ""))
                            if isinstance(ac, list):
                                ac_text = "\n".join(f"- {a}" for a in ac)
                            elif ac:
                                ac_text = ac
    parts = [f"Feature ID: {feature_id}", f"Description: {feature_desc}"]
    if ac_text:
        parts.append(f"\nAcceptance Criteria:\n{ac_text}")
    return "\n".join(parts)


def resolve_prompt(role: str, config: dict) -> str:
    """Return prompt filename for the given role based on delegation.mode config."""
    mode = config.get("delegation", {}).get("mode", "full-plan")
    mapping = _DELEGATION_PROMPTS.get(mode, _DELEGATION_PROMPTS["full-plan"])
    return mapping.get(role, f"{role}.md")


def load_prompt(prompt_path: Path, replacements: Optional[dict] = None) -> str:
    """Load a prompt template and apply replacements."""
    text = prompt_path.read_text()
    if replacements:
        for key, value in replacements.items():
            text = text.replace(f"{{{{{key}}}}}", str(value))
    # Process conditional blocks
    text = process_conditionals(text, replacements or {})
    return text


def process_conditionals(text: str, context: dict) -> str:
    """Process {{#IF_X}}...{{/IF_X}} conditional blocks."""
    import re

    def replace_block(match):
        condition = match.group(1)
        content = match.group(2)
        if context.get(condition):
            return content
        return ""

    text = re.sub(
        r"\{\{#(IF_\w+)\}\}(.*?)\{\{/\1\}\}",
        replace_block,
        text,
        flags=re.DOTALL,
    )
    return text


RATE_LIMIT_WAIT_SECONDS = 300  # 5 minutes between rate limit retries
RATE_LIMIT_MAX_RETRIES = 3


async def run_agent_session(
    prompt: str,
    options: dict,
    project_dir: Path,
    progress_label: str = "",
    system_prompt: Optional[str] = None,
    resume_session_id: Optional[str] = None,
) -> dict:
    """Run a single agent session using Claude Agent SDK.

    Falls back to `claude -p` CLI if no ANTHROPIC_API_KEY is set,
    allowing the harness to run on a Claude subscription without a key.

    Handles rate limiting automatically: on rate limit, waits and resumes
    the same session (preserving full conversation context). Retries up to
    RATE_LIMIT_MAX_RETRIES times before returning the rate_limited result.

    Args:
        prompt: User message (dynamic, per-feature content).
        progress_label: If set, print streaming progress markers (e.g., "Planner").
        system_prompt: Static template passed via --system-prompt for caching.
        resume_session_id: If set, resume a previous session instead of starting fresh.
            The agent keeps full conversation history from the prior run.

    Returns {status, output, cost, usage, session_id}.
    """
    # Copy options to prevent mutation (SDK path pops "hooks" key)
    _opts = dict(options)
    result = await _run_agent_session_inner(prompt, _opts, project_dir,
                                            progress_label, system_prompt, resume_session_id)

    # Rate limit retry loop: wait and resume the same session
    for _rl in range(RATE_LIMIT_MAX_RETRIES):
        if result.get("status") != "rate_limited":
            return result
        _sid = result.get("session_id") or resume_session_id
        _wait = RATE_LIMIT_WAIT_SECONDS
        print(f"  Rate limited ({_rl + 1}/{RATE_LIMIT_MAX_RETRIES}). Waiting {_wait // 60}m...")
        print(f"    {result.get('output', '')[:120]}")
        await asyncio.sleep(_wait)
        _opts = dict(options)  # fresh copy each retry (SDK path mutates)
        if _sid:
            # Resume the same session — agent keeps full context
            result = await _run_agent_session_inner(
                "You were rate limited. Continue where you left off.",
                _opts, project_dir, progress_label,
                system_prompt=None, resume_session_id=_sid)
        else:
            # No session to resume — retry fresh
            result = await _run_agent_session_inner(
                prompt, _opts, project_dir, progress_label,
                system_prompt, resume_session_id=None)

    return result


async def _run_agent_session_inner(
    prompt: str,
    options: dict,
    project_dir: Path,
    progress_label: str = "",
    system_prompt: Optional[str] = None,
    resume_session_id: Optional[str] = None,
) -> dict:
    """Inner session runner — no rate limit retry (handled by caller)."""
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return await run_agent_session_cli(prompt, project_dir, options, system_prompt=system_prompt, resume_session_id=resume_session_id)

    try:
        from claude_code_sdk import ClaudeCodeOptions, ClaudeSDKClient
        from claude_code_sdk.types import HookMatcher
    except ImportError:
        return await run_agent_session_cli(prompt, project_dir, options, system_prompt=system_prompt, resume_session_id=resume_session_id)

    # TODO: SDK path does not yet support system_prompt or resume_session_id.
    # Currently subscription-only (CLI path). If switching to API key, wire
    # system_prompt into ClaudeCodeOptions or the SDK's system prompt mechanism.
    # resume_session_id is silently ignored in SDK mode — falls back to fresh session.

    # Convert hooks dict to HookMatcher objects
    hooks_config = options.pop("hooks", {})
    sdk_hooks = {}
    for event, matchers in hooks_config.items():
        sdk_hooks[event] = [
            HookMatcher(matcher=m["matcher"], hooks=m["hooks"])
            for m in matchers
        ]

    client = ClaudeSDKClient(
        options=ClaudeCodeOptions(
            **options,
            hooks=sdk_hooks,
        )
    )

    output_text = ""
    cost_usd = 0.0
    usage = {}
    session_id = None
    duration_ms = 0
    duration_api_ms = 0
    num_turns = 0
    _tool_count = 0
    _text_chars = 0
    _last_progress = 0

    try:
        async with client:
            await client.query(prompt)
            async for msg in client.receive_response():
                msg_type = type(msg).__name__

                if msg_type == "AssistantMessage" and hasattr(msg, "content"):
                    for block in msg.content:
                        if type(block).__name__ == "TextBlock" and hasattr(block, "text"):
                            output_text += block.text
                            _text_chars += len(block.text)
                            # Print progress markers every ~2000 chars
                            if progress_label and _text_chars - _last_progress > 2000:
                                _last_progress = _text_chars
                                # Detect what's being generated from content
                                marker = ""
                                if "## Architecture" in block.text:
                                    marker = "architecture"
                                elif "## Features" in block.text or "## Success Criteria" in block.text:
                                    marker = "features"
                                elif "## Data Model" in block.text or "## Security" in block.text:
                                    marker = "data model"
                                elif '"phases"' in block.text:
                                    marker = "work plan"
                                elif '"acceptance_criteria"' in block.text:
                                    marker = "tasks"
                                elif "## Constraints" in block.text:
                                    marker = "constraints"
                                if marker:
                                    print(f"  [{progress_label}] generating {marker}...", flush=True)
                                else:
                                    print(f"  [{progress_label}] {_text_chars // 1000}k chars...", flush=True)
                        elif type(block).__name__ == "ToolUseBlock" and hasattr(block, "name"):
                            _tool_count += 1
                            if progress_label:
                                print(f"  [{progress_label}] tool: {block.name} (#{_tool_count})", flush=True)
                            else:
                                print(f"  [Tool: {block.name}]", flush=True)

                elif msg_type == "ResultMessage":
                    cost_usd = getattr(msg, "total_cost_usd", 0) or 0
                    usage = getattr(msg, "usage", {}) or {}
                    session_id = getattr(msg, "session_id", None)
                    duration_ms = getattr(msg, "duration_ms", 0) or 0
                    duration_api_ms = getattr(msg, "duration_api_ms", 0) or 0
                    num_turns = getattr(msg, "num_turns", 0) or 0
                    # Check for error subtypes
                    subtype = getattr(msg, "subtype", "success")
                    if subtype and subtype != "success":
                        return {
                            "status": "error",
                            "output": output_text,
                            "cost": cost_usd,
                            "usage": usage,
                            "session_id": session_id,
                            "duration_ms": duration_ms,
                            "duration_api_ms": duration_api_ms,
                            "num_turns": num_turns,
                            "error": f"Session ended with: {subtype}",
                        }

                # Silently skip: SystemMessage, UserMessage, StreamEvent, rate_limit_event, etc.

    except Exception as e:
        error_msg = str(e)
        # Rate limiting — return rate_limited status (caller handles retry)
        if "rate_limit" in error_msg.lower() or "429" in error_msg:
            return {
                "status": "rate_limited",
                "output": output_text or error_msg,
                "cost": cost_usd,
                "usage": {},
                "session_id": session_id,
                "duration_ms": 0,
                "duration_api_ms": 0,
                "num_turns": 0,
                "error": f"Rate limited: {error_msg}",
            }
        raise

    return {
        "status": "continue",
        "output": output_text,
        "cost": cost_usd,
        "usage": usage,
        "session_id": session_id,
        "duration_ms": duration_ms,
        "duration_api_ms": duration_api_ms,
        "num_turns": num_turns,
    }


async def run_agent_session_cli(
    prompt: str,
    project_dir: Path,
    options: Optional[dict] = None,
    system_prompt: Optional[str] = None,
    resume_session_id: Optional[str] = None,
) -> dict:
    """Run via `claude -p` CLI (subscription mode, no API key needed).

    Uses asyncio subprocess so multiple CLI sessions can run concurrently
    in parallel fleet mode. The cmd array is built from trusted constants
    and config values — no shell interpolation.

    Uses --output-format json to get JSONL output with cost/token metrics.
    The last line is a result message with total_cost_usd, usage, etc.

    Args:
        prompt: User message (dynamic, per-feature).
        system_prompt: Static template for --system-prompt flag (enables caching).
        resume_session_id: If set, resume a previous CLI session. The agent
            keeps full conversation context from the prior run and receives
            *prompt* as a new user message in the existing conversation.
    """
    options = options or {}
    model = options.get("model", "claude-sonnet-4-6")

    if resume_session_id:
        # Resume existing session — agent retains full conversation history.
        # system_prompt is already baked into the session, don't re-send.
        cmd = [
            "claude", "--resume", resume_session_id, "-p", prompt,
            "--output-format", "json",
            "--model", model,
            "--dangerously-skip-permissions",
        ]
    else:
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "json",
            "--model", model,
            "--dangerously-skip-permissions",
        ]
        if system_prompt:
            cmd += ["--system-prompt", system_prompt]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(project_dir),
    )
    _active_worker_procs.add(proc)

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=3600,  # 1 hour max per session
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        _active_worker_procs.discard(proc)
        return {
            "status": "error",
            "output": "[CLI session timed out after 3600s]",
            "cost": 0.0,
            "usage": {},
            "session_id": None,
            "duration_ms": 0,
            "duration_api_ms": 0,
            "num_turns": 0,
        }
    finally:
        _active_worker_procs.discard(proc)

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    # Parse JSONL: extract text from assistant messages, metrics from result
    output_parts = []
    cost_usd = 0.0
    usage = {}
    session_id = None
    duration_ms = 0
    duration_api_ms = 0
    num_turns = 0

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json_mod.loads(line)
        except json_mod.JSONDecodeError:
            output_parts.append(line)
            continue

        msg_type = msg.get("type", "")

        if msg_type == "assistant":
            for block in msg.get("content", []):
                if block.get("type") == "text":
                    output_parts.append(block.get("text", ""))

        elif msg_type == "result":
            # CLI uses "cost_usd", SDK uses "total_cost_usd" — check both
            cost_usd = msg.get("cost_usd", 0) or msg.get("total_cost_usd", 0) or 0
            usage = msg.get("usage", {}) or {}
            session_id = msg.get("session_id")
            duration_ms = msg.get("duration_ms", 0) or 0
            duration_api_ms = msg.get("duration_api_ms", 0) or 0
            num_turns = msg.get("num_turns", 0) or 0
            if msg.get("result"):
                output_parts.append(msg["result"])

    output = "\n".join(output_parts)
    if stderr:
        output += f"\n[stderr: {stderr[:500]}]"

    if proc.returncode != 0:
        # Detect rate limit from Claude CLI output patterns:
        # - "You've hit your limit" — subscription rate limit
        # - "rate_limit" — API error code
        # - "hit your limit · resets" — full message with reset time
        _lower = output.lower()
        _is_rate_limit = ("hit your limit" in _lower
                          or "rate_limit" in _lower
                          or ("resets " in _lower and "limit" in _lower))
        return {
            "status": "rate_limited" if _is_rate_limit else "error",
            "output": output,
            "cost": cost_usd,
            "usage": usage,
            "session_id": session_id,
            "duration_ms": duration_ms,
            "duration_api_ms": duration_api_ms,
            "num_turns": num_turns,
            "error_reason": "rate_limited" if _is_rate_limit else f"exit code {proc.returncode}",
        }

    return {
        "status": "continue",
        "output": output,
        "cost": cost_usd,
        "usage": usage,
        "session_id": session_id,
        "duration_ms": duration_ms,
        "duration_api_ms": duration_api_ms,
        "num_turns": num_turns,
    }


async def run_parallel_wave(
    layer: list[dict],
    wave_num: int,
    project_dir: Path,
    state_dir: Path,
    config: dict,
    prompts_dir: Path,
    cost_tracker: "CostTracker",
    tracker: "EventTracker",
    state_mgr: "StateManager",
    work_plan: "WorkPlan",
    work_plan_path: Path,
    session: dict,
) -> dict:
    """Run parallel worker pool: gen→QA pipelines with N concurrent slots.

    Pool size is runtime-configurable via <state_dir>/pool_size file.
    Write a number to resize live: echo 5 > .harness/runs/<run>/pool_size

    Returns {completed: list[str], failed: list[str], conflicts: list[str]}.
    """
    parallel_config = config.get("parallel", {})
    max_workers = parallel_config.get("max_workers", 3)
    timeout_minutes = parallel_config.get("agent_timeout_minutes", 30)
    relay_enabled = parallel_config.get("discovery_relay", True)

    # Filter out features with empty scope (force sequential)
    parallel_features = [f for f in layer if f.get("scope")]
    sequential_features = [f for f in layer if not f.get("scope")]

    completed, failed, conflicts = [], [], []

    for feat in sequential_features:
        failed.append(feat["id"])

    if not parallel_features:
        return {"completed": completed, "failed": failed, "conflicts": conflicts}

    # Clean up failed/interrupted assignments with no live worktree — prevents dispatch blockage
    _wa_data = worker_assignments.load_assignments(state_dir)
    _stale_wids = []
    for _wid, _asgn in _wa_data.get("assignments", {}).items():
        if _asgn.get("status") in ("failed", "interrupted", "running"):
            _wt = Path(_asgn.get("worktree_dir", ""))
            _br = _asgn.get("branch", "")
            if not _wt.exists() and not branch_has_commits(project_dir, _br):
                _stale_wids.append(_wid)
    for _wid in _stale_wids:
        worker_assignments.remove_assignment(state_dir, _wid)
    if _stale_wids:
        print(f"  Cleared {len(_stale_wids)} stale assignment(s): {_stale_wids}")

    # Sweep stale instances
    swept = sweep_stale_instances(state_dir, parallel_config.get("stale_instance_hours", 2.0))
    if swept:
        swept_ids = get_swept_feature_ids(swept)
        for fid in swept_ids:
            fleet_session.requeue_feature(session, fid, reason="stale_instance")
        for inst in swept:
            wt = inst.get("worktreeDir")
            br = inst.get("branch")
            if wt and br:
                try:
                    cleanup_worktree(project_dir, Path(wt), br)
                except Exception:
                    pass
        print(f"  Swept {len(swept)} stale instance(s), requeued {len(swept_ids)} feature(s)")

    # --- Runtime-configurable pool size ---
    def _get_pool_size() -> int:
        pool_file = state_dir / "pool_size"
        if pool_file.exists():
            try:
                val = int(pool_file.read_text().strip())
                if val > 0:
                    return val
            except (ValueError, OSError):
                pass
        return max_workers

    # Relay context is now built per-agent (not once per wave) so agents
    # starting later see briefs from agents that already finished.
    # relay_enabled flag still controls whether relay is used at all.

    test_command = config.get("evaluator", {}).get("test_suite_command", "echo 'No test command'")

    wave_replacements = {
        "STATE_DIR": str(state_dir),
        "TEST_COMMAND": test_command,
        "IF_WEB_PROJECT": config.get("evaluator", {}).get("browser_verification") != "never",
    }
    prompt_file = "generator-v2.md" if (prompts_dir / "generator-v2.md").exists() else resolve_prompt("generator", config)
    gen_system_prompt = load_prompt(prompts_dir / prompt_file, wave_replacements)

    _qa_repl = {
        "STATE_DIR": str(state_dir),
        "TEST_COMMAND": test_command,
        "IF_WEB_PROJECT": config.get("evaluator", {}).get("browser_verification") != "never",
    }
    _qa_file = "evaluator-v2.md" if (prompts_dir / "evaluator-v2.md").exists() else "evaluator.md"
    qa_system_prompt = load_prompt(prompts_dir / _qa_file, _qa_repl)

    gen_timeout = timeout_minutes * 60
    qa_timeout = 15 * 60

    # Worker pool with async queue for results
    # Dynamic pool: re-reads pool_size file on every acquire so live resizing works
    _pool_active = 0
    _pool_lock = asyncio.Lock()
    _pool_slot_free = asyncio.Event()
    _pool_slot_free.set()

    async def _acquire_slot():
        nonlocal _pool_active
        while True:
            async with _pool_lock:
                if _pool_active < _get_pool_size():
                    _pool_active += 1
                    return
            _pool_slot_free.clear()
            await _pool_slot_free.wait()

    async def _release_slot():
        nonlocal _pool_active
        async with _pool_lock:
            _pool_active -= 1
        _pool_slot_free.set()

    pool_size = _get_pool_size()
    result_queue = asyncio.Queue()
    max_retries = config.get("generator", {}).get("max_retries_per_feature", 3)
    # Retry counts: always read from work_plan (persisted on disk), never in-memory
    print(f"  Pool: {len(parallel_features)} features, {pool_size} concurrent slots")

    def _get_attempt_count(feature_id: str) -> int:
        """Read attempt count from persisted work_plan — single source of truth."""
        if work_plan is not None:
            for _, _, _, _t in work_plan._all_tasks():
                if _t["id"] == feature_id:
                    return _t.get("attempts", 0)
        return 0

    def _find_reusable_worktree(feature_id: str):
        """Look up reusable worktree from worker_assignments on disk — no in-memory cache.

        Returns (worktree_path, branch, worker_id, gen_session_id) or None.
        gen_session_id may be None if the prior run didn't record one.
        """
        _wa_path = state_dir / "fleet" / "worker_assignments.json"
        if not _wa_path.exists():
            return None
        try:
            _wa = json_mod.loads(_wa_path.read_text())
            for _wid, _asgn in _wa.get("assignments", {}).items():
                if _asgn.get("feature_id") == feature_id and _asgn.get("status") == "failed":
                    _wt_path = Path(_asgn.get("worktree_dir", ""))
                    _br = _asgn.get("branch", "")
                    _sid = _asgn.get("gen_session_id") or None  # treat "" as None
                    if _wt_path.exists() and _br and branch_has_commits(project_dir, _br):
                        return (_wt_path, _br, _wid, _sid)
        except Exception:
            pass
        return None

    tracker.wave_start(wave_num, len(parallel_features), [f["id"] for f in parallel_features])
    fleet_session.start_wave(session, wave_num, [])
    fleet_session.save_session(session, state_dir)

    async def _pool_worker(feature):
        """Acquire pool slot -> create worktree -> gen -> QA -> push result."""
        feature_id = feature["id"]

        await _acquire_slot()
        try:
            if _shutdown_requested or _check_drain(state_dir):
                await result_queue.put(("skipped", feature_id, None))
                return

            agent_id = generate_instance_id()
            scope = feature.get("scope", [])

            # Reuse existing worktree from prior eval-fail retry (read from disk)
            _existing = _find_reusable_worktree(feature_id)
            _resume_sid = None  # generator session ID for --resume
            if _existing:
                wt_dir, branch, wid, _resume_sid = _existing
                if _resume_sid:
                    print(f"  {feature_id}: reusing worktree {wid} for retry (resuming session)")
                else:
                    print(f"  {feature_id}: reusing worktree {wid} for retry (fresh session)")
                register_instance(agent_id, feature_id, 0, wave_num, state_dir,
                                  worktree_dir=str(wt_dir), branch=branch)
            else:
                # Create new worktree
                try:
                    claim_scope(agent_id, scope, feature_id, state_dir)
                except ScopeOverlapError as e:
                    print(f"  Scope overlap for {feature_id}: {e} -- requeued")
                    await result_queue.put(("conflict", feature_id, None))
                    return

                try:
                    wid_num = hash(agent_id) % 10000
                    wt_dir, branch = create_worktree(project_dir, wid_num)
                    register_instance(agent_id, feature_id, 0, wave_num, state_dir,
                                      worktree_dir=str(wt_dir), branch=branch)
                    worker_assignments.add_assignment(
                        state_dir, f"worker-{wid_num}", feature_id,
                        branch, str(wt_dir), agent_id, wave_num, scope,
                    )
                    wid = f"worker-{wid_num}"
                except Exception as e:
                    print(f"  Worktree creation failed for {feature_id}: {e}")
                    unregister_instance(agent_id, state_dir)
                    await result_queue.put(("error", feature_id, None))
                    return

            # --- Generator (own timeout) ---
            feature_desc = feature.get("description", "")
            # Build relay fresh per-agent so later agents see earlier agents' briefs
            _relay = _get_cached_relay(state_dir, wave_num) if relay_enabled else ""
            write_task_brief(wt_dir, feature, state_dir,
                             work_plan=work_plan, relay_context=_relay)
            # feedback.md is already in worktree from evaluator (retry case)
            # or needs to be copied from persistent location (first attempt after resume)
            _wt_fb = Path(wt_dir) / "feedback.md"
            if not _wt_fb.exists():
                _fb_file = state_dir / f"feedback-{feature_id}.md"
                if _fb_file.exists():
                    import shutil
                    shutil.copy2(_fb_file, _wt_fb)

            gen_options = create_client_options(wt_dir, config)

            # If we have a prior generator session ID, resume it with eval feedback
            # instead of starting fresh — agent keeps full context from prior run.
            if _resume_sid:
                user_msg = (
                    f"The evaluator reviewed your work on {feature_id} and found issues. "
                    f"Read feedback.md in this directory for the full eval report. "
                    f"Fix ALL issues listed there. You already have full context from your previous work. "
                    f"Do NOT re-explore the codebase — go straight to fixing the problems."
                )
                try:
                    gen_result = await asyncio.wait_for(
                        run_agent_session(user_msg, gen_options, wt_dir,
                                          resume_session_id=_resume_sid),
                        timeout=gen_timeout,
                    )
                except asyncio.TimeoutError:
                    print(f"  Generator {agent_id} on {feature_id}: timed out after {timeout_minutes}m")
                    # Preserve session_id for next retry — prefer any new ID from partial output
                    _timeout_sid = _resume_sid
                    if _timeout_sid:
                        worker_assignments.update_assignment_session_id(state_dir, wid, _timeout_sid)
                    await result_queue.put(("result", feature_id, {
                        "agent_id": agent_id, "feature": feature, "wt_dir": wt_dir,
                        "branch": branch, "wid": wid,
                        "gen_result": {"output": "", "cost": 0, "session_id": _timeout_sid},
                        "gen_status": "timeout", "qa_result": None,
                    }))
                    return
            else:
                user_msg = f"Your assigned feature: {feature_id} -- {feature_desc}\nRead TASK_BRIEF.md in this directory for full details."
                try:
                    gen_result = await asyncio.wait_for(
                        run_agent_session(user_msg, gen_options, wt_dir, system_prompt=gen_system_prompt),
                        timeout=gen_timeout,
                    )
                except asyncio.TimeoutError:
                    print(f"  Generator {agent_id} on {feature_id}: timed out after {timeout_minutes}m")
                    await result_queue.put(("result", feature_id, {
                        "agent_id": agent_id, "feature": feature, "wt_dir": wt_dir,
                        "branch": branch, "wid": wid,
                        "gen_result": {"output": "", "cost": 0}, "gen_status": "timeout", "qa_result": None,
                    }))
                    return

            # Persist generator session ID for resume on retry
            _gen_sid = gen_result.get("session_id")
            if _gen_sid:
                worker_assignments.update_assignment_session_id(state_dir, wid, _gen_sid)
            elif _resume_sid:
                # Resume was attempted but no session_id came back (session expired/missing).
                # Clear the stale ID so next retry falls back to a fresh session.
                worker_assignments.update_assignment_session_id(state_dir, wid, "")

            output = gen_result.get("output", "")
            gen_status = "ok"
            if gen_result.get("status") == "error":
                if _resume_sid:
                    print(f"  Agent {agent_id} on {feature_id}: resume failed ({gen_result.get('error_reason', 'session error')}) -- next retry will use fresh session")
                else:
                    print(f"  Agent {agent_id} on {feature_id}: {gen_result.get('error_reason', 'session error')}")
                gen_status = "error"
            elif "---HARNESS_STATUS---" not in output:
                print(f"  Agent {agent_id} on {feature_id}: no HARNESS_STATUS block -- treating as failed")
                gen_status = "error"

            should_qa = gen_status == "ok" or branch_has_commits(project_dir, branch)
            if not should_qa:
                await result_queue.put(("result", feature_id, {
                    "agent_id": agent_id, "feature": feature, "wt_dir": wt_dir,
                    "branch": branch, "wid": wid,
                    "gen_result": gen_result, "gen_status": gen_status, "qa_result": None,
                }))
                return

            if gen_status != "ok":
                print(f"  Agent {agent_id} on {feature_id}: {gen_status} but has commits -- running QA anyway")

            # --- QA (own timeout) ---
            worker_assignments.update_assignment_phase(state_dir, wid, "evaluator")
            print(f"  QA {feature_id} in worktree")
            qa_user_msg = _build_qa_user_msg(feature_id, work_plan)
            qa_options = create_client_options(wt_dir, config)
            try:
                qa_result = await asyncio.wait_for(
                    run_agent_session(qa_user_msg, qa_options, wt_dir, system_prompt=qa_system_prompt),
                    timeout=qa_timeout,
                )
            except asyncio.TimeoutError:
                print(f"  QA {feature_id}: timed out after 15m")
                qa_result = {"output": "[QA timed out]", "cost": 0}

            # Persist evaluator session ID for crash recovery
            _eval_sid = qa_result.get("session_id")
            if _eval_sid:
                worker_assignments.update_assignment_session_id(state_dir, wid, _eval_sid, phase="evaluator")

            await result_queue.put(("result", feature_id, {
                "agent_id": agent_id, "feature": feature, "wt_dir": wt_dir,
                "branch": branch, "wid": wid,
                "gen_result": gen_result, "gen_status": gen_status, "qa_result": qa_result,
            }))
        finally:
            await _release_slot()

    # Launch all features into pool (dynamic pool throttles concurrency)
    async def _safe_worker(feature):
        """Wrapper ensuring result_queue always gets a message, even on crash."""
        try:
            await _pool_worker(feature)
        except Exception as e:
            print(f"  Worker crashed on {feature['id']}: {e}")
            await result_queue.put(("error", feature["id"], None))

    for feature in parallel_features:
        asyncio.ensure_future(_safe_worker(feature))

    # Process results as they arrive -- merges are sequential (touch main)
    processed = 0
    total = len(parallel_features)

    while processed < total:
        kind, feature_id, data = await result_queue.get()
        processed += 1

        if kind == "skipped":
            continue
        elif kind == "conflict":
            # Re-dispatch after delay — conflicting scope will free when other task finishes
            _conflict_feature = next((f for f in parallel_features if f["id"] == feature_id), None)
            if _conflict_feature:
                async def _delayed_retry(feat, delay=15):
                    await asyncio.sleep(delay)
                    await _safe_worker(feat)
                total += 1
                asyncio.ensure_future(_delayed_retry(_conflict_feature))
                print(f"  {feature_id}: scope conflict -- retry in 15s")
            else:
                conflicts.append(feature_id)
            continue
        elif kind == "error":
            # Worker crashed before producing a result — retry
            _error_feature = next((f for f in parallel_features if f["id"] == feature_id), None)
            if work_plan is not None:
                work_plan.increment_task_attempts(feature_id, work_plan_path)
            attempt = _get_attempt_count(feature_id)
            if _error_feature and attempt < max_retries:
                print(f"  {feature_id}: worker error ({attempt}/{max_retries}) -- retrying")
                total += 1
                asyncio.ensure_future(_safe_worker(_error_feature))
            else:
                failed.append(feature_id)
            continue

        # kind == "result"
        r = data
        agent_id = r["agent_id"]
        feature = r["feature"]
        wt_dir = r["wt_dir"]
        branch = r["branch"]
        wid = r["wid"]
        gen_result = r["gen_result"]
        gen_status = r["gen_status"]
        qa_result = r["qa_result"]

        cost_tracker.record("generator", gen_result.get("cost", 0),
                            phase="generator-pool", **_metrics_from(gen_result))
        cost_tracker.record_feature(feature_id, "generator", gen_result.get("cost", 0))

        # Compress discovery brief + write to shared knowledge dir
        handoff_data = parse_handoff(gen_result.get("output", ""))
        brief = compress_discovery(gen_result.get("output", ""), agent_id, feature_id, gen_status)
        write_brief(brief, wave_num, agent_id, state_dir)
        # Also write to knowledge dir so other agents can read it directly
        _knowledge_dir = state_dir / "knowledge"
        _knowledge_dir.mkdir(parents=True, exist_ok=True)
        try:
            (_knowledge_dir / f"{feature_id}.md").write_text(brief)
        except Exception as e:
            _log_error(state_dir, f"knowledge_write:{feature_id}", e)

        # No QA result -> generator failed without commits
        if qa_result is None:
            # Reset passes=True the generator may have written prematurely
            _reset_feature_passes(state_dir, feature_id)

            # Only force-delete if no commits — preserve worktrees with prior work
            if branch_has_commits(project_dir, branch):
                worker_assignments.update_assignment_status(state_dir, wid, "failed")
                print(f"  {feature_id}: gen failed but worktree has commits — preserving")
            else:
                cleanup_worktree(project_dir, wt_dir, branch, force=True)
                worker_assignments.remove_assignment(state_dir, wid)
            unregister_instance(agent_id, state_dir)
            fleet_session.record_agent_result(session, wave_num, agent_id, feature_id, gen_status)

            if work_plan is not None:
                work_plan.increment_task_attempts(feature_id, work_plan_path)
            attempt = _get_attempt_count(feature_id)
            if attempt < max_retries:
                print(f"  {feature_id}: gen {gen_status} ({attempt}/{max_retries}) -- retrying")
                fleet_session.requeue_feature(session, feature_id, reason=gen_status)
                total += 1
                asyncio.ensure_future(_safe_worker(feature))
            else:
                print(f"  {feature_id}: gen {gen_status} ({attempt}/{max_retries}) -- BLOCKED")
                failed.append(feature_id)
                if work_plan is not None:
                    work_plan.mark_task_blocked(feature_id, "max_retries_exceeded", work_plan_path)
            fleet_session.save_session(session, state_dir)
            continue

        # Record QA cost
        cost_tracker.record("evaluator", qa_result.get("cost", 0),
                            phase="evaluator-pool", **_metrics_from(qa_result))
        cost_tracker.record_feature(feature_id, "evaluator", qa_result.get("cost", 0))

        if "VERDICT: FAIL" in qa_result.get("output", ""):
            # Reset passes=True the generator may have written prematurely
            _reset_feature_passes(state_dir, feature_id)
            # Evaluator writes ./feedback.md in worktree — also persist for crash recovery.
            _fb_path = state_dir / f"feedback-{feature_id}.md"
            _wt_fb = Path(wt_dir) / "feedback.md"
            if _wt_fb.exists():
                import shutil
                shutil.copy2(_wt_fb, _fb_path)
            else:
                _fb_path.write_text(qa_result.get("output", ""))
                # Write to worktree too so retry generator sees it
                _wt_fb.write_text(qa_result.get("output", ""))

            unregister_instance(agent_id, state_dir)

            if work_plan is not None:
                work_plan.increment_task_attempts(feature_id, work_plan_path)
            attempt = _get_attempt_count(feature_id)

            fleet_session.record_agent_result(session, wave_num, agent_id, feature_id, "eval_fail")
            tracker.feature_fail(feature_id, feature.get("description", ""), "VERDICT: FAIL")

            if attempt < max_retries:
                # Keep worktree — mark assignment as failed so _find_reusable_worktree picks it up
                worker_assignments.update_assignment_status(state_dir, wid, "failed")
                print(f"  {feature_id}: QA FAIL ({attempt}/{max_retries}) -- retrying in same worktree")
                fleet_session.requeue_feature(session, feature_id, reason="eval_fail")
                total += 1
                asyncio.ensure_future(_safe_worker(feature))
            else:
                print(f"  {feature_id}: QA FAIL ({attempt}/{max_retries}) -- BLOCKED (worktree preserved)")
                # Never destroy worktree with commits — it can be recovered manually or on next resume
                worker_assignments.update_assignment_status(state_dir, wid, "blocked")
                failed.append(feature_id)
                if work_plan is not None:
                    work_plan.mark_task_blocked(feature_id, "max_retries_exceeded", work_plan_path)
            fleet_session.save_session(session, state_dir)
            continue

        # QA passed -- safe to merge
        merge_result = merge_worktree(project_dir, branch)
        unregister_instance(agent_id, state_dir)

        if not merge_result["success"]:
            # NEVER cleanup worktree on merge failure — preserve the code for retry
            if merge_result.get("conflict"):
                tracker.merge_conflict(agent_id, feature_id, merge_result.get("error", ""))
                fleet_session.requeue_feature(session, feature_id, reason="conflict")
                conflicts.append(feature_id)
                fleet_session.record_agent_result(session, wave_num, agent_id, feature_id, "conflict")
            else:
                failed.append(feature_id)
                fleet_session.record_agent_result(session, wave_num, agent_id, feature_id, "merge_error")
            worker_assignments.update_assignment_status(state_dir, wid, "failed")
            fleet_session.save_session(session, state_dir)
            continue

        # Merge succeeded — NOW safe to cleanup worktree
        cleanup_worktree(project_dir, wt_dir, branch, force=True)
        worker_assignments.remove_assignment(state_dir, wid)
        await state_mgr.mark_feature_passing_async(feature_id)
        if work_plan is not None:
            work_plan.mark_task_done(feature_id, work_plan_path)
            work_plan.sync_feature_list(state_dir / "feature_list.json")
        fleet_session.mark_feature_complete(session, feature_id)
        completed.append(feature_id)
        tracker.feature_pass(feature_id, feature.get("description", ""), 0)
        fleet_session.record_agent_result(session, wave_num, agent_id, feature_id, "complete")
        fleet_session.save_session(session, state_dir)
        print(f"  {feature_id}: QA PASS -- merged to main")

        # Keep state.json in sync
        _cur = state_mgr.load_state()
        await state_mgr.update_state_async(
            phase="generator",
            current_feature_id=feature_id,
            iteration=_cur.get("iteration", 0) + 1,
            generator_sessions=_cur.get("generator_sessions", 0) + 1,
            total_cost_usd=cost_tracker.total,
            cost_breakdown=cost_tracker.to_dict(),
            last_updated=datetime.now(timezone.utc).isoformat(),
        )

        # Add discoveries
        if handoff_data.get("found") and handoff_data.get("items"):
            for item in handoff_data["items"][:3]:
                fleet_session.add_discovery(session, wave_num, f"Wave {wave_num} {feature_id}: {item}")

    # Complete wave
    fleet_session.complete_wave(session, wave_num)
    fleet_session.save_session(session, state_dir)
    tracker.wave_complete(wave_num, len(completed), len(failed), len(conflicts))

    return {"completed": completed, "failed": failed, "conflicts": conflicts}


def _setup_run(
    project_dir: Path,
    resume: bool = False,
    run_id: Optional[str] = None,
    prompt: str = "",
) -> tuple:
    """Set up run isolation. Returns (run_id, state_mgr, registry).

    - New run: creates .harness/runs/<run_id>/ and index entry
    - Resume without run_id: finds latest in_progress run
    - Resume with run_id: targets that specific run
    - Auto-migrates legacy .harness/state/ layout
    """
    harness_dir = project_dir / ".harness"
    harness_dir.mkdir(parents=True, exist_ok=True)

    registry = RunRegistry(harness_dir)

    # Auto-migrate legacy state/ directory if present
    registry.migrate_legacy()

    if resume and run_id:
        # Explicit run target
        state_mgr = StateManager(registry.run_dir(run_id))
        return run_id, state_mgr, registry

    if resume:
        # Find latest incomplete run
        found = registry.find_resumable()
        if found:
            state_mgr = StateManager(registry.run_dir(found))
            return found, state_mgr, registry
        # Nothing to resume — fall through to create new

    # Create new run
    new_id = registry.create_run(prompt=prompt or "")
    state_mgr = StateManager(registry.run_dir(new_id))
    return new_id, state_mgr, registry


async def resume_interrupted_workers(
    project_dir: Path,
    state_dir: Path,
    config: dict,
    prompts_dir: Path,
    cost_tracker: "CostTracker",
    tracker: "EventTracker",
    state_mgr: "StateManager",
    work_plan: "WorkPlan",
    work_plan_path: Path,
) -> dict:
    """Resume agents into existing worktrees that have unmerged commits.

    Reads worker_assignments.json, finds worktrees with commits ahead of main,
    and re-dispatches agents with a recovery prompt.

    Returns {completed: list[str], failed: list[str], resumed: int}.
    """
    import subprocess as _sp

    # Kill orphaned claude processes from previous orchestrator in our worktrees.
    # When the orchestrator is kill -9'd, child claude processes survive as orphans.
    _worktree_base = str(project_dir / ".worktrees")
    try:
        _ps = _sp.run(
            ["pgrep", "-f", f"claude.*{_worktree_base}"],
            capture_output=True, text=True,
        )
        for _pid_str in _ps.stdout.strip().splitlines():
            _pid = int(_pid_str.strip())
            try:
                import os
                os.kill(_pid, 9)
                print(f"  Killed orphaned agent PID {_pid}")
            except (ProcessLookupError, PermissionError):
                pass
    except Exception:
        pass  # pgrep not available or no matches — safe to continue

    # Clean stale coordination state left by dead agents before checking assignments
    from .coordination import sweep_stale_instances as _sweep_stale
    _swept = _sweep_stale(state_dir, stale_hours=0.01)  # ~36 seconds — aggressive on resume
    if _swept:
        print(f"  Resume: swept {len(_swept)} stale coordination entries")

    resumable = worker_assignments.get_resumable_assignments(state_dir)
    if not resumable:
        return {"completed": [], "failed": [], "resumed": 0}

    completed, failed = [], []
    dispatched = []
    _pending_evals = []  # (wid, assignment, wt_dir, branch, feature_id)
    _needs_dispatch = set()  # wids that need recovery generator

    for wid, assignment in resumable.items():
        wt_dir = Path(assignment["worktree_dir"])
        branch = assignment["branch"]
        feature_id = assignment["feature_id"]

        # Decision matrix: check worktree and branch state
        wt_exists = wt_dir.exists()
        br_has_commits = branch_has_commits(project_dir, branch)

        if not wt_exists and not br_has_commits:
            # Nothing to recover — clear assignment, task goes back to pending pool
            print(f"  Resume skip {feature_id}: no worktree or branch — returning to pool")
            worker_assignments.remove_assignment(state_dir, wid)
            continue

        if wt_exists and not br_has_commits:
            # Worktree exists but no commits — nothing to preserve, clean up
            print(f"  Resume skip {feature_id}: no commits — cleaning up")
            cleanup_worktree(project_dir, wt_dir, branch, force=True)
            worker_assignments.remove_assignment(state_dir, wid)
            continue

        if not wt_exists and br_has_commits:
            # Branch exists with commits but worktree gone — recreate worktree from branch
            print(f"  Resume {feature_id}: recreating worktree from branch {branch}")
            try:
                _sp.run(
                    ["git", "worktree", "add", str(wt_dir), branch],
                    cwd=project_dir, capture_output=True, text=True, check=True,
                )
                _init_submodules_local(project_dir, wt_dir)
            except Exception as e:
                print(f"  Resume failed for {feature_id}: {e}")
                worker_assignments.remove_assignment(state_dir, wid)
                failed.append(feature_id)
                continue

        # Ensure submodules are populated (older worktrees may have empty dirs)
        _init_submodules_local(project_dir, wt_dir)

        # Check if evaluator already passed (pending-merge: commit exists, eval passed,
        # but merge never happened because orchestrator was killed mid-pipeline).
        # Detect by checking for VERDICT: PASS in feedback or evaluator phase completion.
        _phase = assignment.get("phase", "generator")
        _gen_sid = assignment.get("gen_session_id") or None
        _eval_sid = assignment.get("eval_session_id") or None

        if _phase == "evaluator" and br_has_commits:
            # Evaluator was running or finished — re-evaluate to determine pass/fail
            _pending_evals.append((wid, assignment, wt_dir, branch, feature_id))
            continue

        # Failed workers with commits: collect for parallel evaluation below.
        if assignment.get("status") == "failed" and br_has_commits:
            _pending_evals.append((wid, assignment, wt_dir, branch, feature_id))
            continue

        # Status is "running" or "interrupted" in generator phase — needs recovery generator
        _needs_dispatch.add(wid)

    # --- Parallel evaluation of failed worktrees ---
    if _pending_evals:
        print(f"  Evaluating {len(_pending_evals)} failed worktree(s) in parallel...")
        test_command = config.get("evaluator", {}).get("test_suite_command", "echo 'No test command'")
        timeout_mins = config.get("parallel", {}).get("agent_timeout_minutes", 30)

        async def _eval_one(wid, assignment, wt_dir, branch, feature_id):
            eval_repl, eval_file, eval_fallback, eval_user_msg = _build_eval_replacements(
                feature_id, state_dir, config, test_command, work_plan=work_plan)
            _eval_path = prompts_dir / eval_file if (prompts_dir / eval_file).exists() else prompts_dir / eval_fallback
            eval_system = load_prompt(_eval_path, eval_repl)
            eval_options = create_client_options(wt_dir, config)
            try:
                result = await asyncio.wait_for(
                    run_agent_session(eval_user_msg, eval_options, wt_dir, system_prompt=eval_system),
                    timeout=15 * 60,
                )
            except asyncio.TimeoutError:
                result = {"output": "[eval timed out]", "cost": 0}
            return wid, assignment, wt_dir, branch, feature_id, result

        eval_tasks = [
            _eval_one(wid, a, wt, br, fid)
            for wid, a, wt, br, fid in _pending_evals
        ]
        eval_results = await asyncio.gather(*eval_tasks, return_exceptions=True)

        for item in eval_results:
            if isinstance(item, Exception):
                print(f"  Recovery eval crashed: {item}")
                continue
            wid, assignment, wt_dir, branch, feature_id, eval_result = item
            cost_tracker.record("evaluator", eval_result.get("cost", 0),
                                phase="evaluator-recovery", **_metrics_from(eval_result))

            if "VERDICT: FAIL" not in eval_result.get("output", ""):
                # Passed — merge sequentially (touches main)
                merge_result = merge_worktree(project_dir, branch)
                if merge_result["success"]:
                    cleanup_worktree(project_dir, wt_dir, branch, force=True)
                    worker_assignments.remove_assignment(state_dir, wid)
                    await state_mgr.mark_feature_passing_async(feature_id)
                    if work_plan is not None:
                        work_plan.mark_task_done(feature_id, work_plan_path)
                        work_plan.sync_feature_list(state_dir / "feature_list.json")
                    completed.append(feature_id)
                    print(f"  Resume {feature_id}: evaluated + merged → PASSED")
                    _cur = state_mgr.load_state()
                    await state_mgr.update_state_async(
                        phase="generator",
                        current_feature_id=feature_id,
                        iteration=_cur.get("iteration", 0) + 1,
                        generator_sessions=_cur.get("generator_sessions", 0) + 1,
                        total_cost_usd=cost_tracker.total,
                        cost_breakdown=cost_tracker.to_dict(),
                        last_updated=datetime.now(timezone.utc).isoformat(),
                    )
                else:
                    print(f"  Resume {feature_id}: merge failed — {merge_result.get('error', '')[:100]}")
                    failed.append(feature_id)
                continue

            # FAILED — persist feedback, dispatch recovery generator
            import shutil
            _wt_fb = wt_dir / "feedback.md"
            if _wt_fb.exists():
                shutil.copy2(_wt_fb, state_dir / f"feedback-{feature_id}.md")
            print(f"  Resume {feature_id}: evaluator FAILED — dispatching recovery generator")
            worker_assignments.update_assignment_status(state_dir, wid, "running")
            _needs_dispatch.add(wid)

    # --- Dispatch recovery generators ---
    # _needs_dispatch contains: items from first loop (status=running/interrupted)
    # + items that just failed parallel eval (added above)
    for wid in _needs_dispatch:
        assignment = resumable[wid]
        wt_dir = Path(assignment["worktree_dir"])
        if not wt_dir.exists():
            continue
        branch = assignment["branch"]
        feature_id = assignment["feature_id"]

        # Worktree exists + branch has commits → re-dispatch with recovery prompt
        print(f"  Resuming {feature_id} in {wt_dir}")

        # Build git log context for recovery prompt
        git_log = _sp.run(
            ["git", "log", "--oneline", "main..HEAD"],
            cwd=wt_dir, capture_output=True, text=True,
        ).stdout.strip() or "(no commits)"

        # Look up feature description from work_plan phases
        feature_desc = feature_id
        if work_plan:
            for ph in work_plan.data.get("phases", []):
                for ep in ph.get("epics", []):
                    for st in ep.get("stories", []):
                        for t in st.get("tasks", []):
                            if t.get("id") == feature_id:
                                feature_desc = t.get("description", feature_id)

        # Write task brief for resumed agent (include relay from all available briefs)
        _feature_data = {"id": feature_id, "description": feature_desc,
                         "scope": assignment.get("scope", [])}
        _resume_relay = get_relay_context(state_dir, current_wave=assignment.get("wave", 1), include_current_wave=True)
        write_task_brief(wt_dir, _feature_data, state_dir, work_plan=work_plan, relay_context=_resume_relay)

        # Check for resumable session ID from the interrupted agent
        _resume_sid = assignment.get("gen_session_id") or None

        if _resume_sid:
            # Resume the exact interrupted session — agent has full context
            user_msg = (
                f"Your session was interrupted while working on {feature_id}. "
                f"You are in the same worktree with all committed work intact. "
                f"Check feedback.md if it exists, then continue where you left off."
            )
            gen_system = None  # system prompt already baked into session
            print(f"  {feature_id}: resuming interrupted session")
        else:
            # No session ID — fall back to recovery prompt
            resume_replacements = {
                "FEATURE_ID": feature_id,
                "FEATURE_DESC": feature_desc,
                "GIT_LOG": git_log,
            }
            recovery_preamble = load_prompt(prompts_dir / "generator-resume.md", resume_replacements)

            gen_system_replacements = {
                "STATE_DIR": str(state_dir),
                "TEST_COMMAND": config.get("evaluator", {}).get("test_suite_command", "echo 'No test command'"),
                "IF_WEB_PROJECT": config.get("evaluator", {}).get("browser_verification") != "never",
            }
            prompt_file = "generator-v2.md" if (prompts_dir / "generator-v2.md").exists() else resolve_prompt("generator", config)
            gen_system = load_prompt(prompts_dir / prompt_file, gen_system_replacements)
            user_msg = recovery_preamble + f"\nYour assigned feature: {feature_id} — {feature_desc}\nRead TASK_BRIEF.md in this directory for full details."
            print(f"  {feature_id}: no session to resume — using recovery prompt")

        # Update assignment status
        agent_id = generate_instance_id()
        worker_assignments.update_assignment_status(state_dir, wid, "running")

        gen_options = create_client_options(wt_dir, config)
        dispatched.append((agent_id, feature_id, wt_dir, branch, wid, user_msg, gen_system, gen_options, _resume_sid))

    if not dispatched:
        return {"completed": completed, "failed": failed, "resumed": 0}

    print(f"  Resuming {len(dispatched)} interrupted worker(s)...")

    # Launch resumed agents in parallel
    async def _resume_one(prompt, options, wt_dir, system_prompt=None, resume_session_id=None):
        return await run_agent_session(prompt, options, wt_dir, system_prompt=system_prompt, resume_session_id=resume_session_id)

    timeout_minutes = config.get("parallel", {}).get("agent_timeout_minutes", 30)
    tasks = []
    for agent_id, feature_id, wt_dir, branch, wid, prompt, sys_prompt, options, resume_sid in dispatched:
        coro = _resume_one(prompt, options, wt_dir, system_prompt=sys_prompt, resume_session_id=resume_sid)
        wrapped = asyncio.wait_for(coro, timeout=timeout_minutes * 60)
        tasks.append((agent_id, feature_id, wt_dir, branch, wid, asyncio.ensure_future(wrapped)))

    # Gather and process results
    test_command = config.get("evaluator", {}).get("test_suite_command", "echo 'No test command'")
    for agent_id, feature_id, wt_dir, branch, wid, task in tasks:
        try:
            result = await task
            output = result.get("output", "")

            has_status_block = "---HARNESS_STATUS---" in output
            has_commits = branch_has_commits(project_dir, branch)

            if (result.get("status") == "error" or not has_status_block) and not has_commits:
                # Agent failed AND produced no commits — nothing to salvage
                print(f"  Resumed agent on {feature_id}: failed (no status block, no commits)")
                worker_assignments.update_assignment_status(state_dir, wid, "failed")
                failed.append(feature_id)
                continue

            if not has_status_block and has_commits:
                print(f"  Resumed agent on {feature_id}: no status block but has commits — evaluating")

            # Evaluate IN the worktree first — only merge if eval passes
            print(f"  Resumed {feature_id}: running evaluator in worktree")
            eval_repl, eval_file, eval_fallback, eval_user_msg = _build_eval_replacements(
                feature_id, state_dir, config, test_command, work_plan=work_plan)
            _eval_path = prompts_dir / eval_file if (prompts_dir / eval_file).exists() else prompts_dir / eval_fallback
            eval_system = load_prompt(_eval_path, eval_repl)
            eval_options = create_client_options(wt_dir, config)
            try:
                eval_result = await asyncio.wait_for(
                    run_agent_session(eval_user_msg, eval_options, wt_dir, system_prompt=eval_system),
                    timeout=15 * 60,
                )
            except asyncio.TimeoutError:
                print(f"  Resumed {feature_id}: eval timed out (15m) — preserving worktree")
                worker_assignments.update_assignment_status(state_dir, wid, "failed")
                failed.append(feature_id)
                continue
            cost_tracker.record("evaluator", eval_result.get("cost", 0),
                                phase="evaluator-resume", **_metrics_from(eval_result))

            if "VERDICT: FAIL" in eval_result.get("output", ""):
                # Feedback stays in worktree for retry gen; also persist for crash recovery
                import shutil as _sh
                _wt_fb = wt_dir / "feedback.md"
                _persist_fb = state_dir / f"feedback-{feature_id}.md"
                if _wt_fb.exists():
                    _sh.copy2(_wt_fb, _persist_fb)
                elif eval_result.get("output"):
                    _persist_fb.write_text(eval_result["output"])
                    _wt_fb.write_text(eval_result["output"])
                # Preserve worktree — pool retry will reuse it
                worker_assignments.update_assignment_status(state_dir, wid, "failed")
                print(f"  Resumed {feature_id}: eval FAIL — worktree preserved, returning to pool")
                failed.append(feature_id)
            else:
                # Eval passed — now merge
                merge_result = merge_worktree(project_dir, branch)
                if not merge_result["success"]:
                    print(f"  Resumed {feature_id}: eval PASS but merge failed — preserving")
                    worker_assignments.update_assignment_status(state_dir, wid, "failed")
                    failed.append(feature_id)
                    continue

                cleanup_worktree(project_dir, wt_dir, branch, force=True)
                worker_assignments.remove_assignment(state_dir, wid)
                await state_mgr.mark_feature_passing_async(feature_id)
                if work_plan is not None:
                    work_plan.mark_task_done(feature_id, work_plan_path)
                    work_plan.sync_feature_list(state_dir / "feature_list.json")
                completed.append(feature_id)
                print(f"  Resumed {feature_id}: PASSED + merged")

                _cur = state_mgr.load_state()
                await state_mgr.update_state_async(
                    phase="generator",
                    current_feature_id=feature_id,
                    iteration=_cur.get("iteration", 0) + 1,
                    generator_sessions=_cur.get("generator_sessions", 0) + 1,
                    total_cost_usd=cost_tracker.total,
                    cost_breakdown=cost_tracker.to_dict(),
                    last_updated=datetime.now(timezone.utc).isoformat(),
                )

        except asyncio.TimeoutError:
            print(f"  Resumed agent on {feature_id}: timed out")
            worker_assignments.update_assignment_status(state_dir, wid, "failed")
            failed.append(feature_id)
        except Exception as e:
            print(f"  Resumed agent on {feature_id}: error — {e}")
            worker_assignments.update_assignment_status(state_dir, wid, "failed")
            failed.append(feature_id)

    return {"completed": completed, "failed": failed, "resumed": len(dispatched)}


async def run_harness(
    config_path: Path,
    project_dir: Path,
    prompt: Optional[str] = None,
    spec_path: Optional[Path] = None,
    plan_path: Optional[Path] = None,
    resume: bool = False,
    run_id: Optional[str] = None,
    cli_overrides: Optional[dict] = None,
) -> dict:
    """Main harness entry point.

    Runs the full planner → generator ↔ evaluator loop.
    Returns summary dict.
    """
    config = load_config(config_path)
    if cli_overrides:
        config.update(cli_overrides)

    # Run isolation: each run gets its own directory
    active_run_id, state_mgr, registry = _setup_run(
        project_dir, resume=resume, run_id=run_id, prompt=prompt or ""
    )
    state_dir = state_mgr.state_dir

    cb = CircuitBreaker(state_dir, config.get("circuit_breaker", {}))
    cost_tracker = CostTracker(log_path=state_dir / "token_log.jsonl")
    prompts_dir = Path(__file__).parent.parent / "prompts"

    cp = ControlPlaneClient(state_dir=state_dir)
    try:
        if resume:
            cp.resume_run(project_dir.name, str(project_dir))
        else:
            cp.create_run(project_dir.name, str(project_dir))
    except Exception:
        pass

    # Create EventTracker for full lifecycle observability (feature 040)
    tracker = EventTracker(cp)

    # --- Signal handling for graceful shutdown ---
    import signal
    global _shutdown_requested
    _shutdown_requested = False
    _prev_sigint = signal.getsignal(signal.SIGINT)
    _prev_sigterm = signal.getsignal(signal.SIGTERM)

    def _handle_shutdown(sig, frame):
        global _shutdown_requested
        if _shutdown_requested:
            # Second signal — hard stop: kill workers, preserve assignments
            print("\nHard stop — killing workers, preserving worktree assignments...")
            worker_assignments.mark_all_running_as_interrupted(state_dir)
            for proc in list(_active_worker_procs):
                try:
                    proc.kill()
                except Exception:
                    pass
            raise KeyboardInterrupt
        _shutdown_requested = True
        print("\nShutdown requested. Waiting for active workers to finish (Ctrl+C again to force)...")

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    start_time = time.time()
    state = state_mgr.load_state()

    # Resume from previous run
    if resume and state["phase"] != "init":
        cost_tracker = CostTracker.from_dict(state.get("cost_breakdown", {}))
        cost_tracker.log_path = state_dir / "token_log.jsonl"
        print(f"Resuming run {active_run_id} from phase: {state['phase']}, iteration: {state['iteration']}")
        # Reset stale state — current_feature_id reflects the last killed session,
        # not what will actually run next. Clear drain state if resuming after drain.
        # Increment iteration so state.json reflects a new cycle started.
        state_mgr.update_state(
            phase="generator",
            current_feature_id=None,
            evaluator_retries_current_feature=0,
            iteration=state.get("iteration", 0) + 1,
            last_updated=datetime.now(timezone.utc).isoformat(),
        )
        _clear_drain(state_dir)  # in case drain file lingered
    else:
        state = state_mgr.update_state(
            started_at=datetime.now(timezone.utc).isoformat(),
            phase="init",
        )

    # Initialize run log
    _write_run_log(state_dir, "run_start",
                   run_id=active_run_id, project=str(project_dir),
                   model=config.get("model", "default"),
                   resume=resume)

    print("=" * 60)
    print("  LONG-RUNNING AGENT HARNESS")
    print("=" * 60)
    print(f"Project: {project_dir}")
    print(f"Model: {config.get('model', 'default')}")
    print(f"Planner model: {config.get('planner_model', config.get('model', 'default'))}")
    print(f"Limits: cost=${config.get('max_cost_usd', 'unlimited')}, "
          f"time={config.get('max_duration_minutes', 'unlimited')}m, "
          f"iterations={config.get('max_iterations', 'unlimited')}")
    print()

    # Sweep merged worktrees at run start (feature 040)
    removed = []
    try:
        removed_list = sweep_merged_worktrees(project_dir)
        removed = removed_list or []
        if removed:
            print(f"  Worktree sweep (start): removed {len(removed)} merged worktree(s)")
    except Exception as e:
        print(f"  Worktree sweep (start) failed (non-fatal): {e}")
    tracker.worktree_sweep("start", len(removed))

    # Pre-flight: ensure browser tools available for evaluator (feature 040)
    browser_result = ensure_browser_tools(project_dir, config)
    tracker.browser_preflight(browser_result or "ok")

    # Knowledge query (feature 040)
    knowledge_section = _query_knowledge_for_planner()
    chunk_count = len(knowledge_section.split("\n\n")) if knowledge_section else 0
    tracker.knowledge_query(chunk_count)

    # Determine strategy (feature 038) + delegation mode
    delegation_mode = config.get("delegation", {}).get("mode", "full-plan")
    default_strategy = config.get("default_strategy")
    strategies = config.get("strategies", {})
    # Only use multi-role pipeline in full-plan mode
    use_pipeline = bool(
        delegation_mode == "full-plan"
        and default_strategy
        and strategies.get(default_strategy)
    )

    # === PHASE 1: PLANNER ===
    # Run planner if not complete — resume skips completed roles internally via planner_roles state
    if not state.get("planner_complete"):
        print(f"--- PHASE 1: PLANNER (delegation: {delegation_mode}){' (resuming)' if resume else ''} ---")

        if use_pipeline:
            # Feature 038: multi-phase pipeline (full-plan mode only)
            strategy_config = strategies[default_strategy]
            input_section = _build_input_section(prompt, spec_path, plan_path)
            await run_planner_pipeline(
                strategy_config=strategy_config,
                prompts_dir=prompts_dir,
                project_dir=project_dir,
                state_dir=state_dir,
                state_mgr=state_mgr,
                cost_tracker=cost_tracker,
                knowledge_section=knowledge_section,
                input_section=input_section,
                event_tracker=tracker,
                config=config,
            )
            use_work_plan = True
        else:
            # Feature 039: legacy single-session planner
            planner_prompt = build_planner_prompt(
                prompts_dir, prompt, spec_path, plan_path, state_dir, config
            )
            planner_options = create_client_options(
                project_dir,
                config,
                model_override=config.get("planner_model"),
                system_prompt="You are a product architect designing a comprehensive application specification.",
            )
            result = await run_agent_session(planner_prompt, planner_options, project_dir, progress_label="Planner")
            cost_tracker.record("planner", result["cost"],
                                phase="planner", **_metrics_from(result))
            print(f"  [Planner] complete. Cost: ${result['cost']:.2f}")
            use_work_plan = False

        state_mgr.update_state(
            phase="generator",
            planner_complete=True,
            total_cost_usd=cost_tracker.total,
            cost_breakdown=cost_tracker.to_dict(),
        )

        # Validate planner output (feature 046)
        validation = validate_planner_output(state_dir, use_work_plan=use_work_plan)
        if not validation["valid"]:
            print(f"  Planner validation failed: {validation['reason']}")
            if not use_pipeline:
                print("  Re-running planner with stricter prompt...")
                planner_prompt = build_planner_prompt(
                    prompts_dir, prompt, spec_path, plan_path, state_dir
                )
                planner_options = create_client_options(
                    project_dir, config, model_override=config.get("planner_model"),
                    system_prompt="You are a product architect designing a comprehensive application specification.",
                )
                retry_prompt = (
                    planner_prompt
                    + "\n\n## CRITICAL: PREVIOUS PLANNER RUN FAILED VALIDATION\n\n"
                    + f"Reason: {validation['reason']}\n\n"
                    + "You MUST:\n"
                    + "1. Run all 3 review passes (Technical Gaps, AI Failure Modes, Community Alignment)\n"
                    + "2. Write findings to SPEC_GAPS.md at {{STATE_DIR}}/SPEC_GAPS.md\n"
                    + "3. Write a non-empty feature_list.json\n"
                ).replace("{{STATE_DIR}}", str(state_dir))
                result2 = await run_agent_session(retry_prompt, planner_options, project_dir)
                cost_tracker.record("planner", result2["cost"],
                                    phase="planner-retry", **_metrics_from(result2))
                validation2 = validate_planner_output(state_dir, use_work_plan=False)
                if not validation2["valid"]:
                    print(f"  Planner still invalid after retry: {validation2['reason']}")
                    print("  Proceeding anyway — evaluator will catch gaps.")
                else:
                    print(f"  Planner validation passed on retry. Cost: ${result2['cost']:.2f}")
        else:
            print(f"  Planner validation: output present")

        # Feature 040: announce run_started with task count
        if use_work_plan:
            try:
                wp = WorkPlan.load(state_dir / "work_plan.json")
                n = wp.count_tasks()["total"]
            except Exception:
                n = 0
        else:
            n = state_mgr.count_features().get("total", 0)
        tracker.run_started(n)

        # Push work plan to control plane for dashboard task view
        if use_work_plan:
            try:
                wp_data = json_mod.loads((state_dir / "work_plan.json").read_text())
                tracker.push_work_plan(wp_data)
            except Exception as e:
                _log_error(state_dir, "push_work_plan_planner", e)

        _write_run_log(state_dir, "planner_complete",
                       tasks=n, cost_usd=cost_tracker.total)
        print()

    # === PHASE 2: GENERATOR ↔ EVALUATOR LOOP ===
    print("--- PHASE 2: GENERATOR + EVALUATOR LOOP ---")

    # Feature 044: auto-convert feature_list.json → work_plan.json if needed
    work_plan_path = state_dir / "work_plan.json"
    feature_list_path = state_dir / "feature_list.json"
    work_plan: Optional[WorkPlan] = None

    if work_plan_path.exists():
        try:
            work_plan = WorkPlan.load(work_plan_path)
            # Sync feature_list.json from work_plan so generator prompt can read it
            work_plan.sync_feature_list(state_dir / "feature_list.json")
            print(f"  Loaded work_plan.json: {work_plan.count_tasks()['total']} tasks (synced → feature_list.json)")
            # Push work plan to control plane (covers resume path)
            try:
                wp_data = json_mod.loads(work_plan_path.read_text())
                tracker.push_work_plan(wp_data)
            except Exception as e:
                _log_error(state_dir, "push_work_plan_resume", e)
        except Exception as e:
            print(f"  WARNING: work_plan.json load failed ({e}), falling back to feature list")
    elif feature_list_path.exists():
        try:
            import json as _json
            features = _json.loads(feature_list_path.read_text())
            work_plan = WorkPlan.from_flat_features(features)
            work_plan.save(work_plan_path)
            print(f"  Auto-converted feature_list.json → work_plan.json: {work_plan.count_tasks()['total']} tasks")
        except Exception as e:
            print(f"  WARNING: auto-conversion failed ({e})")

    # Feature 045: DAG validation at generator start
    if work_plan is not None:
        dag_result = work_plan.validate_dag()
        if not dag_result["valid"]:
            print(f"\nERROR: work_plan.json has invalid dependency graph:")
            for err in dag_result["errors"]:
                print(f"  - {err}")
            print("Cannot proceed — fix the work plan before running the generator.")
            return {
                "duration_minutes": 0,
                "iterations": 0,
                "features": {"total": 0, "passing": 0, "blocked": 0, "remaining": 0},
                "cost": cost_tracker.to_dict(),
                "circuit_breaker_opens": 0,
            }
        else:
            tc = work_plan.count_tasks()
            print(f"  DAG validation: OK ({tc['total']} tasks, {len(work_plan.data.get('phases',[]))} phases)")

    prev_feature_counts = None
    iteration = state.get("iteration", 0)
    prev_phase_id = work_plan.current_phase()["id"] if (work_plan and work_plan.current_phase()) else None

    # === PARALLEL DISPATCH (Fleet mode) ===
    parallel_config = config.get("parallel", {})
    session = None  # fleet session; also used by sequential loop for status tracking
    if parallel_config.get("enabled") and work_plan is not None:
        print("--- PARALLEL MODE: Fleet wave execution ---")

        # Resume interrupted workers before starting new waves
        if resume:
            resume_result = await resume_interrupted_workers(
                project_dir=project_dir,
                state_dir=state_dir,
                config=config,
                prompts_dir=prompts_dir,
                cost_tracker=cost_tracker,
                tracker=tracker,
                state_mgr=state_mgr,
                work_plan=work_plan,
                work_plan_path=state_dir / "work_plan.json",
            )
            if resume_result["resumed"] > 0:
                print(f"  Resumed {resume_result['resumed']} worker(s): "
                      f"{len(resume_result['completed'])} completed, "
                      f"{len(resume_result['failed'])} failed")

        flat_tasks = work_plan.flatten_for_grouping()
        layers = group_by_dependency(flat_tasks)
        print(f"  {len(flat_tasks)} tasks → {len(layers)} dependency layer(s)")

        if len(layers) > 0:
            # Seed session with confirmed state from work_plan so session.json
            # is immediately consistent with reality (survives kill/resume cycles)
            all_wp_tasks = [
                t for ph in work_plan.data.get("phases", [])
                for ep in ph.get("epics", [])
                for st in ep.get("stories", [])
                for t in st.get("tasks", [])
            ]
            wp_done = [t["id"] for t in all_wp_tasks if t.get("status") == "done"]
            wp_blocked = [t["id"] for t in all_wp_tasks if t.get("status") == "blocked"]
            session = fleet_session.init_session(
                state_dir, len(layers),
                already_completed=wp_done,
                already_failed=wp_blocked,
            )

            for wave_num, layer in enumerate(layers, start=1):
                # Add features to session queue
                for feat in layer:
                    fleet_session.add_to_queue(
                        session, feat["id"], feat.get("description", ""),
                        feat.get("scope", []), wave_num,
                    )
                fleet_session.save_session(session, state_dir)  # visibility: queue populated

                pending_in_layer = [f for f in layer if not f.get("passes") and not f.get("blocked")]
                if not pending_in_layer:
                    continue

                print(f"  Wave {wave_num}: {len(layer)} features")
                wave_result = await run_parallel_wave(
                    layer=layer,
                    wave_num=wave_num,
                    project_dir=project_dir,
                    state_dir=state_dir,
                    config=config,
                    prompts_dir=prompts_dir,
                    cost_tracker=cost_tracker,
                    tracker=tracker,
                    state_mgr=state_mgr,
                    work_plan=work_plan,
                    work_plan_path=state_dir / "work_plan.json",
                    session=session,
                )
                print(f"  Wave {wave_num} result: {len(wave_result['completed'])} done, "
                      f"{len(wave_result['failed'])} failed, {len(wave_result['conflicts'])} conflicts")
                # Persist session state after each wave so fleet dir is live
                fleet_session.save_session(session, state_dir)

                # If all agents failed, stop parallel and fall through to sequential
                if not wave_result["completed"] and (wave_result["failed"] or wave_result["conflicts"]):
                    print(f"  Wave {wave_num}: no completions — falling back to sequential")
                    break

            fleet_session.complete_session(session)
            fleet_session.save_session(session, state_dir)
            tc = work_plan.count_tasks()
            print(f"  Fleet complete: {tc['done']}/{tc['total']} tasks done, "
                  f"{tc['pending']} remaining for sequential loop")

    # === SEQUENTIAL LOOP (existing behavior, handles remaining features) ===
    while True:
        # Check drain before spawning new agents
        if _check_drain(state_dir):
            print("\nDrain requested — stopping after current cycle completes.")
            state_mgr.update_state(
                phase="drained",
                last_updated=datetime.now(timezone.utc).isoformat(),
            )
            _clear_drain(state_dir)
            break

        iteration += 1
        elapsed = time.time() - start_time

        # Check exit conditions
        current_state = state_mgr.load_state()
        current_state["iteration"] = iteration
        current_state["total_cost_usd"] = cost_tracker.total
        state_mgr.save_state(current_state)

        exit_check = check_exit_conditions(current_state, config, elapsed)
        if exit_check:
            print(f"\n{exit_check['reason']}")
            break

        # Check circuit breaker
        if cb.is_permanently_halted:
            print(f"\nCircuit breaker: PERMANENTLY HALTED ({cb.total_opens} opens)")
            break

        if cb.state == CircuitBreaker.OPEN:
            if not cb.check_cooldown():
                print(f"\nCircuit breaker: OPEN (cooling down)")
                break

        # Check completion — use work_plan counts if available, else feature_list
        test_command = config.get("evaluator", {}).get("test_suite_command")
        if work_plan is not None:
            tc = work_plan.count_tasks()
            if tc["total"] > 0 and tc["pending"] == 0:
                # All tasks done or blocked — check test suite
                test_ok = True
                if test_command:
                    from .completion import run_test_suite
                    test_result = run_test_suite(test_command, project_dir)
                    test_ok = test_result["passed"]
                if test_ok:
                    state_mgr.record_exit_signal("completion", iteration)
                    signals = state_mgr.load_exit_signals()
                    if len(signals.get("completion_signals", [])) >= 2:
                        print(f"\nComplete: all {tc['total']} tasks done ({tc['done']} done, {tc['blocked']} blocked)")
                        break
        else:
            completion = check_completion(
                project_dir / ".claude" / "harness", test_command
            )
            if completion["complete"]:
                state_mgr.record_exit_signal("completion", iteration)
                signals = state_mgr.load_exit_signals()
                if len(signals.get("completion_signals", [])) >= 2:
                    print(f"\nComplete: {completion['reason']}")
                    break

        # Feature 043: Get next task from WorkPlan or fall back to feature list
        if work_plan is not None:
            # Feature 045: phase transition logging
            current_phase = work_plan.current_phase()
            if current_phase and current_phase.get("id") != prev_phase_id:
                print(f"\n  Phase transition: {prev_phase_id} → {current_phase['id']} ({current_phase.get('name', '')})")
                tracker.feature_start(
                    f"phase-{current_phase['id']}",
                    f"Phase transition: {current_phase.get('name', '')}",
                )
                prev_phase_id = current_phase["id"]

            next_task = work_plan.get_next_task()
            if next_task is None:
                print("\nNo remaining tasks to work on.")
                state_mgr.record_exit_signal("completion", iteration)
                break
            next_feature = next_task
            feature_id = next_feature["id"]
        else:
            next_feature = state_mgr.get_next_feature()
            if not next_feature:
                print("\nNo remaining features to work on.")
                state_mgr.record_exit_signal("completion", iteration)
                break
            feature_id = next_feature["id"]

        feature_start_time = time.time()
        feature_desc = next_feature.get("description", "")
        print(f"\n[Iteration {iteration}] Feature {feature_id}: {feature_desc[:60]}")
        tracker.feature_start(feature_id, feature_desc)

        # Track sequential progress in session for dashboard visibility
        if session is not None:
            session["mode"] = "sequential"
            session["current_sequential_feature"] = feature_id
            session["sequential_iteration"] = iteration
            fleet_session.save_session(session, state_dir)

        # --- GENERATOR SESSION ---
        # Write task brief so agent doesn't need to read giant state files
        _seq_relay = get_relay_context(state_dir, current_wave=0, include_current_wave=True)
        write_task_brief(project_dir, next_feature, state_dir, work_plan=work_plan, relay_context=_seq_relay)

        gen_system_replacements = {
            "STATE_DIR": str(state_dir),
            "TEST_COMMAND": test_command or "echo 'No test command configured'",
            "IF_WEB_PROJECT": config.get("evaluator", {}).get("browser_verification") != "never",
        }
        prompt_file = "generator-v2.md" if (prompts_dir / "generator-v2.md").exists() else resolve_prompt("generator", config)
        gen_system = load_prompt(prompts_dir / prompt_file, gen_system_replacements)
        gen_user_msg = f"Your assigned feature: {feature_id} — {feature_desc}\nRead TASK_BRIEF.md in this directory for full details."
        gen_options = create_client_options(project_dir, config)

        gen_result = await run_agent_session(gen_user_msg, gen_options, project_dir, system_prompt=gen_system)
        cost_tracker.record("generator", gen_result["cost"],
                            phase="generator-seq", **_metrics_from(gen_result))
        cost_tracker.record_feature(feature_id, "generator", gen_result["cost"])

        # Write discovery brief + shared knowledge
        try:
            _brief = compress_discovery(gen_result.get("output", ""), feature_id, feature_id,
                                        "ok" if gen_result.get("status") != "error" else "error")
            write_brief(_brief, 0, feature_id, state_dir)
            _knowledge_dir = state_dir / "knowledge"
            _knowledge_dir.mkdir(parents=True, exist_ok=True)
            (_knowledge_dir / f"{feature_id}.md").write_text(_brief)
        except Exception as e:
            _log_error(state_dir, f"write_brief:{feature_id}", e)

        state_mgr.update_state(
            phase="generator",
            current_feature_id=feature_id,
            iteration=iteration,
            generator_sessions=current_state.get("generator_sessions", 0) + 1,
            total_cost_usd=cost_tracker.total,
            cost_breakdown=cost_tracker.to_dict(),
            last_updated=datetime.now(timezone.utc).isoformat(),
        )

        # Sync agent-marked statuses from feature_list.json back into work_plan
        if work_plan is not None:
            ingested = work_plan.ingest_feature_list(
                state_dir / "feature_list.json", work_plan_path)
            if ingested:
                print(f"  Synced {len(ingested)} agent-marked task(s) into work_plan")
                for t in ingested:
                    if t["status"] == "done":
                        tracker.feature_pass(t["id"], t["description"], 0)
                    elif t["status"] == "blocked":
                        tracker.feature_fail(t["id"], t["description"], "agent_blocked")

        # Assess progress
        if work_plan is not None:
            tc = work_plan.count_tasks()
            feature_counts = {
                "total": tc["total"],
                "passing": tc["done"],
                "blocked": tc["blocked"],
                "remaining": tc["pending"],
            }
        else:
            feature_counts = state_mgr.count_features()
        progress = assess_progress(
            gen_result["output"],
            project_dir=project_dir,
            current_feature_counts=feature_counts,
            previous_feature_counts=prev_feature_counts,
            peak_output_length=cb._data.get("peak_output_length", 0),
        )
        prev_feature_counts = feature_counts

        # Feed circuit breaker
        error_text = extract_error_from_output(gen_result["output"])
        err_hash = hash_error(error_text) if error_text else None
        cb_state_before = cb.state
        cb.record_iteration(
            progress=progress.has_progress,
            error_hash=err_hash,
            output_length=len(gen_result["output"]),
            iteration=iteration,
        )
        # Feature 041: use tracker for circuit breaker events
        if cb_state_before != CircuitBreaker.OPEN and cb.state == CircuitBreaker.OPEN:
            tracker.cb_open(cb._data.get("open_reason", "stagnation"))
        elif cb_state_before == CircuitBreaker.OPEN and cb.state == CircuitBreaker.CLOSED:
            tracker.cb_close()

        # Record exit signal if complete
        if progress.exit_signal:
            state_mgr.record_exit_signal("completion", iteration)

        print(f"  Generator: cost=${gen_result['cost']:.2f}, "
              f"progress={progress.has_progress}, "
              f"features={feature_counts['passing']}/{feature_counts['total']}")
        gen_usage = gen_result.get("usage") or {}
        _write_run_log(state_dir, "generator_iteration",
                       iteration=iteration, feature_id=feature_id,
                       cost_usd=gen_result["cost"],
                       input_tokens=gen_usage.get("input_tokens", 0),
                       output_tokens=gen_usage.get("output_tokens", 0),
                       duration_ms=gen_result.get("duration_ms", 0),
                       num_turns=gen_result.get("num_turns", 0),
                       progress=progress.has_progress,
                       passing=feature_counts["passing"],
                       total=feature_counts["total"])

        # --- EVALUATOR SESSION ---
        # Skip evaluator if generator errored (rate limit, timeout, etc.)
        # to prevent marking features done based on stale state
        if gen_result.get("status") == "error":
            print(f"  Generator returned error — skipping evaluator: {gen_result.get('error', 'unknown')}")
            tracker.feature_fail(feature_id, feature_desc, gen_result.get("error", "generator_error"))
            _write_run_log(state_dir, "generator_error",
                           iteration=iteration, feature_id=feature_id,
                           error=gen_result.get("error", "unknown"))
        elif progress.has_progress and feature_counts["remaining"] >= 0:
            _retry = current_state.get("evaluator_retries_current_feature", 0) + 1
            eval_repl, eval_file, eval_fallback, eval_user_msg = _build_eval_replacements(
                feature_id, state_dir, config,
                test_command or "echo 'No test command configured'",
                work_plan=work_plan, retry_count=_retry)
            _eval_path = prompts_dir / eval_file if (prompts_dir / eval_file).exists() else prompts_dir / eval_fallback
            eval_system = load_prompt(_eval_path, eval_repl)
            eval_options = create_client_options(project_dir, config)

            eval_result = await run_agent_session(eval_user_msg, eval_options, project_dir, system_prompt=eval_system)
            cost_tracker.record("evaluator", eval_result["cost"],
                                phase="evaluator-seq", **_metrics_from(eval_result))
            cost_tracker.record_feature(feature_id, "evaluator", eval_result["cost"])

            state_mgr.update_state(
                evaluator_sessions=current_state.get("evaluator_sessions", 0) + 1,
                total_cost_usd=cost_tracker.total,
                cost_breakdown=cost_tracker.to_dict(),
            )

            # Check verdict
            if "VERDICT: FAIL" in eval_result["output"]:
                max_retries = config.get("generator", {}).get("max_retries_per_feature", 3)
                if work_plan is not None:
                    retries = work_plan.increment_task_attempts(feature_id, work_plan_path)
                    if retries >= max_retries:
                        work_plan.mark_task_blocked(feature_id, "max_retries_exceeded", work_plan_path)
                        state_mgr.clear_feedback()
                        print(f"  Evaluator: FAIL ({retries}/{max_retries}) — BLOCKED")
                    else:
                        print(f"  Evaluator: FAIL ({retries}/{max_retries}) — feedback written")
                    work_plan.sync_feature_list(state_dir / "feature_list.json")
                    state_mgr.update_state(evaluator_retries_current_feature=retries)
                else:
                    retries = state_mgr.increment_retries(feature_id)
                    if retries >= max_retries:
                        state_mgr.mark_feature_blocked(feature_id, "max_retries_exceeded")
                        state_mgr.clear_feedback()
                        print(f"  Evaluator: FAIL ({retries}/{max_retries}) — BLOCKED")
                    else:
                        print(f"  Evaluator: FAIL ({retries}/{max_retries}) — feedback written")
                    state_mgr.update_state(evaluator_retries_current_feature=retries)
                tracker.feature_fail(feature_id, feature_desc, "VERDICT: FAIL")
                tracker.feature_retry(feature_id, feature_desc, current_state.get("evaluator_retries_current_feature", 0) + 1)
            else:
                state_mgr.clear_feedback()
                state_mgr.update_state(evaluator_retries_current_feature=0)
                print(f"  Evaluator: PASS — cost=${eval_result['cost']:.2f}")
                if work_plan is not None:
                    work_plan.mark_task_done(feature_id, work_plan_path)
                    work_plan.sync_feature_list(state_dir / "feature_list.json")
                duration_ms = int((time.time() - feature_start_time) * 1000)
                tracker.feature_pass(feature_id, feature_desc, duration_ms)
                # Browser check — informational only, does not affect feature pass/fail
                _perform_browser_check(feature_desc, eval_result["output"])

        # Delay before next iteration
        await asyncio.sleep(config.get("generator", {}).get("auto_continue_delay", AUTO_CONTINUE_DELAY))

    # === FINAL SUMMARY ===
    if work_plan is not None:
        tc = work_plan.count_tasks()
        final_counts = {
            "total": tc["total"],
            "passing": tc["done"],
            "blocked": tc["blocked"],
            "remaining": tc["pending"],
        }
    else:
        final_counts = state_mgr.count_features()
    elapsed_total = time.time() - start_time
    elapsed_min = int(elapsed_total / 60)

    _write_run_log(state_dir, "run_complete",
                   duration_min=elapsed_min, iterations=iteration,
                   passing=final_counts["passing"],
                   total=final_counts["total"],
                   blocked=final_counts["blocked"],
                   cost_usd=cost_tracker.total,
                   input_tokens=cost_tracker.input_tokens,
                   output_tokens=cost_tracker.output_tokens,
                   total_turns=cost_tracker.total_turns,
                   api_time_ms=cost_tracker.total_api_ms)

    print()
    print("=" * 60)
    print("  HARNESS COMPLETE")
    print("=" * 60)
    print(f"Duration: {elapsed_min}m")
    print(f"Iterations: {iteration}")
    print(f"Features: {final_counts['passing']}/{final_counts['total']} passing, "
          f"{final_counts['blocked']} blocked")
    print(cost_tracker.format_summary())
    top_features = cost_tracker.most_expensive_features(5)
    if top_features:
        print("Top 5 most expensive features:")
        for fid, cost in top_features:
            print(f"  Feature {fid}: ${cost:.4f}")
    print(f"Circuit breaker: {cb.total_opens} opens")

    # Feature 041: suspicion check via tracker
    suspicion_result = _perform_suspicion_check(state_mgr, config, cb.total_opens, elapsed_total, final_counts.get("remaining", 1))
    if suspicion_result and suspicion_result.get("suspicious"):
        tracker.suspicion_warning(suspicion_result.get("reasons", []))

    print(f"Resume: python .harness/run.py --resume --run-id {active_run_id}")
    print()

    # Feature 041: use tracker for completion events
    retro_path = project_dir / "retrospective.json"
    if final_counts["remaining"] == 0:
        tracker.run_complete(final_counts["passing"], cost_tracker.total,
                             input_tokens=cost_tracker.input_tokens,
                             output_tokens=cost_tracker.output_tokens,
                             total_turns=cost_tracker.total_turns,
                             api_time_ms=cost_tracker.total_api_ms)
        if retro_path.exists():
            tracker.retro_generated()
            try:
                cp.ingest_retro(str(retro_path))
            except Exception:
                pass
    else:
        tracker.run_failed()

    # Sweep merged worktrees at run end (feature 040)
    removed_end = []
    try:
        removed_end_list = sweep_merged_worktrees(project_dir)
        removed_end = removed_end_list or []
        if removed_end:
            print(f"  Worktree sweep (end): removed {len(removed_end)} merged worktree(s)")
    except Exception as e:
        print(f"  Worktree sweep (end) failed (non-fatal): {e}")
    tracker.worktree_sweep("end", len(removed_end))

    # Update run registry with final status
    run_status = "complete" if final_counts["remaining"] == 0 else "in_progress"
    registry.update_run(
        active_run_id,
        status=run_status,
        total_cost_usd=cost_tracker.total,
        features_total=final_counts["total"],
        features_passing=final_counts["passing"],
    )

    return {
        "run_id": active_run_id,
        "duration_minutes": elapsed_min,
        "iterations": iteration,
        "features": final_counts,
        "cost": cost_tracker.to_dict(),
        "circuit_breaker_opens": cb.total_opens,
    }


def _perform_suspicion_check(state_mgr, config: dict, cb_opens: int, elapsed_total: float, remaining: int) -> Optional[dict]:
    """Run check_suspicion when all features are done — informational only, never affects exit.

    Returns the suspicion result dict (or None if remaining > 0).
    """
    if remaining != 0:
        return None
    suspicion = check_suspicion(state_mgr, config, cb_opens, elapsed_total)
    if suspicion["suspicious"]:
        print()
        print("  WARNING: Suspicious completion detected:")
        for reason in suspicion["reasons"]:
            print(f"    - {reason}")
    return suspicion


def _perform_browser_check(feature_desc: str, evaluator_output: str) -> None:
    """Run check_evaluator_used_browser after evaluator PASS — informational only, never fails feature."""
    browser_check = check_evaluator_used_browser(feature_desc, evaluator_output)
    if browser_check["warning"] is not None:
        print(f"  WARNING: {browser_check['warning']}")


def _perform_sweep(project_dir: Path, config: dict, phase: str) -> None:
    """Run worktree sweep for the given phase ("start" or "end").

    Non-fatal: logs any exception and continues.
    """
    worktree_config = config.get("worktree", {})
    sweep_key = f"sweep_on_{phase}"
    if not worktree_config.get(sweep_key, True):
        return
    try:
        removed = sweep_merged_worktrees(project_dir)
        if removed:
            print(f"  Worktree sweep ({phase}): removed {len(removed)} merged worktree(s)")
    except Exception as e:
        print(f"  Worktree sweep ({phase}) failed (non-fatal): {e}")


def ensure_browser_tools(project_dir: Path, config: dict) -> str:
    """Ensure Playwright is available if browser verification is enabled.

    Installs @playwright/mcp if needed. This is a pre-flight check so the
    evaluator doesn't fail mid-run when it tries to use browser tools.

    Returns a result string: "ok", "skipped", or "install_failed".
    """
    import subprocess

    evaluator_config = config.get("evaluator", {})
    if evaluator_config.get("browser_verification") == "never":
        return "skipped"

    browser_tool = evaluator_config.get("browser_tool", "playwright")
    if browser_tool != "playwright":
        return "skipped"

    # Check if @playwright/mcp is available
    try:
        result = subprocess.run(
            ["npx", "@playwright/mcp", "--help"],
            capture_output=True, text=True, timeout=30,
            cwd=str(project_dir),
        )
        if result.returncode == 0:
            return "ok"  # Already available
    except Exception:
        pass

    print("  Installing @playwright/mcp for browser verification...")
    try:
        subprocess.run(
            ["npm", "install", "--save-dev", "@playwright/mcp"],
            capture_output=True, text=True, timeout=120,
            cwd=str(project_dir),
        )
        return "ok"
    except Exception as e:
        print(f"  Could not install @playwright/mcp: {e}")
        print("  Browser verification may not work. Set evaluator.browser_verification: never to disable.")
        return "install_failed"


def _build_input_section(
    prompt: Optional[str],
    spec_path: Optional[Path],
    plan_path: Optional[Path],
) -> str:
    """Build the input section to append to the architect prompt."""
    if spec_path:
        spec_content = Path(spec_path).read_text()
        return f"\n\n## INPUT: Existing Specification\n\n{spec_content}"
    elif plan_path:
        plan_content = Path(plan_path).read_text()
        return f"\n\n## INPUT: Existing Plan\n\n{plan_content}"
    elif prompt:
        return f"\n\n## INPUT: Project Description\n\n{prompt}"
    return ""


def validate_planner_output(state_dir: Path, use_work_plan: bool = False) -> dict:
    """Validate that the planner produced required output files.

    When use_work_plan=True (strategy pipeline mode):
    1. work_plan.json exists with a "phases" key
    2. spec_gaps.json or SPEC_GAPS.md exists

    When use_work_plan=False (legacy mode):
    1. feature_list.json exists and is non-empty
    2. spec_gaps.json or SPEC_GAPS.md exists

    Returns {valid: bool, reason: str}.
    """
    import json as _json

    state_dir = Path(state_dir)
    # Check for spec gaps — v3 pipeline uses spec_gaps.json, legacy uses SPEC_GAPS.md
    spec_gaps_json = state_dir / "spec_gaps.json"
    spec_gaps_md = state_dir / "SPEC_GAPS.md"

    if use_work_plan:
        work_plan_path = state_dir / "work_plan.json"
        if not work_plan_path.exists():
            return {"valid": False, "reason": "Planner did not write work_plan.json"}
        try:
            data = _json.loads(work_plan_path.read_text())
            if "phases" not in data:
                return {"valid": False, "reason": "work_plan.json missing 'phases' key"}
        except (_json.JSONDecodeError, Exception) as e:
            return {"valid": False, "reason": f"work_plan.json is invalid JSON: {e}"}
    else:
        feature_list_path = state_dir / "feature_list.json"
        if not feature_list_path.exists():
            return {"valid": False, "reason": "Planner did not write feature_list.json"}
        try:
            features = _json.loads(feature_list_path.read_text())
            if not features or (isinstance(features, list) and len(features) == 0):
                return {"valid": False, "reason": "feature_list.json is empty — planner produced no features"}
        except (_json.JSONDecodeError, Exception) as e:
            return {"valid": False, "reason": f"feature_list.json is invalid JSON: {e}"}

    if not spec_gaps_json.exists() and not spec_gaps_md.exists():
        return {"valid": False, "reason": "Planner skipped spec review — no spec_gaps.json or SPEC_GAPS.md found. Re-running planner."}

    if spec_gaps_md.exists() and not spec_gaps_json.exists():
        gaps_content = spec_gaps_md.read_text().strip()
        if len(gaps_content) < 50:
            return {"valid": False, "reason": f"SPEC_GAPS.md too short ({len(gaps_content)} chars) — planner likely skipped review"}

    return {"valid": True, "reason": "Planner output validated"}


def build_planner_prompt(
    prompts_dir: Path,
    prompt: Optional[str],
    spec_path: Optional[Path],
    plan_path: Optional[Path],
    state_dir: Path,
    config: Optional[dict] = None,
) -> str:
    """Build the planner prompt based on input mode.

    Queries the knowledge DB for past learnings and appends them
    to the planner context so it can guard against known failure modes.
    Uses delegation mode to select the appropriate architect prompt.
    """
    architect_prompt = resolve_prompt("architect", config or {})
    prompt_file = prompts_dir / architect_prompt
    # Fall back to planner.md if the delegation-mode prompt doesn't exist
    if not prompt_file.exists():
        prompt_file = prompts_dir / "planner.md"
    base = load_prompt(prompt_file, {"STATE_DIR": str(state_dir)})

    # Inject knowledge from past runs
    knowledge_section = _query_knowledge_for_planner()
    if knowledge_section:
        base = base + "\n" + knowledge_section

    if spec_path:
        spec_content = Path(spec_path).read_text()
        return f"{base}\n\n## INPUT: Existing Specification\n\n{spec_content}"
    elif plan_path:
        plan_content = Path(plan_path).read_text()
        return f"{base}\n\n## INPUT: Existing Plan\n\nConvert this plan into a feature_list.json:\n\n{plan_content}"
    elif prompt:
        return f"{base}\n\n## INPUT: Project Description\n\n{prompt}"
    else:
        raise ValueError("One of --prompt, --spec, or --plan is required")


UI_KEYWORDS = [
    "dashboard",
    "ui",
    "frontend",
    "browser",
    "render",
    "navigate",
]

BROWSER_INDICATORS = [
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "mcp__playwright__",
    "screenshot",
]


def check_evaluator_used_browser(feature_desc: str, evaluator_output: str) -> dict:
    """Check whether an evaluator used browser tools to verify a UI feature.

    Returns a dict with keys:
        ui_feature (bool): True if the feature description mentions UI/browser keywords.
        browser_used (bool): True if browser tools were detected in evaluator output.
        warning (str|None): Warning message if UI feature was verified without browser, else None.
    """
    desc_lower = feature_desc.lower()
    ui_feature = any(kw in desc_lower for kw in UI_KEYWORDS)

    if not ui_feature:
        return {"ui_feature": False, "browser_used": False, "warning": None}

    output_lower = evaluator_output.lower()
    browser_used = any(ind in output_lower for ind in BROWSER_INDICATORS)

    warning = None if browser_used else "Evaluator verified UI feature without browser verification"

    return {"ui_feature": True, "browser_used": browser_used, "warning": warning}


def _query_knowledge_for_planner() -> str:
    """Query the control plane knowledge DB for past learnings.

    Returns formatted markdown string, or empty string if unavailable.
    """
    from .knowledge_client import format_knowledge_for_planner

    try:
        cp = ControlPlaneClient()
        chunks = cp.get_all_chunks()
        return format_knowledge_for_planner(chunks)
    except Exception:
        return ""
