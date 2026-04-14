#!/usr/bin/env python3
"""
Long-Running Agent Harness — CLI Entry Point
=============================================

Usage:
    python run.py --prompt "Build a todo app with auth"
    python run.py --spec docs/specs/todo-spec.md
    python run.py --plan docs/plans/todo-plan.md
    python run.py --resume
    python run.py --prompt "..." --max-cost 50 --max-duration 120 --max-iterations 20
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add parent to path so we can import core modules
sys.path.insert(0, str(Path(__file__).parent))

from product.services.run_service import RunService


def _copy_retrospective_template(project_dir: str, project_name: str, features_total: int, features_done: int, duration_str: str = "unknown"):
    """Copy retrospective template to project dir on run completion."""
    import os
    template_path = os.path.join(os.path.dirname(__file__), "templates", "retrospective.json.template")
    retro_path = os.path.join(project_dir, "retrospective.json")

    if os.path.exists(retro_path):
        return  # Don't overwrite existing retrospective

    if not os.path.exists(template_path):
        print(f"[harness] Warning: retrospective template not found at {template_path}")
        return

    from datetime import datetime
    with open(template_path) as f:
        template = f.read()

    content = template.replace("{PROJECT_NAME}", project_name)
    content = content.replace("{DATE}", datetime.now().strftime("%Y-%m-%d"))
    content = content.replace("{DURATION}", duration_str)
    content = content.replace("{FEATURES_PLANNED}", str(features_total))
    content = content.replace("{FEATURES_IMPLEMENTED}", str(features_done))

    with open(retro_path, "w") as f:
        f.write(content)

    print(f"[harness] Retrospective written to {retro_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Long-Running Agent Harness — 3-agent autonomous build system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Build from prompt
  python run.py --prompt "Build a todo app with auth and real-time sync"

  # Build from existing spec
  python run.py --spec docs/specs/todo-spec.md

  # Build from existing plan
  python run.py --plan docs/plans/todo-plan.md

  # Resume after crash
  python run.py --resume

  # With limits
  python run.py --prompt "..." --max-cost 50 --max-duration 120 --max-iterations 20
        """,
    )

    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument("--prompt", type=str, help="Project description (1-4 sentences)")
    input_group.add_argument("--spec", type=Path, help="Path to existing spec file")
    input_group.add_argument("--plan", type=Path, help="Path to existing plan file")
    input_group.add_argument("--resume", action="store_true", help="Resume previous run")

    parser.add_argument("--project-dir", type=Path, default=Path("."),
                        help="Project directory (default: current directory)")
    parser.add_argument("--config", type=Path, default=None,
                        help="Config file path (default: .harness/config.yaml)")
    parser.add_argument("--max-cost", type=float, default=None,
                        help="Maximum cost in USD")
    parser.add_argument("--max-duration", type=int, default=None,
                        help="Maximum duration in minutes")
    parser.add_argument("--max-iterations", type=int, default=None,
                        help="Maximum generator iterations")
    parser.add_argument("--model", type=str, default=None,
                        help="Override model for generator/evaluator")
    parser.add_argument("--planner-model", type=str, default=None,
                        help="Override model for planner")

    return parser.parse_args()


def main():
    args = parse_args()
    run_service = RunService()

    # Validate input
    if not args.resume and not args.prompt and not args.spec and not args.plan:
        print("Error: one of --prompt, --spec, --plan, or --resume is required")
        sys.exit(1)

    # Resolve paths
    project_dir = args.project_dir.resolve()
    config_path = args.config or (project_dir / ".harness" / "config.yaml")

    if not config_path.exists():
        # Try the src config as fallback (for development)
        src_config = Path(__file__).parent / "config.yaml"
        if src_config.exists():
            config_path = src_config
        else:
            print(f"Error: config not found at {config_path}")
            sys.exit(1)

    # Build CLI overrides
    overrides = {}
    if args.max_cost is not None:
        overrides["max_cost_usd"] = args.max_cost
    if args.max_duration is not None:
        overrides["max_duration_minutes"] = args.max_duration
    if args.max_iterations is not None:
        overrides["max_iterations"] = args.max_iterations
    if args.model:
        overrides["model"] = args.model
    if args.planner_model:
        overrides["planner_model"] = args.planner_model

    # Load .env from project dir (stdlib-only, no dotenv dependency)
    import os
    env_path = project_dir / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip().lstrip("export ")
                value = value.strip().strip("'\"")
                if key and not os.environ.get(key):
                    os.environ[key] = value

    # Auto-detect control plane: .env > probe localhost
    if not os.environ.get("HARNESS_CONTROL_PLANE_URL"):
        try:
            import urllib.request
            urllib.request.urlopen("http://localhost:7842/api/v1/projects", timeout=2)
            os.environ["HARNESS_CONTROL_PLANE_URL"] = "http://localhost:7842"
        except Exception:
            pass
    if os.environ.get("HARNESS_CONTROL_PLANE_URL"):
        print(f"  Control plane: {os.environ['HARNESS_CONTROL_PLANE_URL']}")

    # Run
    try:
        result = asyncio.run(
            run_service.run(
                config_path=config_path,
                project_dir=project_dir,
                prompt=args.prompt,
                spec_path=args.spec,
                plan_path=args.plan,
                resume=args.resume,
                cli_overrides=overrides if overrides else None,
            )
        )
        raw_result = result.get("raw_result", {})

        _copy_retrospective_template(
            str(project_dir),
            project_dir.name,
            raw_result.get("features", {}).get("total", 0),
            raw_result.get("features", {}).get("passing", 0),
            f"{raw_result.get('duration_minutes', 0)}m",
        )

        # Exit 0: harness ran successfully (complete or hit a configured limit)
        # Exit 1: unrecoverable error (handled by except blocks below)
        sys.exit(0)

    except KeyboardInterrupt:
        print("\nInterrupted. Resume with: python run.py --resume")
        sys.exit(130)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"\nFatal error: {e}")
        print(tb)
        print("Resume with: python run.py --resume")
        # Write to unified run.jsonl if state_dir can be inferred
        try:
            import json as _json
            from datetime import datetime, timezone
            runs_dir = project_dir / ".harness" / "runs"
            if runs_dir.exists():
                latest = sorted(runs_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
                if latest:
                    log_dir = latest[0] / "logs"
                    log_dir.mkdir(exist_ok=True)
                    entry = {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "event": "error",
                        "context": "run_harness_fatal",
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "traceback": tb,
                    }
                    with open(log_dir / "run.jsonl", "a") as f:
                        f.write(_json.dumps(entry) + "\n")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
