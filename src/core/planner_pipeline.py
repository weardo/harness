"""
Planner Pipeline — Multi-Phase Planner with Validation Gates
=============================================================

Replaces the monolithic single-session planner with a 4-role pipeline:
  Architect → Adversary → Refiner → Validator

Each role runs as a separate agent session via run_agent_session().
Between roles, artifacts are validated against schema + quality constraints.
If validation fails, the role is retried once with a feedback prompt.

Resume support: completed roles are tracked in state.json so a crash
mid-pipeline resumes from the last incomplete role.

Event IDs use "planner-{role}" prefix for dashboard display.
"""

import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .state import atomic_write, atomic_read
from .client import create_client_options, load_config


def _write_planner_log(state_dir: Path, event: str, **data) -> None:
    """Append a timestamped entry to logs/planner.jsonl."""
    log_dir = state_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **data}
    with open(log_dir / "planner.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


def validate_json_array(
    path: Path,
    min_items: int = 1,
    required_fields: Optional[list] = None,
) -> dict:
    """Validate a JSON array file for structure and completeness.

    Returns:
        {"valid": True} or {"valid": False, "reason": "<description>"}
    """
    if required_fields is None:
        required_fields = []

    if not path.exists():
        return {"valid": False, "reason": f"File not found: {path}"}

    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
    except json.JSONDecodeError as e:
        return {"valid": False, "reason": f"Invalid JSON: {e}"}

    if not isinstance(data, list):
        return {"valid": False, "reason": f"Expected JSON array, got {type(data).__name__}"}

    if len(data) < min_items:
        return {
            "valid": False,
            "reason": f"Array has {len(data)} items, min {min_items} required",
        }

    for field in required_fields:
        missing = [
            i for i, item in enumerate(data)
            if not isinstance(item, dict) or field not in item
        ]
        if missing:
            return {
                "valid": False,
                "reason": f"missing field '{field}' in items at indices {missing[:5]}",
            }

    return {"valid": True}


def validate_markdown(
    path: Path,
    min_length: int = 0,
    required_headings: Optional[list] = None,
    min_gaps: int = 0,
) -> dict:
    """Validate a Markdown file for length, headings, and gap entries.

    Gap entries are lines starting with '### GAP-' or '### Gap-' or
    numbered lines like '1.' under a gap section heading.

    Returns:
        {"valid": True} or {"valid": False, "reason": "<description>"}
    """
    if required_headings is None:
        required_headings = []

    if not path.exists():
        return {"valid": False, "reason": f"File not found: {path}"}

    content = path.read_text(encoding="utf-8")

    if len(content) < min_length:
        return {
            "valid": False,
            "reason": f"File too short: {len(content)} chars, min {min_length} required",
        }

    for heading in required_headings:
        if heading not in content:
            return {
                "valid": False,
                "reason": f"Missing required heading: '{heading}'",
            }

    if min_gaps > 0:
        gap_count = sum(
            1 for line in content.splitlines()
            if line.startswith("### GAP-") or line.startswith("### Gap-")
        )
        if gap_count < min_gaps:
            return {
                "valid": False,
                "reason": f"only {gap_count} gap entr{'y' if gap_count == 1 else 'ies'} found, min {min_gaps} required",
            }

    return {"valid": True}


def validate_work_plan(
    path: Path,
    min_tasks: int = 1,
    min_phases: int = 1,
    require_acceptance_criteria: bool = True,
) -> dict:
    """Validate a hierarchical work_plan.json file.

    Returns:
        {"valid": True} or {"valid": False, "reason": "<description>"}
    """
    if not path.exists():
        return {"valid": False, "reason": f"File not found: {path}"}

    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
    except json.JSONDecodeError as e:
        return {"valid": False, "reason": f"Invalid JSON: {e}"}

    if not isinstance(data, dict) or "phases" not in data:
        return {"valid": False, "reason": "Missing 'phases' key in work_plan.json"}

    phases = data["phases"]
    if not isinstance(phases, list) or len(phases) < min_phases:
        return {
            "valid": False,
            "reason": f"Expected at least {min_phases} phase(s), got {len(phases) if isinstance(phases, list) else 0}",
        }

    all_tasks = []
    for phase in phases:
        for epic in phase.get("epics", []):
            for story in epic.get("stories", []):
                all_tasks.extend(story.get("tasks", []))

    if len(all_tasks) < min_tasks:
        return {
            "valid": False,
            "reason": f"Work plan has {len(all_tasks)} tasks, min {min_tasks} required",
        }

    if require_acceptance_criteria:
        missing = [
            t.get("id", "?")
            for t in all_tasks
            if not t.get("acceptance_criteria")
        ]
        if missing:
            return {
                "valid": False,
                "reason": f"Tasks missing acceptance_criteria: {missing[:5]}",
            }

    return {"valid": True}


def validate_spec_gaps(
    path: Path,
    min_gaps: int = 3,
    required_categories: Optional[list] = None,
) -> dict:
    """Validate spec_gaps.json — structured gap analysis from the Adversary.

    Returns:
        {"valid": True} or {"valid": False, "reason": "<description>"}
    """
    if required_categories is None:
        required_categories = []

    if not path.exists():
        return {"valid": False, "reason": f"File not found: {path}"}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"valid": False, "reason": f"Invalid JSON: {e}"}

    if not isinstance(data, dict) or "gaps" not in data:
        return {"valid": False, "reason": "Missing 'gaps' key in spec_gaps.json"}

    gaps = data["gaps"]
    if not isinstance(gaps, list):
        return {"valid": False, "reason": "'gaps' must be an array"}

    if len(gaps) < min_gaps:
        return {"valid": False, "reason": f"Only {len(gaps)} gaps found, min {min_gaps} required"}

    # Check required categories
    found_categories = {g.get("category") for g in gaps}
    for cat in required_categories:
        if cat not in found_categories:
            return {"valid": False, "reason": f"Missing required category: '{cat}'"}

    # Check each gap has required fields
    for i, gap in enumerate(gaps):
        for field in ["id", "category", "risk", "title", "what_breaks", "fix"]:
            if field not in gap:
                return {"valid": False, "reason": f"Gap {i} missing required field: '{field}'"}

    return {"valid": True}


