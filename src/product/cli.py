"""Minimal product-facing CLI for requests, context inspection, and run launch."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

try:
    from product.launch import (
        build_execution_overrides,
        load_project_env,
        maybe_detect_control_plane,
        resolve_config_path,
    )
    from product.services.run_launch_service import RunLaunchService
    from product.services.context_assembler import ContextAssembler
    from product.services.request_service import RequestService
    from product.services.run_service import RunService
except ImportError:  # pragma: no cover - package import path under pytest
    from src.product.launch import (
        build_execution_overrides,
        load_project_env,
        maybe_detect_control_plane,
        resolve_config_path,
    )
    from src.product.services.run_launch_service import RunLaunchService
    from src.product.services.context_assembler import ContextAssembler
    from src.product.services.request_service import RequestService
    from src.product.services.run_service import RunService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Harness product CLI for requests, context, and runs",
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path("."),
        help="Project directory (default: current directory)",
    )

    subparsers = parser.add_subparsers(dest="resource", required=True)

    requests_parser = subparsers.add_parser("requests", help="Manage product-layer requests")
    requests_subparsers = requests_parser.add_subparsers(dest="action", required=True)

    create_parser = requests_subparsers.add_parser("create", help="Create a request")
    create_parser.add_argument("--project-id", type=str, default=None, help="Logical project id")
    create_parser.add_argument("--title", required=True, type=str, help="Short request title")
    create_parser.add_argument("--description", required=True, type=str, help="Request description")
    create_parser.add_argument("--source", default="manual", type=str, help="Request source")
    create_parser.add_argument("--priority", default="normal", type=str, help="Request priority")
    create_parser.add_argument(
        "--acceptance-criterion",
        action="append",
        default=[],
        dest="acceptance_criteria",
        help="Acceptance criterion (repeatable)",
    )
    create_parser.add_argument(
        "--link-path",
        action="append",
        default=[],
        dest="linked_paths",
        help="Project-relative path to include in context (repeatable)",
    )

    requests_subparsers.add_parser("list", help="List saved requests")

    show_parser = requests_subparsers.add_parser("show", help="Show one request")
    show_parser.add_argument("request_id", type=str, help="Request identifier")

    context_parser = subparsers.add_parser("context", help="Inspect assembled request context")
    context_subparsers = context_parser.add_subparsers(dest="action", required=True)

    context_show_parser = context_subparsers.add_parser("show", help="Show request context")
    context_show_parser.add_argument("--request-id", required=True, type=str, help="Request identifier")

    runs_parser = subparsers.add_parser("runs", help="Launch product-layer runs")
    runs_subparsers = runs_parser.add_subparsers(dest="action", required=True)

    runs_start_parser = runs_subparsers.add_parser("start", help="Start a run from a request")
    runs_start_parser.add_argument("--request-id", required=True, type=str, help="Request identifier")
    runs_start_parser.add_argument(
        "--launch-id",
        type=str,
        default=None,
        help=argparse.SUPPRESS,
    )
    runs_start_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Config file path (default: .harness/config.yaml)",
    )
    runs_start_parser.add_argument("--max-cost", type=float, default=None, help="Maximum cost in USD")
    runs_start_parser.add_argument(
        "--max-duration",
        type=int,
        default=None,
        help="Maximum duration in minutes",
    )
    runs_start_parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Maximum generator iterations",
    )
    runs_start_parser.add_argument("--model", type=str, default=None, help="Override model")
    runs_start_parser.add_argument(
        "--planner-model",
        type=str,
        default=None,
        help="Override planner model",
    )

    return parser


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(
    argv: Optional[list[str]] = None,
    *,
    request_service_cls: type[RequestService] = RequestService,
    context_assembler: Optional[ContextAssembler] = None,
    run_service: Optional[RunService] = None,
) -> int:
    args = parse_args(argv)
    project_dir = args.project_dir.resolve()
    request_service = request_service_cls(project_dir)
    assembler = context_assembler or ContextAssembler()
    launcher = run_service or RunService()

    if args.resource == "requests":
        return _handle_requests(args, request_service, project_dir)
    if args.resource == "context":
        return _handle_context(args, request_service, assembler, project_dir)
    if args.resource == "runs":
        return _handle_runs(args, request_service, launcher, project_dir)

    print(f"Unknown command group: {args.resource}", file=sys.stderr)
    return 1


def _handle_requests(
    args: argparse.Namespace,
    request_service: RequestService,
    project_dir: Path,
) -> int:
    if args.action == "create":
        record = request_service.create_request(
            project_id=args.project_id or project_dir.name,
            title=args.title,
            description=args.description,
            source=args.source,
            priority=args.priority,
            acceptance_criteria=args.acceptance_criteria,
            linked_paths=args.linked_paths,
        )
        _print_json(record)
        return 0

    if args.action == "list":
        _print_json(request_service.list_requests())
        return 0

    if args.action == "show":
        record = request_service.get_request(args.request_id)
        if record is None:
            print(f"Request not found: {args.request_id}", file=sys.stderr)
            return 1
        _print_json(record)
        return 0

    print(f"Unknown requests action: {args.action}", file=sys.stderr)
    return 1


def _handle_context(
    args: argparse.Namespace,
    request_service: RequestService,
    assembler: ContextAssembler,
    project_dir: Path,
) -> int:
    if args.action != "show":
        print(f"Unknown context action: {args.action}", file=sys.stderr)
        return 1

    record = request_service.get_request(args.request_id)
    if record is None:
        print(f"Request not found: {args.request_id}", file=sys.stderr)
        return 1

    _print_json(assembler.assemble(record, project_dir))
    return 0


def _handle_runs(
    args: argparse.Namespace,
    request_service: RequestService,
    run_service: RunService,
    project_dir: Path,
) -> int:
    if args.action != "start":
        print(f"Unknown runs action: {args.action}", file=sys.stderr)
        return 1

    record = request_service.get_request(args.request_id)
    if record is None:
        print(f"Request not found: {args.request_id}", file=sys.stderr)
        return 1

    config_path = resolve_config_path(project_dir, args.config)
    if config_path is None:
        print(f"Error: config not found at {args.config or (project_dir / '.harness' / 'config.yaml')}", file=sys.stderr)
        return 1

    overrides = build_execution_overrides(
        max_cost=args.max_cost,
        max_duration=args.max_duration,
        max_iterations=args.max_iterations,
        model=args.model,
        planner_model=args.planner_model,
    )
    load_project_env(project_dir)
    maybe_detect_control_plane()
    launch_service = RunLaunchService(project_dir)

    metadata = dict(record.get("metadata", {}) or {})
    metadata.pop("launch_error", None)
    if args.launch_id:
        metadata["active_launch_id"] = args.launch_id
        metadata["last_launch_id"] = args.launch_id
    request_service.update_request(args.request_id, status="running", metadata=metadata)

    try:
        result = asyncio.run(
            run_service.run_for_request(
                config_path=config_path,
                project_dir=project_dir,
                request=record,
                cli_overrides=overrides or None,
            )
        )
    except KeyboardInterrupt:
        failure_metadata = dict(metadata)
        failure_metadata.pop("active_launch_id", None)
        failure_metadata["launch_error"] = "Run interrupted."
        request_service.update_request(args.request_id, status="blocked", metadata=failure_metadata)
        if args.launch_id:
            _safe_fail_launch(launch_service, args.launch_id, error="Run interrupted.", exit_code=130)
        raise
    except Exception as exc:
        failure_metadata = dict(metadata)
        failure_metadata.pop("active_launch_id", None)
        failure_metadata["launch_error"] = str(exc)
        request_service.update_request(args.request_id, status="blocked", metadata=failure_metadata)
        if args.launch_id:
            _safe_fail_launch(launch_service, args.launch_id, error=str(exc), exit_code=1)
        print(str(exc), file=sys.stderr)
        return 1

    existing_metadata = dict(metadata)
    existing_metadata.pop("active_launch_id", None)
    if result.get("run_id"):
        existing_metadata["last_run_id"] = result["run_id"]

    if result.get("status") == "complete":
        status = "completed"
        existing_metadata.pop("launch_error", None)
    else:
        status = "blocked"
        existing_metadata["launch_error"] = (
            result.get("summary")
            or "Run exited without completing all requested work."
        )
    request_service.update_request(args.request_id, status=status, metadata=existing_metadata)
    if args.launch_id:
        _safe_complete_launch(
            launch_service,
            args.launch_id,
            request_status_after_exit=status,
            run_id=result.get("run_id"),
        )
    _print_json(result)
    return 0


def _safe_complete_launch(
    launch_service: RunLaunchService,
    launch_id: str,
    *,
    request_status_after_exit: str,
    run_id: Optional[str],
) -> None:
    try:
        launch_service.complete_launch(
            launch_id,
            request_status_after_exit=request_status_after_exit,
            run_id=run_id,
        )
    except ValueError:
        return


def _safe_fail_launch(
    launch_service: RunLaunchService,
    launch_id: str,
    *,
    error: str,
    exit_code: int,
) -> None:
    try:
        launch_service.fail_launch(launch_id, error=error, exit_code=exit_code)
    except ValueError:
        return


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


if __name__ == "__main__":  # pragma: no cover - CLI execution
    raise SystemExit(main())
