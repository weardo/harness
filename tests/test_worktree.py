"""Tests for src/core/worktree.py — uses real temporary git repos (no mocks)."""
import subprocess
from pathlib import Path

import pytest

from src.core.worktree import list_worktrees, is_ancestor_of, sweep_merged_worktrees, should_use_worktree, find_worktree_by_topic


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    """Run a git command, raise on non-zero exit."""
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _make_repo(path: Path) -> None:
    """Initialise a bare-minimum git repo with one commit."""
    _git(["init"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    (path / "README.md").write_text("hello")
    _git(["add", "README.md"], path)
    _git(["commit", "-m", "initial"], path)


# ---------------------------------------------------------------------------
# Feature 004 — TestListWorktrees
# ---------------------------------------------------------------------------

class TestListWorktrees:
    def test_list_worktrees_basic(self, tmp_path):
        """Main + one linked worktree are returned."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)

        linked = tmp_path / "linked"
        _git(["worktree", "add", str(linked), "-b", "feature-x"], repo)

        entries = list_worktrees(repo)

        assert len(entries) == 2
        paths = [e["path"] for e in entries]
        assert repo.resolve() in paths
        assert linked.resolve() in paths

    def test_list_worktrees_main_flag(self, tmp_path):
        """First entry always has is_main=True; linked worktrees have is_main=False."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)

        linked = tmp_path / "linked"
        _git(["worktree", "add", str(linked), "-b", "feature-y"], repo)

        entries = list_worktrees(repo)

        assert entries[0]["is_main"] is True
        assert all(not e["is_main"] for e in entries[1:])

    def test_list_worktrees_detached(self, tmp_path):
        """Detached HEAD worktree returns branch=None."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)

        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, check=True
        ).stdout.strip()

        linked = tmp_path / "detached"
        _git(["worktree", "add", "--detach", str(linked)], repo)

        entries = list_worktrees(repo)

        detached_entry = next(e for e in entries if e["path"] == linked.resolve())
        assert detached_entry["branch"] is None

    def test_list_worktrees_paths_are_absolute(self, tmp_path):
        """All returned paths are absolute (Path objects)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)

        linked = tmp_path / "linked_abs"
        _git(["worktree", "add", str(linked), "-b", "abs-check"], repo)

        entries = list_worktrees(repo)

        for entry in entries:
            assert isinstance(entry["path"], Path)
            assert entry["path"].is_absolute()

    def test_list_worktrees_no_linked(self, tmp_path):
        """Repo with no linked worktrees returns single-entry list."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)

        entries = list_worktrees(repo)

        assert len(entries) == 1
        assert entries[0]["is_main"] is True
        assert entries[0]["path"] == repo.resolve()

    def test_list_worktrees_has_commit(self, tmp_path):
        """Each entry has a non-empty commit SHA."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)

        entries = list_worktrees(repo)

        for entry in entries:
            assert entry["commit"]
            assert len(entry["commit"]) == 40  # full SHA


# ---------------------------------------------------------------------------
# Feature 006 — TestIsAncestorOf
# ---------------------------------------------------------------------------

class TestIsAncestorOf:
    def test_ancestor(self, tmp_path):
        """Commit A is an ancestor of commit B made after it."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)

        commit_a = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, check=True
        ).stdout.strip()

        # Make a second commit
        (repo / "file2.txt").write_text("second")
        _git(["add", "file2.txt"], repo)
        _git(["commit", "-m", "second"], repo)

        assert is_ancestor_of(repo, commit_a) is True

    def test_same_commit(self, tmp_path):
        """A commit is its own ancestor (git semantics)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)

        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, check=True
        ).stdout.strip()

        assert is_ancestor_of(repo, head, target=head) is True

    def test_not_ancestor(self, tmp_path):
        """Commit on a diverged branch is NOT an ancestor of master/main."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)

        # Get HEAD of default branch
        main_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, check=True
        ).stdout.strip()

        # Create diverged branch with a new commit
        _git(["checkout", "-b", "diverged"], repo)
        (repo / "diverged.txt").write_text("diverged")
        _git(["add", "diverged.txt"], repo)
        _git(["commit", "-m", "diverged commit"], repo)

        diverged_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, check=True
        ).stdout.strip()

        # diverged_commit is NOT an ancestor of the original main_head
        assert is_ancestor_of(repo, diverged_commit, target=main_head) is False


# ---------------------------------------------------------------------------
# Feature 008 — TestSweepMergedWorktrees
# ---------------------------------------------------------------------------

class TestSweepMergedWorktrees:
    def test_sweep_removes_merged(self, tmp_path):
        """2 worktrees at master HEAD are removed; 1 ahead-of-master survives."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)

        wt1 = tmp_path / "wt1"
        wt2 = tmp_path / "wt2"
        wt3 = tmp_path / "wt3"

        _git(["worktree", "add", str(wt1), "-b", "merged-1"], repo)
        _git(["worktree", "add", str(wt2), "-b", "merged-2"], repo)
        _git(["worktree", "add", str(wt3), "-b", "ahead-1"], repo)

        # Advance wt3 so its HEAD is AHEAD of master
        (wt3 / "ahead.txt").write_text("ahead")
        _git(["add", "ahead.txt"], wt3)
        _git(["commit", "-m", "ahead commit"], wt3)

        removed = sweep_merged_worktrees(repo)

        assert len(removed) == 2
        removed_paths = {Path(p).resolve() for p in removed}
        assert wt1.resolve() in removed_paths
        assert wt2.resolve() in removed_paths
        assert wt3.resolve() not in removed_paths

        # wt1 and wt2 directories are gone; wt3 still exists
        assert not wt1.exists()
        assert not wt2.exists()
        assert wt3.exists()

    def test_sweep_never_removes_main(self, tmp_path):
        """Main worktree is never removed even though its commit equals HEAD."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)

        removed = sweep_merged_worktrees(repo)

        assert removed == []
        assert repo.exists()

    def test_sweep_dry_run(self, tmp_path):
        """dry_run=True returns paths that would be removed without removing them."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)

        wt1 = tmp_path / "wt1"
        wt2 = tmp_path / "wt2"
        _git(["worktree", "add", str(wt1), "-b", "merged-a"], repo)
        _git(["worktree", "add", str(wt2), "-b", "merged-b"], repo)

        removed = sweep_merged_worktrees(repo, dry_run=True)

        assert len(removed) == 2
        # Worktrees were NOT actually removed
        assert wt1.exists()
        assert wt2.exists()

    def test_sweep_calls_prune(self, tmp_path):
        """After sweep, git worktree prune runs and no ghost references remain."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)

        wt1 = tmp_path / "wt1"
        _git(["worktree", "add", str(wt1), "-b", "merged-prune"], repo)

        sweep_merged_worktrees(repo)

        # Only the main worktree should remain in the list (prune cleaned up)
        remaining = list_worktrees(repo)
        assert len(remaining) == 1
        assert remaining[0]["is_main"] is True

    def test_sweep_dry_run_still_prunes(self, tmp_path):
        """dry_run=True still calls git worktree prune, cleaning up ghost references."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)

        # Create a worktree then manually delete its directory — creates a ghost reference
        ghost = tmp_path / "ghost"
        _git(["worktree", "add", str(ghost), "-b", "ghost-branch"], repo)
        import shutil
        shutil.rmtree(str(ghost))  # remove dir without `git worktree remove`

        # Confirm the ghost reference exists before sweep
        raw = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(repo), capture_output=True, text=True, check=True
        ).stdout
        assert str(ghost) in raw  # ghost reference present

        # dry_run=True should NOT remove real worktrees, but SHOULD call prune
        sweep_merged_worktrees(repo, dry_run=True)

        # After prune, the ghost reference must be gone
        remaining = list_worktrees(repo)
        remaining_paths = [str(e["path"]) for e in remaining]
        assert not any(str(ghost) in p for p in remaining_paths)


