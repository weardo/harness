"""Build explicit context bundles from product-layer request objects."""

from __future__ import annotations

from pathlib import Path

try:
    from product.models.request import ContextBundleRecord, RequestRecord
except ImportError:  # pragma: no cover - package import path under pytest
    from src.product.models.request import ContextBundleRecord, RequestRecord


class ContextAssembler:
    """Construct a small, inspectable context bundle from a request."""

    def assemble(self, request: RequestRecord, project_dir: Path) -> ContextBundleRecord:
        linked_paths = request.get("linked_paths", []) or []
        linked_artifacts: list[dict[str, str]] = []
        for rel_path in linked_paths:
            path = Path(rel_path)
            if not path.is_absolute():
                path = Path(project_dir) / rel_path
            if path.exists():
                linked_artifacts.append(
                    {
                        "path": str(path),
                        "name": path.name,
                    }
                )

        description = request.get("description", "").strip()
        request_summary = description.splitlines()[0] if description else request.get("title", "")

        return ContextBundleRecord(
            request_id=request.get("id", ""),
            objective=request.get("title", "") or request_summary,
            request_summary=request_summary,
            acceptance_criteria=request.get("acceptance_criteria", []) or [],
            linked_paths=linked_paths,
            linked_artifacts=linked_artifacts,
            source=request.get("source", "manual"),
        )
