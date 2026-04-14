"""Select execution intent and recipe for product-layer requests."""

from __future__ import annotations

from typing import Any

try:
    from interfaces.execution_types import ExecutionIntent, ExecutionRecipe
    from product.models.request import RequestRecord
except ImportError:  # pragma: no cover - package import path under pytest
    from src.interfaces.execution_types import ExecutionIntent, ExecutionRecipe
    from src.product.models.request import RequestRecord


class RecipeSelector:
    """Translate product request shape into an execution intent and recipe."""

    def select_for_request(
        self,
        request: RequestRecord,
        *,
        context_bundle: dict[str, Any],
    ) -> tuple[ExecutionIntent, ExecutionRecipe]:
        linked_paths = list(context_bundle.get("linked_paths", []) or request.get("linked_paths", []) or [])
        has_existing_spec = any(str(path).endswith(".md") for path in linked_paths)
        has_existing_plan = any(str(path).endswith(".json") and "plan" in str(path) for path in linked_paths)
        description = str(request.get("description", "") or "")

        if linked_paths and not has_existing_spec and len(description) < 600:
            scope_level = self._scope_level_for(linked_paths)
            return (
                ExecutionIntent(
                    intent_type="brownfield_change",
                    scope_level=scope_level,
                    desired_outcome="code_change",
                    linked_paths=linked_paths,
                    has_existing_spec=False,
                    has_existing_plan=has_existing_plan,
                    resume_run_id=None,
                ),
                ExecutionRecipe(
                    recipe_id="brownfield-scoped-v1",
                    planning_policy="minimal",
                    task_source="direct_scope",
                    execution_mode="single_scope",
                    evaluation_policy="scoped_tests",
                    completion_policy="single_change",
                ),
            )

        return (
            ExecutionIntent(
                intent_type="greenfield_build",
                scope_level="large",
                desired_outcome="code_change",
                linked_paths=linked_paths,
                has_existing_spec=has_existing_spec,
                has_existing_plan=has_existing_plan,
                resume_run_id=None,
            ),
            ExecutionRecipe(
                recipe_id="greenfield-full-v1",
                planning_policy="full",
                task_source="work_plan",
                execution_mode="task_loop",
                evaluation_policy="full_suite",
                completion_policy="task_graph",
            ),
        )

    @staticmethod
    def _scope_level_for(linked_paths: list[str]) -> str:
        count = len(linked_paths)
        if count <= 2:
            return "tiny"
        if count <= 5:
            return "small"
        if count <= 12:
            return "medium"
        return "large"