# ---------------------------------------------------------------------------
# Feature 010 — TestShouldUseWorktree
# ---------------------------------------------------------------------------

class TestShouldUseWorktree:
    def test_explore_returns_false(self):
        assert should_use_worktree("explore the codebase") is False

    def test_validate_returns_false(self):
        assert should_use_worktree("validate the implementation") is False

    def test_research_returns_false(self):
        assert should_use_worktree("research authentication patterns") is False

    def test_search_returns_false(self):
        assert should_use_worktree("search for API endpoints") is False

    def test_audit_returns_false(self):
        assert should_use_worktree("audit security configuration") is False

    def test_implement_returns_true(self):
        assert should_use_worktree("implement auth module") is True

    def test_build_returns_true(self):
        assert should_use_worktree("build the dashboard") is True

    def test_fix_returns_true(self):
        assert should_use_worktree("fix the login bug") is True

    def test_add_returns_true(self):
        assert should_use_worktree("add user registration") is True

    def test_empty_string_returns_true(self):
        assert should_use_worktree("") is True

    def test_case_insensitive(self):
        assert should_use_worktree("EXPLORE the code") is False


# ---------------------------------------------------------------------------
# Features 011+012 — TestFindWorktreeByTopic
# ---------------------------------------------------------------------------

class TestFindWorktreeByTopic:
    def test_find_by_branch_name(self, tmp_path):
        """Worktree whose branch name contains the topic is returned."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)

        auth_wt = tmp_path / "auth-wt"
        _git(["worktree", "add", str(auth_wt), "-b", "feature/auth"], repo)

        result = find_worktree_by_topic(repo, "auth")

        assert result is not None
        assert result["branch"] == "feature/auth"

    def test_find_by_branch_name_case_insensitive(self, tmp_path):
        """Branch name match is case-insensitive."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)

        wt = tmp_path / "upper-wt"
        _git(["worktree", "add", str(wt), "-b", "feature/AUTH-system"], repo)

        result = find_worktree_by_topic(repo, "auth")

        assert result is not None
        assert result["branch"] == "feature/AUTH-system"

    def test_find_by_commit_message(self, tmp_path):
        """Worktree whose recent commit message contains topic is returned when branch name does not."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)

        billing_wt = tmp_path / "billing-wt"
        _git(["worktree", "add", str(billing_wt), "-b", "feature/task-42"], repo)
        (billing_wt / "billing.txt").write_text("billing stub")
        _git(["add", "billing.txt"], billing_wt)
        _git(["commit", "-m", "add billing integration stub"], billing_wt)

        result = find_worktree_by_topic(repo, "billing")

        assert result is not None
        assert result["branch"] == "feature/task-42"

    def test_no_match_returns_none(self, tmp_path):
        """Returns None when no worktree matches the topic."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)

        wt = tmp_path / "unrelated"
        _git(["worktree", "add", str(wt), "-b", "feature/unrelated"], repo)

        result = find_worktree_by_topic(repo, "nonexistent-topic-xyz")

        assert result is None

    def test_main_worktree_excluded(self, tmp_path):
        """Main worktree is never returned even if its path contains the topic."""
        repo = tmp_path / "auth-repo"
        repo.mkdir()
        _make_repo(repo)

        # Only main worktree exists; topic matches the directory name path but
        # find_worktree_by_topic must skip is_main=True entries.
        result = find_worktree_by_topic(repo, "auth")

        assert result is None

    def test_no_linked_worktrees_returns_none(self, tmp_path):
        """Returns None when only the main worktree exists."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)

        result = find_worktree_by_topic(repo, "anything")

        assert result is None
