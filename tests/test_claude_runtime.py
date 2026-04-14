"""Tests for the Claude runtime adapter."""

import asyncio
import tempfile
from pathlib import Path

from src.adapters.runtime.claude_runtime import ClaudeRuntimeAdapter


import pytest


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def config():
    return {
        "model": "claude-sonnet-4-6",
        "evaluator": {
            "browser_tool": "playwright",
            "browser_verification": "auto",
        },
        "generator": {
            "max_turns_per_session": 1000,
        },
        "security": {
            "sandbox": True,
            "permission_mode": "acceptEdits",
            "deny_reads": [".env"],
        },
    }


class TestClaudeRuntimeAdapter:
    def test_load_runtime_config(self, tmp_dir):
        path = tmp_dir / "config.yaml"
        path.write_text("model: test-model\n")
        adapter = ClaudeRuntimeAdapter()
        loaded = adapter.load_runtime_config(path)
        assert loaded["model"] == "test-model"

    def test_build_session_options(self, tmp_dir, config):
        adapter = ClaudeRuntimeAdapter()
        request = {
            "request_id": "req-123",
            "project_id": "demo",
            "source_type": "manual",
            "objective": "Build a dashboard",
            "context_bundle": {"prompt": "Build a dashboard"},
            "runtime_profile": "claude",
            "execution_policy": {},
        }
        options = adapter.build_session_options(tmp_dir, config, request)
        assert options["model"] == "claude-sonnet-4-6"
        assert "Bash" in options["allowed_tools"]
        assert Path(options["settings"]).exists()

    def test_run_session_delegates_to_runner(self, tmp_dir):
        adapter = ClaudeRuntimeAdapter()

        async def fake_runner(prompt, options, project_dir, **kwargs):
            return {
                "status": "continue",
                "output": prompt,
                "project_dir": str(project_dir),
                "kwargs": kwargs,
            }

        result = asyncio.run(
            adapter.run_session(
                "hello",
                {"model": "test"},
                tmp_dir,
                progress_label="Planner",
                system_prompt="System",
                session_runner=fake_runner,
            )
        )
        assert result["output"] == "hello"
        assert result["project_dir"] == str(tmp_dir)
        assert result["kwargs"]["progress_label"] == "Planner"
