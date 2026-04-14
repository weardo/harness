"""Execution-layer types shared by product services and runtime adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional, Protocol, TypedDict


SourceType = Literal["request", "spec", "plan", "manual", "resume"]
IntentType = Literal[
    "greenfield_build",
    "brownfield_change",
    "bugfix",
    "review_or_analysis",
    "plan_only",
    "resume",
]
ScopeLevel = Literal["tiny", "small", "medium", "large"]
PlanningPolicy = Literal["none", "minimal", "full", "resume_existing"]
TaskSource = Literal["direct_scope", "work_plan", "feature_list", "existing_plan"]
ExecutionMode = Literal["single_scope", "task_loop", "parallel_task_loop"]
EvaluationPolicy = Literal["light", "scoped_tests", "full_suite"]
CompletionPolicy = Literal["single_change", "task_graph", "analysis_only"]


class ArtifactRef(TypedDict, total=False):
    """Structured reference to a generated artifact."""

    kind: str
    path: str
    metadata: dict[str, Any]


class ExecutionRequest(TypedDict, total=False):
    """Canonical input shape for execution requests entering the kernel."""

    request_id: str
    project_id: str
    source_type: SourceType
    objective: str
    context_bundle: dict[str, Any]
    runtime_profile: str
    execution_policy: dict[str, Any]
    execution_intent: "ExecutionIntent"
    execution_recipe: "ExecutionRecipe"


class ExecutionIntent(TypedDict, total=False):
    """Intent metadata used to choose the right kernel flow for a request."""

    intent_type: IntentType
    scope_level: ScopeLevel
    desired_outcome: str
    linked_paths: list[str]
    has_existing_spec: bool
    has_existing_plan: bool
    resume_run_id: Optional[str]


class ExecutionRecipe(TypedDict, total=False):
    """Kernel recipe chosen for a request intent."""

    recipe_id: str
    planning_policy: PlanningPolicy
    task_source: TaskSource
    execution_mode: ExecutionMode
    evaluation_policy: EvaluationPolicy
    completion_policy: CompletionPolicy


class KernelRunResult(TypedDict, total=False):
    """Normalized output shape returned by the product-facing run service."""

    run_id: str
    status: str
    summary: str
    cost_usd: float
    duration_seconds: int
    artifacts: list[ArtifactRef]
    branch_name: Optional[str]
    pr_url: Optional[str]
    raw_result: dict[str, Any]


class KernelEvent(TypedDict, total=False):
    """Normalized event shape emitted by the execution layer."""

    run_id: str
    ts: str
    phase: str
    kind: str
    task_id: Optional[str]
    message: str
    metadata: dict[str, Any]


class RuntimeAdapter(Protocol):
    """Abstraction over model/runtime-specific session setup and execution."""

    def load_runtime_config(self, config_path: Path) -> dict[str, Any]:
        """Load runtime configuration from disk."""

    def build_session_options(
        self,
        project_dir: Path,
        config: dict[str, Any],
        request: ExecutionRequest,
        *,
        model_override: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ) -> dict[str, Any]:
        """Build session options for the underlying agent runtime."""

    async def run_session(
        self,
        prompt: str,
        options: dict[str, Any],
        project_dir: Path,
        *,
        progress_label: str = "",
        system_prompt: Optional[str] = None,
        resume_session_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Run one agent session using the underlying runtime."""