def validate_validation(
    path: Path,
    require_sign_off: bool = True,
) -> dict:
    """Validate validation.json — structured report from the Validator.

    Checks that sign_off is present and, if require_sign_off is True,
    that sign_off is True (approved). If sign_off is False, returns
    the issues so the pipeline can loop back to the Refiner.

    Returns:
        {"valid": True} or {"valid": False, "reason": "<description>", "issues": [...]}
    """
    if not path.exists():
        return {"valid": False, "reason": f"File not found: {path}"}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"valid": False, "reason": f"Invalid JSON: {e}"}

    if not isinstance(data, dict):
        return {"valid": False, "reason": "validation.json must be a JSON object"}

    if "sign_off" not in data:
        return {"valid": False, "reason": "Missing 'sign_off' field in validation.json"}

    if require_sign_off and not data["sign_off"]:
        issues = data.get("issues", [])
        issue_summaries = [f"- {iss.get('title', 'unknown')}" for iss in issues[:5]]
        return {
            "valid": False,
            "reason": f"Validator REJECTED ({len(issues)} issues):\n" + "\n".join(issue_summaries),
            "issues": issues,
        }

    return {"valid": True}


def _resolve_model(role_model: str, config: dict) -> str:
    """Resolve role model alias to actual model ID from config."""
    if role_model == "opus":
        return config.get("planner_model", "claude-opus-4-6")
    if role_model == "sonnet":
        return config.get("model", "claude-sonnet-4-6")
    return role_model  # already a concrete model ID


def _read_artifact(state_dir: Path, filename: str) -> str:
    """Read an artifact file from state_dir. Returns content or empty string."""
    path = Path(state_dir) / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    return f"[{filename} not found]"


def _build_role_context(
    role: dict,
    state_dir: Path,
    knowledge_section: str,
    input_section: str,
) -> str:
    """Assemble the context appended after the role's base prompt.

    FM-15 no context bleed: Adversary and later roles do NOT receive
    knowledge_section or input_section.
    """
    role_name = role.get("name", "")

    if role_name == "architect":
        sections = []
        if knowledge_section:
            sections.append(knowledge_section)
        if input_section:
            sections.append(input_section)
        return "\n\n".join(sections)

    elif role_name == "adversary":
        spec = _read_artifact(state_dir, "spec.md")
        draft = _read_artifact(state_dir, "draft_work_plan.json")
        return f"\n\n## spec.md\n{spec}\n\n## draft_work_plan.json\n{draft}"

    elif role_name == "refiner":
        spec = _read_artifact(state_dir, "spec.md")
        draft = _read_artifact(state_dir, "draft_work_plan.json")
        gaps = _read_artifact(state_dir, "spec_gaps.json")
        return (
            f"\n\n## spec.md\n{spec}"
            f"\n\n## draft_work_plan.json\n{draft}"
            f"\n\n## spec_gaps.json\n{gaps}"
        )

    elif role_name == "validator":
        spec = _read_artifact(state_dir, "spec.md")
        plan = _read_artifact(state_dir, "work_plan.json")
        gaps = _read_artifact(state_dir, "spec_gaps.json")
        return (
            f"\n\n## spec.md\n{spec}"
            f"\n\n## work_plan.json\n{plan}"
            f"\n\n## spec_gaps.json\n{gaps}"
        )

    return ""


