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
import time
from pathlib import Path
from typing import Optional

from .state import atomic_write, atomic_read
from .client import create_client_options, load_config


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

    Returns:
        {"success": bool, "roles_completed": list[str], "total_cost": float}
    """
    if config is None:
        config = load_config()

    roles = strategy_config.get("planner_roles", [])
    roles_completed = []
    total_cost = 0.0

    # Load resume state (feature 030-031)
    state = state_mgr.load_state()
    planner_roles_done = state.get("planner_roles", {})  # default {} if missing (031)

    for role in roles:
        role_name = role.get("name", "")
        artifact = role.get("artifact", "")

        # Resume: skip already-completed roles (feature 030)
        if planner_roles_done.get(role_name):
            print(f"  [{role_name}] skipped (already complete)")
            roles_completed.append(role_name)
            continue

        # Announce start
        if event_tracker:
            event_tracker.planner_role_start(
                role_name, f"Producing {artifact}"
            )

        # Update state: current role in progress (feature 030)
        state_mgr.update_state(current_planner_role=role_name)

        # Lazy imports to avoid circular dependency with orchestrator
        from .orchestrator import run_agent_session, load_prompt  # noqa: PLC0415

        # Load base prompt + context
        prompt_path = Path(prompts_dir) / role.get("prompt", f"{role_name}.md")
        base_prompt = load_prompt(prompt_path, {"STATE_DIR": str(state_dir)})
        context = _build_role_context(role, state_dir, knowledge_section, input_section)
        full_prompt = base_prompt + context

        # Resolve model (feature 028)
        model = _resolve_model(role.get("model", "sonnet"), config)
        options = create_client_options(project_dir, config, model_override=model)

        # Run agent session (feature 026)
        t_start = time.time()
        result = await run_agent_session(full_prompt, options, Path(project_dir))
        duration_ms = int((time.time() - t_start) * 1000)
        cost = result.get("cost", 0.0)
        total_cost += cost
        cost_tracker.record("planner", cost)

        print(f"  {role_name}: complete. Cost: ${cost:.2f}. Artifact: {artifact}")

        # Validation gate (feature 029)
        validation_result = _validate_artifact(role, state_dir)
        if not validation_result.get("valid"):
            reason = validation_result.get("reason", "validation failed")
            print(f"  [{role_name}] validation failed: {reason}. Retrying...")

            if event_tracker:
                event_tracker.planner_role_retry(role_name)

            # Build retry prompt with feedback
            artifact_content = _read_artifact(state_dir, artifact)
            retry_prompt = (
                full_prompt
                + f"\n\nVALIDATION FAILED: {reason}\n\nYour previous output:\n{artifact_content}"
            )
            retry_options = create_client_options(project_dir, config, model_override=model)
            retry_result = await run_agent_session(retry_prompt, retry_options, Path(project_dir))
            retry_cost = retry_result.get("cost", 0.0)
            total_cost += retry_cost
            cost_tracker.record("planner", retry_cost)

            retry_validation = _validate_artifact(role, state_dir)
            if retry_validation.get("valid"):
                print(f"  [{role_name}] retry succeeded. Cost: ${retry_cost:.2f}")
            else:
                print(f"  [{role_name}] retry also failed — proceeding anyway")
                if event_tracker:
                    event_tracker.planner_role_fail(role_name, retry_validation.get("reason", ""))

        # Mark role complete in state (feature 030)
        planner_roles_done[role_name] = True
        state_mgr.update_state(planner_roles=planner_roles_done)
        roles_completed.append(role_name)

        if event_tracker:
            event_tracker.planner_role_pass(role_name, artifact, duration_ms)

    # All roles done → mark pipeline complete (feature 030)
    state_mgr.update_state(planner_complete=True)

    if event_tracker:
        event_tracker.planner_validation_summary(roles_completed, total_cost)

    return {
        "success": True,
        "roles_completed": roles_completed,
        "total_cost": total_cost,
    }
