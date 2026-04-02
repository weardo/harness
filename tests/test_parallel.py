"""Tests for parallel.py — dependency grouping and worktree management."""

import subprocess
import tempfile
from pathlib import Path

import pytest

from src.core.parallel import group_by_dependency, create_worktree, cleanup_worktree, merge_worktree


@pytest.fixture
def tmp_git_repo():
    """Create a temporary git repo for worktree tests."""
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
        # Need at least one commit for worktrees
        (repo / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
        yield repo


class TestGroupByDependency:
    def test_empty_features(self):
        assert group_by_dependency([]) == []

    def test_all_independent(self):
        features = [
            {"id": "001", "depends_on": [], "passes": False, "blocked": False},
            {"id": "002", "depends_on": [], "passes": False, "blocked": False},
            {"id": "003", "depends_on": [], "passes": False, "blocked": False},
        ]
        layers = group_by_dependency(features)
        assert len(layers) == 1
        assert len(layers[0]) == 3

    def test_linear_chain(self):
        features = [
            {"id": "001", "depends_on": [], "passes": False, "blocked": False},
            {"id": "002", "depends_on": ["001"], "passes": False, "blocked": False},
            {"id": "003", "depends_on": ["002"], "passes": False, "blocked": False},
        ]
        layers = group_by_dependency(features)
        assert len(layers) == 3
        assert layers[0][0]["id"] == "001"
        assert layers[1][0]["id"] == "002"
        assert layers[2][0]["id"] == "003"

    def test_diamond_dependency(self):
        features = [
            {"id": "001", "depends_on": [], "passes": False, "blocked": False},
            {"id": "002", "depends_on": ["001"], "passes": False, "blocked": False},
            {"id": "003", "depends_on": ["001"], "passes": False, "blocked": False},
            {"id": "004", "depends_on": ["002", "003"], "passes": False, "blocked": False},
        ]
        layers = group_by_dependency(features)
        assert len(layers) == 3
        assert layers[0][0]["id"] == "001"
        layer1_ids = {f["id"] for f in layers[1]}
        assert layer1_ids == {"002", "003"}
        assert layers[2][0]["id"] == "004"

    def test_skips_passing_features(self):
        features = [
            {"id": "001", "depends_on": [], "passes": True, "blocked": False},
            {"id": "002", "depends_on": ["001"], "passes": False, "blocked": False},
        ]
        layers = group_by_dependency(features)
        assert len(layers) == 1
        assert layers[0][0]["id"] == "002"

    def test_skips_blocked_features(self):
        features = [
            {"id": "001", "depends_on": [], "passes": False, "blocked": True},
            {"id": "002", "depends_on": [], "passes": False, "blocked": False},
        ]
        layers = group_by_dependency(features)
        assert len(layers) == 1
        assert layers[0][0]["id"] == "002"

    def test_resolved_deps_count(self):
        """Features depending on already-passed features go in layer 0."""
        features = [
            {"id": "001", "depends_on": [], "passes": True, "blocked": False},
            {"id": "002", "depends_on": ["001"], "passes": False, "blocked": False},
            {"id": "003", "depends_on": [], "passes": False, "blocked": False},
        ]
        layers = group_by_dependency(features)
        assert len(layers) == 1
        layer_ids = {f["id"] for f in layers[0]}
        assert layer_ids == {"002", "003"}

    def test_mixed_depths(self):
        features = [
            {"id": "001", "depends_on": [], "passes": False, "blocked": False},
            {"id": "002", "depends_on": [], "passes": False, "blocked": False},
            {"id": "003", "depends_on": ["001"], "passes": False, "blocked": False},
            {"id": "004", "depends_on": ["001", "002"], "passes": False, "blocked": False},
            {"id": "005", "depends_on": ["003"], "passes": False, "blocked": False},
        ]
        layers = group_by_dependency(features)
        assert len(layers) == 3
        layer0_ids = {f["id"] for f in layers[0]}
        assert layer0_ids == {"001", "002"}
        layer1_ids = {f["id"] for f in layers[1]}
        assert layer1_ids == {"003", "004"}
        assert layers[2][0]["id"] == "005"


class TestWorktreeOperations:
    def test_create_and_cleanup(self, tmp_git_repo):
        wt_dir, branch = create_worktree(tmp_git_repo, 0)
        assert wt_dir.exists()
        assert (wt_dir / "README.md").exists()

        # Verify branch exists
        result = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=tmp_git_repo,
            capture_output=True,
            text=True,
        )
        assert branch in result.stdout

        # Cleanup
        cleanup_worktree(tmp_git_repo, wt_dir, branch)
        assert not wt_dir.exists()

    def test_merge_no_conflict(self, tmp_git_repo):
        wt_dir, branch = create_worktree(tmp_git_repo, 0)

        # Make a change in worktree
        (wt_dir / "new_file.txt").write_text("hello from worker")
        subprocess.run(["git", "add", "."], cwd=wt_dir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "worker change"],
            cwd=wt_dir,
            capture_output=True,
        )

        # Merge back
        result = merge_worktree(tmp_git_repo, branch)
        assert result["success"] is True
        assert result["conflict"] is False
        assert (tmp_git_repo / "new_file.txt").exists()

        cleanup_worktree(tmp_git_repo, wt_dir, branch)

    def test_merge_conflict(self, tmp_git_repo):
        wt_dir, branch = create_worktree(tmp_git_repo, 0)

        # Change in worktree
        (wt_dir / "README.md").write_text("worker version\n")
        subprocess.run(["git", "add", "."], cwd=wt_dir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "worker change"],
            cwd=wt_dir,
            capture_output=True,
        )

        # Conflicting change on main
        (tmp_git_repo / "README.md").write_text("main version\n")
        subprocess.run(["git", "add", "."], cwd=tmp_git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "main change"],
            cwd=tmp_git_repo,
            capture_output=True,
        )

        # Merge should detect conflict
        result = merge_worktree(tmp_git_repo, branch)
        assert result["success"] is False
        assert result["conflict"] is True

        cleanup_worktree(tmp_git_repo, wt_dir, branch)