def _validate_artifact(role: dict, state_dir: Path) -> dict:
    """Run the role's validation against its artifact. Returns {valid, reason}."""
    validation = role.get("validation", {})
    if not validation:
        return {"valid": True}

    vtype = validation.get("type", "")
    artifact_name = role.get("artifact", "")
    artifact_path = Path(state_dir) / artifact_name

    if vtype == "json_array":
        return validate_json_array(
            artifact_path,
            min_items=validation.get("min_items", 1),
            required_fields=validation.get("required_fields", []),
        )
    elif vtype == "markdown":
        return validate_markdown(
            artifact_path,
            min_length=validation.get("min_length", 0),
            required_headings=validation.get("required_headings", []),
            min_gaps=validation.get("min_gaps", 0),
        )
    elif vtype == "work_plan":
        return validate_work_plan(
            artifact_path,
            min_tasks=validation.get("min_tasks", 1),
            min_phases=validation.get("min_phases", 1),
            require_acceptance_criteria=validation.get("require_acceptance_criteria", True),
        )
    elif vtype == "spec_gaps":
        return validate_spec_gaps(
            artifact_path,
            min_gaps=validation.get("min_gaps", 3),
            required_categories=validation.get("required_categories", []),
        )
    elif vtype == "validation":
        return validate_validation(
            artifact_path,
            require_sign_off=validation.get("require_sign_off", True),
        )

    return {"valid": True}


def _artifact_size(state_dir: Path, artifact: str) -> int:
    """Get size of an artifact file in bytes."""
    path = Path(state_dir) / artifact
    return path.stat().st_size if path.exists() else 0


def _is_role_complete(planner_state: dict, role_name: str) -> bool:
    """Check if a role has a 'complete' entry in the structured state."""
    for role in planner_state.get("roles", []):
        if role["name"] == role_name and role["status"] == "complete":
            return True
    return False


async def _run_refiner_fix(
    issues: list,
    roles: list,
    state_dir: Path,
    project_dir: Path,
    config: dict,
    state_mgr,
    cost_tracker,
    event_tracker=None,
    triggered_by: str = "",
) -> float:
    """Run a refiner-fix session to address validator issues. Returns cost."""
    from .orchestrator import run_agent_session  # noqa: PLC0415

    refiner_role = next((r for r in roles if r.get("name") == "refiner"), None)
    if not refiner_role:
        return 0.0

    issues_json = json.dumps(issues, indent=2)
    fix_prompt = (
        "# Fix Validator Issues in work_plan.json\n\n"
        "The Validator found issues in the work plan. Fix them by modifying work_plan.json.\n\n"
        "## Issues to fix:\n\n"
        f"```json\n{issues_json}\n```\n\n"
        "## Instructions:\n\n"
        "1. Read the current work_plan.json from the state directory\n"
        "2. Apply each fix (usually adding depends_on entries to serialize shared-file access)\n"
        "3. Write the updated work_plan.json back\n"
        "4. Do NOT change task descriptions, acceptance_criteria, or steps — only fix the issues listed\n\n"
        f"State directory: {state_dir}\n"
    )

    model = _resolve_model(refiner_role.get("model", "sonnet"), config)

    if event_tracker:
        event_tracker.planner_role_start("refiner-fix", f"Fixing {len(issues)} validator issues")

    state_mgr.start_role("refiner-fix", model=model, triggered_by=triggered_by)

    fix_options = create_client_options(project_dir, config, model_override=model)
    fix_result = await run_agent_session(
        fix_prompt, fix_options, Path(project_dir),
        progress_label="Planner/refiner-fix"
    )
    fix_cost = fix_result.get("cost", 0.0)
    cost_tracker.record("planner", fix_cost,
                        usage=fix_result.get("usage") or {},
                        duration_ms=fix_result.get("duration_ms", 0),
                        duration_api_ms=fix_result.get("duration_api_ms", 0),
                        num_turns=fix_result.get("num_turns", 0))

    state_mgr.complete_role(
        "refiner-fix",
        cost_usd=fix_cost,
        artifact="work_plan.json",
        artifact_size_bytes=_artifact_size(state_dir, "work_plan.json"),
    )
    state_mgr.increment_fix_loops()

    print(f"  refiner-fix: complete. Cost: ${fix_cost:.2f}")

    # Archive old validation for debugging history, then remove so Validator runs fresh
    validation_path = state_dir / "validation.json"
    if validation_path.exists():
        artifacts_dir = state_dir / "artifacts"
        artifacts_dir.mkdir(exist_ok=True)
        iteration = state_mgr.get_planner_state().get("fix_loops_completed", 0)
        archive_name = f"validation.iter-{iteration}.json"
        shutil.copy2(str(validation_path), str(artifacts_dir / archive_name))
        _write_planner_log(state_dir, "validation_archived",
                           iteration=iteration, archive=archive_name)
        validation_path.unlink()

    if event_tracker:
        event_tracker.planner_role_pass("refiner-fix", "work_plan.json", 0)

    return fix_cost


