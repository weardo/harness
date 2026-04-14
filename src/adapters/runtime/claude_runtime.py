"""Claude runtime adapter wrapping the current Harness client/session logic."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

try:
    from interfaces.execution_types import ExecutionRequest
    from interfaces.execution_types import RuntimeAdapter
    from core.client import create_client_options, load_config
except ImportError:  # pragma: no cover - package import path under pytest
    from src.interfaces.execution_types import ExecutionRequest
    from src.interfaces.execution_types import RuntimeAdapter
    from src.core.client import create_client_options, load_config


SessionRunner = Callable[..., Awaitable[dict[str, Any]]]


class ClaudeRuntimeAdapter(RuntimeAdapter):
    """Current default runtime adapter backed by Claude session machinery."""

    name = "claude"

    def load_runtime_config(self, config_path: Path) -> dict[str, Any]:
        """Load the existing Harness YAML config."""
        return load_config(config_path)

    def build_session_options(
        self,
        project_dir: Path,
        config: dict[str, Any],
        request: ExecutionRequest,
        *,
        model_override: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ) -> dict[str, Any]:
        """Build Claude session options using the existing client factory."""
        profile = request.get("runtime_profile")
        effective_system_prompt = system_prompt
        if effective_system_prompt is None and profile:
            effective_system_prompt = (
                f"You are an expert full-stack developer using the '{profile}' runtime profile."
            )
        return create_client_options(
            project_dir,
            config,
            model_override=model_override,
            system_prompt=effective_system_prompt
            or "You are an expert full-stack developer building a production-quality application.",
        )

    async def run_session(
        self,
        prompt: str,
        options: dict[str, Any],
        project_dir: Path,
        *,
        progress_label: str = "",
        system_prompt: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        session_runner: Optional[SessionRunner] = None,
    ) -> dict[str, Any]:
        """Delegate to the current Harness session runner."""
        runner = session_runner
        if runner is None:
            try:
                from core.orchestrator import run_agent_session
            except ImportError:  # pragma: no cover - package import path under pytest
                from src.core.orchestrator import run_agent_session
            runner = run_agent_session

        return await runner(
            prompt,
            options,
            project_dir,
            progress_label=progress_label,
            system_prompt=system_prompt,
            resume_session_id=resume_session_id,
        )
