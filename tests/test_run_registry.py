"""Tests for the dedicated run registry module."""

import tempfile
from pathlib import Path

import pytest

from src.core.run_registry import RunRegistry


@pytest.fixture
def harness_dir():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / ".harness"
        path.mkdir()
        yield path


class TestDedicatedRunRegistry:
    def test_create_run_and_list(self, harness_dir):
        registry = RunRegistry(harness_dir)
        run_id = registry.create_run("build auth")
        runs = registry.list_runs()
        assert runs[0]["run_id"] == run_id
        assert runs[0]["prompt"] == "build auth"

    def test_find_resumable(self, harness_dir):
        registry = RunRegistry(harness_dir)
        run_id = registry.create_run("build auth")
        assert registry.find_resumable() == run_id