async def run_planner_pipeline(  # noqa: C901
    strategy_config: dict,
    prompts_dir: Path,
    project_dir: Path,
    state_dir: Path,
    state_mgr,
    cost_tracker,
    knowledge_section: str,
    input_section: str,
    event_tracker=None,
    config: Optional[dict] = None,
) -> dict:
    """Run the multi-phase planner pipeline.

    Loops through strategy_config["planner_roles"], running each role as a
    separate agent session. Validates artifacts between roles. Supports resume.

    Uses structured planner state machine for tracking. Auto-migrates legacy
    planner_roles dict format.

    Returns:
        {"success": bool, "roles_completed": list[str], "total_cost": float}
    """
    if config is None:
        config = load_config()

    roles = strategy_config.get("planner_roles", [])
    roles_completed = []
    total_cost = 0.0

    # Auto-migrate legacy planner_roles dict if present
    state_mgr.migrate_legacy_planner_state()

    # Initialize planner state machine
    ps = state_mgr.get_planner_state()
    if ps["status"] == "pending":
        state_mgr.start_planner()

    # Resume: check if we need to handle a crashed or rejected state
    resume_point = state_mgr.get_resume_point()

    if resume_point["action"] == "fix_loop":
        # Validator previously rejected — run refiner-fix before continuing
        validation_path = state_dir / "validation.json"
        if validation_path.exists():
            try:
                validation_data = json.loads(validation_path.read_text())
                issues = validation_data.get("issues", [])
                if issues:
                    validator_attempt = sum(
                        1 for r in ps["roles"] if r["name"] == "validator"
                    )
                    print(f"\n  Previous Validator rejected ({len(issues)} issues) — running Refiner-fix first...")
                    fix_cost = await _run_refiner_fix(
                        issues, roles, state_dir, project_dir, config,
                        state_mgr, cost_tracker, event_tracker,
                        triggered_by=f"validator:attempt:{validator_attempt}",
                    )
                    total_cost += fix_cost
            except Exception as e:
                print(f"  Refiner-fix pre-loop failed (non-fatal): {e}")

    for role in roles:
        role_name = role.get("name", "")
        artifact = role.get("artifact", "")

        # Resume: skip already-completed roles
        ps = state_mgr.get_planner_state()
        if _is_role_complete(ps, role_name):
            print(f"  [{role_name}] skipped (already complete)")
            roles_completed.append(role_name)
            continue

        # Announce start
        if event_tracker:
            event_tracker.planner_role_start(
                role_name, f"Producing {artifact}"
            )

        # Record role start in state machine
        model = _resolve_model(role.get("model", "sonnet"), config)
        state_mgr.start_role(role_name, model=model)
        state_mgr.update_state(current_planner_role=role_name)

        # Lazy imports to avoid circular dependency with orchestrator
        from .orchestrator import run_agent_session, load_prompt  # noqa: PLC0415

        # Load base prompt + context
        prompt_path = Path(prompts_dir) / role.get("prompt", f"{role_name}.md")
        base_prompt = load_prompt(prompt_path, {"STATE_DIR": str(state_dir)})
        context = _build_role_context(role, state_dir, knowledge_section, input_section)
        full_prompt = base_prompt + context

        options = create_client_options(project_dir, config, model_override=model)

        # Run agent session
        t_start = time.time()
        result = await run_agent_session(full_prompt, options, Path(project_dir), progress_label=f"Planner/{role_name}")
        duration_ms = int((time.time() - t_start) * 1000)
        cost = result.get("cost", 0.0)
        result_usage = result.get("usage") or {}
        sdk_duration_ms = result.get("duration_ms", 0)
        total_cost += cost
        cost_tracker.record("planner", cost,
                            usage=result_usage,
                            duration_ms=sdk_duration_ms,
                            duration_api_ms=result.get("duration_api_ms", 0),
                            num_turns=result.get("num_turns", 0))

        print(f"  {role_name}: complete. Cost: ${cost:.2f}. Artifact: {artifact}")
        _write_planner_log(state_dir, "role_complete",
                           role=role_name, artifact=artifact,
                           cost_usd=cost, duration_ms=sdk_duration_ms,
                           input_tokens=result_usage.get("input_tokens", 0),
                           output_tokens=result_usage.get("output_tokens", 0),
                           num_turns=result.get("num_turns", 0))

        # Validation gate
        validation_result = _validate_artifact(role, state_dir)
        if not validation_result.get("valid"):
            reason = validation_result.get("reason", "validation failed")

            # Validator rejection (sign_off: false) goes to refiner-fix, not a same-role retry.
            # Only the refiner-fix can actually modify the work plan to address issues.
            if role_name == "validator" and validation_result.get("issues"):
                print(f"  [{role_name}] REJECTED: {reason}")
                artifact_bytes = _artifact_size(state_dir, artifact)
                state_mgr.reject_role(
                    role_name,
                    cost_usd=cost,
                    artifact=artifact,
                    validation=validation_result,
                )
                roles_completed.append(role_name)
                if event_tracker:
                    event_tracker.planner_role_reject(
                        role_name,
                        issues_count=len(validation_result["issues"]),
                        cost_usd=cost,
                    )
                continue  # Skip to post-loop where refiner-fix handles it

            # Non-validator roles: retry once with feedback
            print(f"  [{role_name}] validation failed: {reason}. Retrying...")

            if event_tracker:
                event_tracker.planner_role_retry(role_name)

            artifact_content = _read_artifact(state_dir, artifact)
            retry_prompt = (
                full_prompt
                + f"\n\nVALIDATION FAILED: {reason}\n\nYour previous output:\n{artifact_content}"
            )
            retry_options = create_client_options(project_dir, config, model_override=model)
            retry_result = await run_agent_session(retry_prompt, retry_options, Path(project_dir), progress_label=f"Planner/{role_name} (retry)")
            retry_cost = retry_result.get("cost", 0.0)
            total_cost += retry_cost
            cost_tracker.record("planner", retry_cost,
                                usage=retry_result.get("usage") or {},
                                duration_ms=retry_result.get("duration_ms", 0),
                                duration_api_ms=retry_result.get("duration_api_ms", 0),
                                num_turns=retry_result.get("num_turns", 0))

            retry_validation = _validate_artifact(role, state_dir)
            if retry_validation.get("valid"):
                print(f"  [{role_name}] retry succeeded. Cost: ${retry_cost:.2f}")
                validation_result = retry_validation
            else:
                print(f"  [{role_name}] retry also failed — proceeding anyway")
                validation_result = retry_validation
                if event_tracker:
                    event_tracker.planner_role_fail(role_name, retry_validation.get("reason", ""))

        # Record role completion in state machine
        artifact_bytes = _artifact_size(state_dir, artifact)
        state_mgr.complete_role(
            role_name,
            cost_usd=cost,
            artifact=artifact,
            artifact_size_bytes=artifact_bytes,
            validation=validation_result,
        )
        roles_completed.append(role_name)

        if event_tracker:
            event_tracker.planner_role_pass(role_name, artifact, duration_ms)

    # Post-loop: Validator → Refiner feedback loop (state-machine driven)
    validation_path = state_dir / "validation.json"
    if validation_path.exists():
        try:
            validation_data = json.loads(validation_path.read_text())
            if not validation_data.get("sign_off") and validation_data.get("issues"):
                issues = validation_data["issues"]
                ps = state_mgr.get_planner_state()
                if ps["fix_loops_completed"] < ps["max_fix_loops"]:
                    validator_attempt = sum(
                        1 for r in ps["roles"] if r["name"] == "validator"
                    )
                    print(f"\n  Validator rejected ({len(issues)} issues) — re-running Refiner to fix...")
                    fix_cost = await _run_refiner_fix(
                        issues, roles, state_dir, project_dir, config,
                        state_mgr, cost_tracker, event_tracker,
                        triggered_by=f"validator:attempt:{validator_attempt}",
                    )
                    total_cost += fix_cost
                else:
                    print(f"  Validator rejected but max fix loops ({ps['max_fix_loops']}) reached — proceeding with warning")
        except Exception as e:
            print(f"  Validator feedback loop failed (non-fatal): {e}")

    # All roles done — mark pipeline complete
    state_mgr.complete_planner()
    state_mgr.update_state(planner_complete=True)

    if event_tracker:
        event_tracker.planner_validation_summary(roles_completed, total_cost)

    return {
        "success": True,
        "roles_completed": roles_completed,
        "total_cost": total_cost,
    }
