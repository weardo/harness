"""Product-facing run service wrapping the current Harness orchestrator."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

try:
    from adapters.events.event_normalizer import ProductEventNormalizer
    from adapters.runtime.claude_runtime import ClaudeRuntimeAdapter
    from adapters.storage.artifact_store import ArtifactStore
    from interfaces.execution_types import ExecutionRequest, KernelRunResult
    from product.models.request import RequestRecord
    from product.services.context_assembler import ContextAssembler
    from product.services.recipe_selector import RecipeSelector
    from core.orchestrator import run_harness
except ImportError:  # pragma: no cover - package import path under pytest
    from src.adapters.events.event_normalizer import ProductEventNormalizer
    from src.adapters.runtime.claude_runtime import ClaudeRuntimeAdapter
    from src.adapters.storage.artifact_store import ArtifactStore
    from src.interfaces.execution_types import ExecutionRequest, KernelRunResult
    from src.product.models.request import RequestRecord
    from src.product.services.context_assembler import ContextAssembler
    from src.product.services.recipe_selector import RecipeSelector
    from src.core.orchestrator import run_harness


HarnessRunner = Callable[..., Awaitable[dict[str, Any]]]


class RunService:
    """Thin service boundary between product callers and the current kernel."""

    def __init__(
        self,
        *,
        runtime_adapter: Optional[ClaudeRuntimeAdapter] = None,
        harness_runner: Optional[HarnessRunner] = None,
    ) -> None:
        self.runtime_adapter = runtime_adapter or ClaudeRuntimeAdapter()
        self.event_normalizer = ProductEventNormalizer()
        self.artifact_store = ArtifactStore()
        self.context_assembler = ContextAssembler()
        self.recipe_selector = RecipeSelector()
        self._harness_runner = harness_runner or run_harness

    def build_execution_request(
        self,
        project_dir: Path,
        *,
        prompt: Optional[str] = None,
        spec_path: Optional[Path] = None,
        plan_path: Optional[Path] = None,
        resume: bool = False,
        request_id: Optional[str] = None,
        runtime_profile: str = "claude",
        execution_policy: Optional[dict[str, Any]] = None,
    ) -> ExecutionRequest:
        """Normalize legacy CLI inputs into an execution request."""
        if resume:
            source_type = "resume"
            objective = "Resume the most recent Harness run."
        elif spec_path is not None:
            source_type = "spec"
            objective = f"Execute from specification: {spec_path.name}"
        elif plan_path is not None:
            source_type = "plan"
            objective = f"Execute from plan: {plan_path.name}"
        else:
            source_type = "manual"
            objective = prompt or ""

        context_bundle: dict[str, Any] = {}
        if spec_path is not None:
            context_bundle["spec_path"] = str(spec_path)
        if plan_path is not None:
            context_bundle["plan_path"] = str(plan_path)
        if prompt is not None:
            context_bundle["prompt"] = prompt
        if resume:
            context_bundle["resume"] = True

        return ExecutionRequest(
            request_id=request_id or "adhoc",
            project_id=project_dir.name,
            source_type=source_type,
            objective=objective,
            context_bundle=context_bundle,
            runtime_profile=runtime_profile,
            execution_policy=execution_policy or {},
        )

    def build_execution_request_from_request(
        self,
        project_dir: Path,
        request: RequestRecord,
        *,
        runtime_profile: str = "claude",
        execution_policy: Optional[dict[str, Any]] = None,
    ) -> ExecutionRequest:
        """Build an execution request from a product-layer request object."""
        context_bundle = self.context_assembler.assemble(request, project_dir)
        linked_paths = context_bundle.get("linked_paths", []) or []
        execution_intent, execution_recipe = self.recipe_selector.select_for_request(
            request,
            context_bundle=context_bundle,
        )

        spec_path = next((p for p in linked_paths if str(p).endswith(".md")), None)
        objective = request.get("title", "") or context_bundle.get("objective", "")

        return ExecutionRequest(
            request_id=request.get("id", "adhoc"),
            project_id=request.get("project_id", project_dir.name),
            source_type="request",
            objective=objective,
            context_bundle={
                "prompt": request.get("description", ""),
                "request": request,
                "assembled_context": context_bundle,
                "spec_path": spec_path,
                "linked_paths": linked_paths,
            },
            runtime_profile=runtime_profile,
            execution_policy=execution_policy or {},
            execution_intent=execution_intent,
            execution_recipe=execution_recipe,
        )

    async def run(
        self,
        *,
        config_path: Path,
        project_dir: Path,
        prompt: Optional[str] = None,
        spec_path: Optional[Path] = None,
        plan_path: Optional[Path] = None,
        resume: bool = False,
        run_id: Optional[str] = None,
        cli_overrides: Optional[dict[str, Any]] = None,
    ) -> KernelRunResult:
        """Create an execution request from legacy inputs and run it."""
        request = self.build_execution_request(
            project_dir,
            prompt=prompt,
            spec_path=spec_path,
            plan_path=plan_path,
            resume=resume,
            request_id=run_id,
            execution_policy=cli_overrides,
        )
        return await self.run_from_execution_request(
            config_path=config_path,
            project_dir=project_dir,
            execution_request=request,
            resume=resume,
            run_id=run_id,
            cli_overrides=cli_overrides,
        )

    async def run_for_request(
        self,
        *,
        config_path: Path,
        project_dir: Path,
        request: RequestRecord,
        cli_overrides: Optional[dict[str, Any]] = None,
    ) -> KernelRunResult:
        """Run the kernel from a product-layer request object."""
        execution_request = self.build_execution_request_from_request(
            project_dir,
            request,
            execution_policy=cli_overrides,
        )
        return await self.run_from_execution_request(
            config_path=config_path,
            project_dir=project_dir,
            execution_request=execution_request,
            cli_overrides=cli_overrides,
        )

    async def run_from_execution_request(
        self,
        *,
        config_path: Path,
        project_dir: Path,
        execution_request: ExecutionRequest,
        resume: bool = False,
        run_id: Optional[str] = None,
        cli_overrides: Optional[dict[str, Any]] = None,
    ) -> KernelRunResult:
        """Run the current harness kernel from a normalized execution request."""
        context_bundle = execution_request.get("context_bundle", {})
        raw_result = await self._harness_runner(
            config_path=config_path,
            project_dir=project_dir,
            prompt=context_bundle.get("prompt"),
            spec_path=self._optional_path(context_bundle.get("spec_path")),
            plan_path=self._optional_path(context_bundle.get("plan_path")),
            resume=resume or execution_request.get("source_type") == "resume",
            run_id=run_id,
            cli_overrides=cli_overrides,
            execution_request=execution_request,
        )
        return self._normalize_result(raw_result, project_dir=project_dir)

    @staticmethod
    def _optional_path(value: Any) -> Optional[Path]:
        if not value:
            return None
        return Path(value)

    def _normalize_result(self, raw_result: dict[str, Any], *, project_dir: Path) -> KernelRunResult:
        """Shape the current Harness return value into a stable product-facing result."""
        features = raw_result.get("features", {}) or {}
        total = int(features.get("total", 0) or 0)
        passing = int(features.get("passing", 0) or 0)
        remaining = int(features.get("remaining", 0) or 0)
        blocked = int(features.get("blocked", 0) or 0)
        if total:
            summary = (
                f"{passing}/{total} tasks passing, {blocked} blocked, {remaining} remaining."
            )
        else:
            summary = "Harness run completed."

        status = "complete" if total and remaining == 0 else "in_progress"
        cost = raw_result.get("cost", {}) or {}
        total_cost_usd = float(cost.get("total", 0.0) or 0.0)

        run_id = raw_result.get("run_id", "")
        artifacts = self.artifact_store.list_run_artifacts(project_dir, run_id) if run_id else []

        return KernelRunResult(
            run_id=run_id,
            status=status,
            summary=summary,
            cost_usd=total_cost_usd,
            duration_seconds=int((raw_result.get("duration_minutes", 0) or 0) * 60),
            artifacts=artifacts,
            branch_name=None,
            pr_url=None,
            raw_result=raw_result,
        )

    def normalize_event(self, run_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """Normalize a raw current-generation Harness event for product consumers."""
        return self.event_normalizer.normalize(run_id, event)
