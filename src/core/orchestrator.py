"""
Orchestrator — 3-Agent Loop
=============================

Planner → Generator ↔ Evaluator cycle with circuit breaker,
cost tracking, progress detection, and resume support.
"""

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .state import StateManager
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
from .parallel import group_by_dependency, create_worktree, merge_worktree, cleanup_worktree
from .coordination import (
    generate_instance_id, register_instance, unregister_instance,
    claim_scope, sweep_stale_instances, get_swept_feature_ids, ScopeOverlapError,
)
from .discovery import parse_handoff, compress_discovery, write_brief, get_relay_context
from . import fleet_session


# Delay between sessions
AUTO_CONTINUE_DELAY = 3


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


async def run_agent_session(
    prompt: str,
    options: dict,
    project_dir: Path,
) -> dict:
    """Run a single agent session using Claude Agent SDK.

    Falls back to `claude -p` CLI if no ANTHROPIC_API_KEY is set,
    allowing the harness to run on a Claude subscription without a key.

    Returns {status, output, cost, usage, session_id}.
    """
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return await run_agent_session_cli(prompt, project_dir, options)

    try:
        from claude_code_sdk import ClaudeCodeOptions, ClaudeSDKClient
        from claude_code_sdk.types import HookMatcher
    except ImportError:
        return await run_agent_session_cli(prompt, project_dir, options)

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

    try:
        async with client:
            await client.query(prompt)
            async for msg in client.receive_response():
                msg_type = type(msg).__name__

                if msg_type == "AssistantMessage" and hasattr(msg, "content"):
                    for block in msg.content:
                        if type(block).__name__ == "TextBlock" and hasattr(block, "text"):
                            output_text += block.text
                        elif type(block).__name__ == "ToolUseBlock" and hasattr(block, "name"):
                            print(f"  [Tool: {block.name}]", flush=True)

                elif msg_type == "ResultMessage":
                    cost_usd = getattr(msg, "total_cost_usd", 0) or 0
                    usage = getattr(msg, "usage", {}) or {}
                    session_id = getattr(msg, "session_id", None)
                    # Check for error subtypes
                    subtype = getattr(msg, "subtype", "success")
                    if subtype and subtype != "success":
                        return {
                            "status": "error",
                            "output": output_text,
                            "cost": cost_usd,
                            "usage": usage,
                            "session_id": session_id,
                            "error": f"Session ended with: {subtype}",
                        }

                # Silently skip: SystemMessage, UserMessage, StreamEvent, rate_limit_event, etc.

    except Exception as e:
        error_msg = str(e)
        # Rate limiting — wait and retry
        if "rate_limit" in error_msg.lower() or "429" in error_msg:
            print(f"  Rate limited. Waiting 60s...")
            await asyncio.sleep(60)
            return {
                "status": "error",
                "output": output_text,
                "cost": cost_usd,
                "usage": {},
                "session_id": None,
                "error": f"Rate limited: {error_msg}",
            }
        raise

    return {
        "status": "continue",
        "output": output_text,
        "cost": cost_usd,
        "usage": usage,
        "session_id": session_id,
    }


