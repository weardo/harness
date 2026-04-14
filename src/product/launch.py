"""Shared launch helpers for product-facing CLI and API surfaces."""

from __future__ import annotations

import os
import urllib.request
from pathlib import Path
from typing import Any, Optional


def resolve_config_path(project_dir: Path, config_path: Optional[Path]) -> Optional[Path]:
    """Resolve a config path with the same fallback behavior as the legacy CLI."""
    resolved = config_path or (project_dir / ".harness" / "config.yaml")
    if resolved.exists():
        return resolved

    src_config = Path(__file__).resolve().parents[1] / "config.yaml"
    if src_config.exists():
        return src_config

    return None


def build_execution_overrides(
    *,
    max_cost: Optional[float] = None,
    max_duration: Optional[int] = None,
    max_iterations: Optional[int] = None,
    model: Optional[str] = None,
    planner_model: Optional[str] = None,
) -> dict[str, Any]:
    """Build a normalized execution override payload from product inputs."""
    overrides: dict[str, Any] = {}
    if max_cost is not None:
        overrides["max_cost_usd"] = max_cost
    if max_duration is not None:
        overrides["max_duration_minutes"] = max_duration
    if max_iterations is not None:
        overrides["max_iterations"] = max_iterations
    if model:
        overrides["model"] = model
    if planner_model:
        overrides["planner_model"] = planner_model
    return overrides


def load_project_env(project_dir: Path) -> None:
    """Load `.env` from the project directory without overriding existing env vars."""
    env_path = project_dir / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            value = value.strip().strip("'\"")
            if key and not os.environ.get(key):
                os.environ[key] = value


def maybe_detect_control_plane() -> None:
    """Populate `HARNESS_CONTROL_PLANE_URL` if a local control plane is reachable."""
    if os.environ.get("HARNESS_CONTROL_PLANE_URL"):
        return

    try:
        urllib.request.urlopen("http://localhost:7842/api/v1/projects", timeout=2)
        os.environ["HARNESS_CONTROL_PLANE_URL"] = "http://localhost:7842"
    except Exception:
        return
