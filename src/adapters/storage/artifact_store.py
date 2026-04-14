"""Surface product-facing artifact references from a Harness run directory."""

from __future__ import annotations

from pathlib import Path

try:
    from interfaces.execution_types import ArtifactRef
    from core.run_registry import RunRegistry
except ImportError:  # pragma: no cover - package import path under pytest
    from src.interfaces.execution_types import ArtifactRef
    from src.core.run_registry import RunRegistry


class ArtifactStore:
    """Derive a stable artifact view from the current run filesystem layout."""

    _RUN_ARTIFACTS = [
        ("spec", "spec.md"),
        ("work_plan", "work_plan.json"),
        ("feature_list", "feature_list.json"),
        ("feedback", "feedback.md"),
        ("planner_validation", "validation.json"),
        ("planner_gaps", "spec_gaps.json"),
        ("token_log", "token_log.jsonl"),
        ("planner_log", "logs/planner.jsonl"),
        ("run_log", "logs/run.jsonl"),
        ("state", "state.json"),
    ]

    def list_run_artifacts(self, project_dir: Path, run_id: str) -> list[ArtifactRef]:
        """List surfaced artifacts for a run, if the run directory exists."""
        harness_dir = Path(project_dir) / ".harness"
        registry = RunRegistry(harness_dir)
        try:
            run_dir = registry.run_dir(run_id)
        except ValueError:
            return []

        artifacts: list[ArtifactRef] = []
        for kind, rel_path in self._RUN_ARTIFACTS:
            path = run_dir / rel_path
            if path.exists():
                artifacts.append(
                    ArtifactRef(
                        kind=kind,
                        path=str(path),
                        metadata={
                            "relative_path": rel_path,
                            "size_bytes": path.stat().st_size,
                        },
                    )
                )

        retro_path = Path(project_dir) / "retrospective.json"
        if retro_path.exists():
            artifacts.append(
                ArtifactRef(
                    kind="retrospective",
                    path=str(retro_path),
                    metadata={
                        "relative_path": "retrospective.json",
                        "size_bytes": retro_path.stat().st_size,
                    },
                )
            )

        return artifacts