async def run_agent_session_cli(
    prompt: str,
    project_dir: Path,
    options: Optional[dict] = None,
) -> dict:
    """Run via `claude -p` CLI (subscription mode, no API key needed)."""
    import subprocess

    options = options or {}
    model = options.get("model", "claude-sonnet-4-6")

    cmd = [
        "claude", "-p", prompt,
        "--output-format", "text",
        "--model", model,
        "--dangerously-skip-permissions",
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(project_dir),
        timeout=3600,  # 1 hour max per session
    )

    stderr_note = f"\n[stderr: {result.stderr[:500]}]" if result.stderr else ""
    return {
        "status": "continue" if result.returncode == 0 else "error",
        "output": result.stdout + stderr_note,
        "cost": 0.0,  # CLI doesn't report cost
        "usage": {},
        "session_id": None,
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
    """Run a single parallel wave: N generators in worktrees, merge, evaluate.

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

    # Handle sequential features (no scope) one at a time
    for feat in sequential_features:
        failed.append(feat["id"])  # Mark as needing sequential processing

    if not parallel_features:
        return {"completed": completed, "failed": failed, "conflicts": conflicts}

    # Sweep stale instances before starting
    swept = sweep_stale_instances(state_dir, parallel_config.get("stale_instance_hours", 2.0))
    if swept:
        swept_ids = get_swept_feature_ids(swept)
        for fid in swept_ids:
            fleet_session.requeue_feature(session, fid, reason="stale_instance")
        print(f"  Swept {len(swept)} stale instance(s), requeued {len(swept_ids)} feature(s)")

    # Batch features into groups of max_workers
    batches = [parallel_features[i:i + max_workers] for i in range(0, len(parallel_features), max_workers)]

    for batch_idx, batch in enumerate(batches):
        agent_ids = []
        worktrees = []  # (agent_id, feature, worktree_dir, branch_name)

        # Claim scope and create worktrees
        for feature in batch:
            agent_id = generate_instance_id()
            scope = feature.get("scope", [])
            feature_id = feature["id"]

            try:
                claim_scope(agent_id, scope, feature_id, state_dir)
                register_instance(agent_id, feature_id, 0, wave_num, state_dir)
            except ScopeOverlapError as e:
                print(f"  Scope overlap for {feature_id}: {e} — requeued")
                fleet_session.requeue_feature(session, feature_id, reason="scope_overlap")
                conflicts.append(feature_id)
                continue

            try:
                wt_dir, branch = create_worktree(project_dir, hash(agent_id) % 10000)
                worktrees.append((agent_id, feature, wt_dir, branch))
                agent_ids.append(agent_id)
            except Exception as e:
                print(f"  Worktree creation failed for {feature_id}: {e}")
                unregister_instance(agent_id, state_dir)
                failed.append(feature_id)

        if not worktrees:
            continue

        # Emit wave start
        feature_ids = [f["id"] for _, f, _, _ in worktrees]
        tracker.wave_start(wave_num, len(worktrees), feature_ids)
        fleet_session.start_wave(session, wave_num, agent_ids)

        # Build prompts with discovery relay
        relay_context = ""
        if relay_enabled:
            relay_context = get_relay_context(state_dir, wave_num)

        test_command = config.get("evaluator", {}).get("test_suite_command", "echo 'No test command'")

        # Launch generators in parallel
        async def _run_one(agent_id, feature, wt_dir):
            gen_replacements = {
                "STATE_DIR": str(state_dir),
                "TEST_COMMAND": test_command,
                "IF_WEB_PROJECT": config.get("evaluator", {}).get("browser_verification") != "never",
            }
            gen_prompt = load_prompt(prompts_dir / "generator.md", gen_replacements)
            if relay_context:
                gen_prompt = relay_context + "\n\n" + gen_prompt
            gen_options = create_client_options(wt_dir, config)
            return await run_agent_session(gen_prompt, gen_options, wt_dir)

        tasks = []
        for agent_id, feature, wt_dir, branch in worktrees:
            coro = _run_one(agent_id, feature, wt_dir)
            wrapped = asyncio.wait_for(coro, timeout=timeout_minutes * 60)
            tasks.append((agent_id, feature, wt_dir, branch, asyncio.ensure_future(wrapped)))

        # Gather results
        results = []
        for agent_id, feature, wt_dir, branch, task in tasks:
            feature_id = feature["id"]
            try:
                result = await task
                results.append((agent_id, feature, wt_dir, branch, result, "ok"))
            except asyncio.TimeoutError:
                print(f"  Agent {agent_id} timed out on {feature_id}")
                tracker.agent_timeout(agent_id, feature_id, timeout_minutes * 60)
                results.append((agent_id, feature, wt_dir, branch, {"output": "", "cost": 0}, "timeout"))
            except Exception as e:
                print(f"  Agent {agent_id} failed on {feature_id}: {e}")
                results.append((agent_id, feature, wt_dir, branch, {"output": "", "cost": 0}, "error"))

        # Check for all-failed escalation
        statuses = [s for _, _, _, _, _, s in results]
        if all(s != "ok" for s in statuses):
            tracker.wave_all_failed(wave_num, [f"{f['id']}:{s}" for _, f, _, _, _, s in results])
            print(f"  WAVE {wave_num}: ALL agents failed — falling back to sequential")
            for agent_id, feature, wt_dir, branch, _, _ in results:
                cleanup_worktree(project_dir, wt_dir, branch)
                unregister_instance(agent_id, state_dir)
                failed.append(feature["id"])
            fleet_session.complete_wave(session, wave_num)
            continue

        # Merge results — sorted by feature ID for deterministic order
        results.sort(key=lambda r: r[1]["id"])

        for agent_id, feature, wt_dir, branch, result, status in results:
            feature_id = feature["id"]
            cost_tracker.record("generator", result.get("cost", 0))
            cost_tracker.record_feature(feature_id, "generator", result.get("cost", 0))

            # Compress discovery brief
            handoff_data = parse_handoff(result.get("output", ""))
            brief = compress_discovery(result.get("output", ""), agent_id, feature_id, status)
            write_brief(brief, wave_num, agent_id, state_dir)

            if status != "ok":
                fleet_session.requeue_feature(session, feature_id, reason=status)
                failed.append(feature_id)
                cleanup_worktree(project_dir, wt_dir, branch)
                unregister_instance(agent_id, state_dir)
                fleet_session.record_agent_result(session, wave_num, agent_id, feature_id, status)
                continue

            # Merge worktree
            merge_result = merge_worktree(project_dir, branch)
            cleanup_worktree(project_dir, wt_dir, branch)
            unregister_instance(agent_id, state_dir)

            if not merge_result["success"]:
                if merge_result.get("conflict"):
                    tracker.merge_conflict(agent_id, feature_id, merge_result.get("error", ""))
                    fleet_session.requeue_feature(session, feature_id, reason="conflict")
                    conflicts.append(feature_id)
                    fleet_session.record_agent_result(session, wave_num, agent_id, feature_id, "conflict")
                else:
                    failed.append(feature_id)
                    fleet_session.record_agent_result(session, wave_num, agent_id, feature_id, "merge_error")
                continue

            # Merged successfully — run evaluator sequentially
            print(f"  Merged {feature_id} — running evaluator")
            eval_replacements = {
                "STATE_DIR": str(state_dir),
                "TEST_COMMAND": test_command,
                "IF_WEB_PROJECT": config.get("evaluator", {}).get("browser_verification") != "never",
                "FEATURE_ID": feature_id,
                "RETRY_COUNT": "1",
                "MAX_RETRIES": str(config.get("generator", {}).get("max_retries_per_feature", 3)),
            }
            eval_prompt = load_prompt(prompts_dir / "evaluator.md", eval_replacements)
            eval_options = create_client_options(
                project_dir, config,
                system_prompt="You are a skeptical QA evaluator. Find problems. Do not approve mediocre work.",
            )
            eval_result = await run_agent_session(eval_prompt, eval_options, project_dir)
            cost_tracker.record("evaluator", eval_result.get("cost", 0))
            cost_tracker.record_feature(feature_id, "evaluator", eval_result.get("cost", 0))

            if "VERDICT: FAIL" in eval_result.get("output", ""):
                fleet_session.requeue_feature(session, feature_id, reason="eval_fail")
                failed.append(feature_id)
                tracker.feature_fail(feature_id, feature.get("description", ""), "VERDICT: FAIL")
                fleet_session.record_agent_result(session, wave_num, agent_id, feature_id, "eval_fail")
            else:
                await state_mgr.mark_feature_passing_async(feature_id)
                if work_plan is not None:
                    work_plan.mark_task_done(feature_id, work_plan_path)
                    work_plan.sync_feature_list(state_dir / "feature_list.json")
                fleet_session.mark_feature_complete(session, feature_id)
                completed.append(feature_id)
                tracker.feature_pass(feature_id, feature.get("description", ""), 0)
                fleet_session.record_agent_result(session, wave_num, agent_id, feature_id, "complete")

            # Add discoveries
            if handoff_data.get("found") and handoff_data.get("items"):
                for item in handoff_data["items"][:3]:
                    fleet_session.add_discovery(session, wave_num, f"Wave {wave_num} {feature_id}: {item}")

        # Complete wave
        fleet_session.complete_wave(session, wave_num)
        n_ok = len([s for _, _, _, _, _, s in results if s == "ok"])
        n_fail = len(results) - n_ok
        tracker.wave_complete(wave_num, len(completed), n_fail, len(conflicts))

    return {"completed": completed, "failed": failed, "conflicts": conflicts}


async def run_harness(
    config_path: Path,
    project_dir: Path,
    prompt: Optional[str] = None,
    spec_path: Optional[Path] = None,
    plan_path: Optional[Path] = None,
    resume: bool = False,
    cli_overrides: Optional[dict] = None,
) -> dict:
    """Main harness entry point.

    Runs the full planner → generator ↔ evaluator loop.
    Returns summary dict.
    """
    config = load_config(config_path)
    if cli_overrides:
        config.update(cli_overrides)

    state_dir = project_dir / ".harness" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    state_mgr = StateManager(state_dir)
    cb = CircuitBreaker(state_dir, config.get("circuit_breaker", {}))
    cost_tracker = CostTracker()
    prompts_dir = Path(__file__).parent.parent / "prompts"

    cp = ControlPlaneClient()
    try:
        cp.create_run(project_dir.name, str(project_dir))
    except Exception:
        pass

    # Create EventTracker for full lifecycle observability (feature 040)
    tracker = EventTracker(cp)

    start_time = time.time()
    state = state_mgr.load_state()

    # Resume from previous run
    if resume and state["phase"] != "init":
        cost_tracker = CostTracker.from_dict(state.get("cost_breakdown", {}))
        print(f"Resuming from phase: {state['phase']}, iteration: {state['iteration']}")
    else:
        state = state_mgr.update_state(
            started_at=datetime.now(timezone.utc).isoformat(),
            phase="init",
        )

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

    # Determine strategy (feature 038)
    default_strategy = config.get("default_strategy")
    strategies = config.get("strategies", {})
    use_pipeline = bool(default_strategy and strategies.get(default_strategy))

    # === PHASE 1: PLANNER ===
    if not state.get("planner_complete") and not resume:
        print("--- PHASE 1: PLANNER ---")

        if use_pipeline:
            # Feature 038: multi-phase pipeline
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
                prompts_dir, prompt, spec_path, plan_path, state_dir
            )
            planner_options = create_client_options(
                project_dir,
                config,
                model_override=config.get("planner_model"),
                system_prompt="You are a product architect designing a comprehensive application specification.",
            )
            result = await run_agent_session(planner_prompt, planner_options, project_dir)
            cost_tracker.record("planner", result["cost"])
            print(f"Planner complete. Cost: ${result['cost']:.2f}")
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
                cost_tracker.record("planner", result2["cost"])
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
    if parallel_config.get("enabled") and work_plan is not None:
        print("--- PARALLEL MODE: Fleet wave execution ---")
        flat_tasks = work_plan.flatten_for_grouping()
        layers = group_by_dependency(flat_tasks)
        print(f"  {len(flat_tasks)} tasks → {len(layers)} dependency layer(s)")

        if len(layers) > 0:
            session = fleet_session.init_session(state_dir, len(layers))

            for wave_num, layer in enumerate(layers, start=1):
                # Add features to session queue
                for feat in layer:
                    fleet_session.add_to_queue(
                        session, feat["id"], feat.get("description", ""),
                        feat.get("scope", []), wave_num,
                    )

                if len([f for f in layer if f.get("scope")]) < 2:
                    # Not enough scoped features for parallelism — skip to sequential
                    print(f"  Wave {wave_num}: <2 scoped features, deferring to sequential loop")
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

        # --- GENERATOR SESSION ---
        gen_replacements = {
            "STATE_DIR": str(state_dir),
            "TEST_COMMAND": test_command or "echo 'No test command configured'",
            "IF_WEB_PROJECT": config.get("evaluator", {}).get("browser_verification") != "never",
        }
        gen_prompt = load_prompt(prompts_dir / "generator.md", gen_replacements)
        gen_options = create_client_options(project_dir, config)

        gen_result = await run_agent_session(gen_prompt, gen_options, project_dir)
        cost_tracker.record("generator", gen_result["cost"])
        cost_tracker.record_feature(feature_id, "generator", gen_result["cost"])

        state_mgr.update_state(
            phase="generator",
            current_feature_id=feature_id,
            iteration=iteration,
            generator_sessions=current_state.get("generator_sessions", 0) + 1,
            total_cost_usd=cost_tracker.total,
            cost_breakdown=cost_tracker.to_dict(),
            last_updated=datetime.now(timezone.utc).isoformat(),
        )

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

        # --- EVALUATOR SESSION ---
        if progress.has_progress and feature_counts["remaining"] >= 0:
            eval_replacements = {
                "STATE_DIR": str(state_dir),
                "TEST_COMMAND": test_command or "echo 'No test command configured'",
                "IF_WEB_PROJECT": config.get("evaluator", {}).get("browser_verification") != "never",
                "FEATURE_ID": feature_id,
                "RETRY_COUNT": str(current_state.get("evaluator_retries_current_feature", 0) + 1),
                "MAX_RETRIES": str(config.get("generator", {}).get("max_retries_per_feature", 3)),
            }
            eval_prompt = load_prompt(prompts_dir / "evaluator.md", eval_replacements)
            eval_options = create_client_options(
                project_dir,
                config,
                system_prompt="You are a skeptical QA evaluator. Find problems. Do not approve mediocre work.",
            )

            eval_result = await run_agent_session(eval_prompt, eval_options, project_dir)
            cost_tracker.record("evaluator", eval_result["cost"])
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

    print(f"Resume: python .harness/run.py --resume")
    print()

    # Feature 041: use tracker for completion events
    retro_path = project_dir / "retrospective.json"
    if final_counts["remaining"] == 0:
        tracker.run_complete(final_counts["passing"], cost_tracker.total)
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

    return {
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
) -> str:
    """Build the planner prompt based on input mode.

    Queries the knowledge DB for past learnings and appends them
    to the planner context so it can guard against known failure modes.
    """
    base = load_prompt(prompts_dir / "planner.md", {"STATE_DIR": str(state_dir)})

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
