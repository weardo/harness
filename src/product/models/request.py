"""Request and context models for the product layer."""

from __future__ import annotations

from typing import Any, Literal, TypedDict


RequestSource = Literal["manual", "github", "slack", "spec", "plan"]
RequestStatus = Literal["new", "triaged", "planned", "running", "completed", "blocked"]


class RequestRecord(TypedDict, total=False):
    """Minimal product-layer request object."""

    id: str
    project_id: str
    title: str
    description: str
    source: RequestSource
    status: RequestStatus
    priority: str
    acceptance_criteria: list[str]
    linked_paths: list[str]
    metadata: dict[str, Any]


class ContextBundleRecord(TypedDict, total=False):
    """Minimal inspectable context bundle for product and runtime use."""

    request_id: str
    objective: str
    request_summary: str
    acceptance_criteria: list[str]
    linked_paths: list[str]
    linked_artifacts: list[dict[str, Any]]
    source: str
