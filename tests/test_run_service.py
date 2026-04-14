"""Tests for the product-facing run service."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.product.services.run_service import RunService


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


class TestRunService:
    def test_build_execution_request_from_prompt(self, tmp_dir):
        service = RunService()
        request = service.build_execution_request(
            tmp_dir,
            prompt="Build a dashboard",
        )
        assert request["source_type"] == "manual"
        assert request["context_bundle"]["prompt"] == "Build a dashboard"
        assert request["project_id"] == tmp_dir.name

    def test_build_execution_request_from_spec(self, tmp_dir):
        service = RunService()
        spec_path = tmp_dir / "spec.md"
        request = service.build_execution_request(
            tmp_dir,
            spec_path=spec_path,
        )
        assert request["source_type"] == "spec"
        assert request["context_bundle"]["spec_path"] == str(spec_path)

    def test_run_normalizes_kernel_result(self, tmp_dir):
        fake_runner = AsyncMock(
            return_value={
                "run_id": "run-123",
                "duration_minutes": 2,
                "features": {
                    "total": 5,
                    "passing": 3,
                    "blocked": 1,
                    "remaining": 1,
                },
                "cost": {"total": 12.5},
            }
        )
        service = RunService(harness_runner=fake_runner)
        result = asyncio.run(
            service.run(
                config_path=tmp_dir / "config.yaml",
                project_dir=tmp_dir,
                prompt="Build a dashboard",
            )
        )
        assert result["run_id"] == "run-123"
        assert result["status"] == "in_progress"
        assert result["cost_usd"] == 12.5
        assert result["duration_seconds"] == 120
        assert "3/5 tasks passing" in result["summary"]

    def test_run_from_execution_request_maps_paths(self, tmp_dir):
        fake_runner = AsyncMock(
            return_value={
                "run_id": "run-abc",
                "duration_minutes": 0,
                "features": {"total": 0, "passing": 0, "blocked": 0, "remaining": 0},
                "cost": {"total": 0},
            }
        )
        service = RunService(harness_runner=fake_runner)
        spec_path = tmp_dir / "spec.md"
        request = {
            "request_id": "req-1",
            "project_id": tmp_dir.name,
            "source_type": "spec",
            "objective": "Execute spec",
            "context_bundle": {"spec_path": str(spec_path)},
            "runtime_profile": "claude",
            "execution_policy": {},
        }
        asyncio.run(
            service.run_from_execution_request(
                config_path=tmp_dir / "config.yaml",
                project_dir=tmp_dir,
                execution_request=request,
            )
        )
        _, kwargs = fake_runner.await_args
        assert kwargs["spec_path"] == spec_path
        assert kwargs["project_dir"] == tmp_dir
        assert kwargs["execution_request"]["request_id"] == "req-1"
