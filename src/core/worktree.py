"""
Worktree Utilities
==================

Git worktree introspection and lifecycle management:
list_worktrees, is_ancestor_of, sweep_merged_worktrees,
should_use_worktree, and find_worktree_by_topic for task routing decisions.
"""

import subprocess
from pathlib import Path
from typing import Optional

from .parallel import _rescue_submodule_objects


def list_worktrees(repo_dir) -> list[dict]:
    """Return a list of all git worktrees for the given repo.

    Each entry is a dict with keys:
        path (Path): absolute path to the worktree
        commit (str): HEAD commit SHA
        branch (Optional[str]): branch name, or None for detached HEAD
        is_main (bool): True only for the primary worktree

    Returns a list with at least one entry (the main worktree).
    """
    repo_dir = Path(repo_dir).resolve()
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        timeout=30,
    )
    result.check_returncode()

    worktrees = []
    # Split on blank lines between stanzas
    raw_blocks = result.stdout.strip().split("\n\n")

    for idx, block in enumerate(raw_blocks):
        if not block.strip():
            continue
        entry: dict = {"path": None, "commit": None, "branch": None, "is_main": idx == 0}

        for line in block.splitlines():
            line = line.rstrip()
            if line.startswith("worktree "):
                entry["path"] = Path(line[len("worktree "):]).resolve()
            elif line.startswith("HEAD "):
                entry["commit"] = line[len("HEAD "):]
            elif line.startswith("branch "):
                ref = line[len("branch "):]
                # Normalise refs/heads/<name> → <name>
                if ref.startswith("refs/heads/"):
                    entry["branch"] = ref[len("refs/heads/"):]
                else:
                    entry["branch"] = ref
            elif line == "detached":
                entry["branch"] = None
            # "bare" keyword: leave branch as None

        if entry["path"] is not None:
            worktrees.append(entry)

    return worktrees


def sweep_merged_worktrees(
    repo_dir,
    dry_run: bool = False,
    state_dir: Optional[Path] = None,
) -> list[str]:
    """Remove linked worktrees whose HEAD has been merged into the main worktree.

    - Never removes the main worktree (is_main=True).
    - Removes linked worktrees whose HEAD commit is an ancestor of (or equal to)
      the main worktree's current HEAD.
    - Calls ``git worktree remove --force`` for each merged worktree.
    - Calls ``git branch -D`` for each removed worktree's branch (errors ignored).
    - Calls ``git worktree prune`` at the end (always, including dry_run).
    - dry_run=True returns paths that *would* be removed without touching anything.

    By default, dirty worktrees (uncommitted changes) are SKIPPED to protect
    in-progress agent work. If ``state_dir`` is provided, dirty worktrees that
    have **no entry in worker_assignments.json** are treated as orphans and
    removed regardless of dirty status — those changes are leftover from a
    killed orchestrator, not in-progress work that anyone will resume.

    Returns a list of removed worktree path strings.
    """
    repo_dir = Path(repo_dir).resolve()

    # Load assignment lookup if state_dir provided so we can detect orphans
    _assigned_worktrees: set[str] = set()
    if state_dir is not None:
        try:
            from . import worker_assignments as _wa
            _data = _wa.load_assignments(Path(state_dir))
            for _wid, _a in _data.get("assignments", {}).items():
                wt = _a.get("worktree_dir", "")
                if wt:
                    _assigned_worktrees.add(str(Path(wt).resolve()))
        except Exception:
            pass  # Best-effort — fall back to dirty-protection

    # Resolve the main worktree's HEAD — the merge target
    head_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        timeout=30,
    )
    head_result.check_returncode()
    main_head = head_result.stdout.strip()

    worktrees = list_worktrees(repo_dir)
    removed: list[str] = []

    for wt in worktrees:
        if wt["is_main"]:
            continue  # main worktree is never removed

        if is_ancestor_of(repo_dir, wt["commit"], target=main_head):
            path_str = str(wt["path"])

            # By default protect worktrees with uncommitted changes — a generator
            # may be actively writing files but hasn't committed yet. Exception:
            # if state_dir is provided AND this worktree has no assignment entry,
            # the dirty changes are orphans (left by a killed orchestrator) and
            # safe to delete.
            wt_path = Path(path_str)
            _is_orphan = (
                state_dir is not None
                and str(wt_path.resolve()) not in _assigned_worktrees
            )
            if wt_path.exists() and not _is_orphan:
                _dirty = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=str(wt_path),
                    capture_output=True, text=True, timeout=10,
                )
                # Also check submodules for uncommitted changes
                _sub_dirty = subprocess.run(
                    ["git", "submodule", "foreach", "--quiet",
                     "git", "status", "--porcelain"],
                    cwd=str(wt_path),
                    capture_output=True, text=True, timeout=15,
                )
                if _dirty.stdout.strip() or _sub_dirty.stdout.strip():
                    continue  # Skip — worktree has uncommitted work in progress

            if not dry_run:
                # Rescue submodule objects before destroying the worktree
                if wt["branch"]:
                    try:
                        _rescue_submodule_objects(repo_dir, wt["branch"])
                    except Exception:
                        pass  # Best-effort
                subprocess.run(
                    ["git", "worktree", "remove", "--force", path_str],
                    cwd=str(repo_dir),
                    capture_output=True,
                    timeout=30,
                )
                if wt["branch"]:
                    subprocess.run(
                        ["git", "branch", "-D", wt["branch"]],
                        cwd=str(repo_dir),
                        capture_output=True,
                        timeout=30,
                    )
            removed.append(path_str)

    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=str(repo_dir),
        capture_output=True,
        timeout=30,
    )

    return removed


def is_ancestor_of(repo_dir, commit: str, target: str = "HEAD") -> bool:
    """Return True if commit is an ancestor of (or equal to) target.

    Uses `git merge-base --is-ancestor` semantics:
        exit 0  → True  (commit is ancestor of target, or they are equal)
        exit 1  → False (commit is NOT an ancestor)
        exit ≥128 → RuntimeError (bad object, not a git repo, etc.)
    """
    repo_dir = Path(repo_dir).resolve()
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, target],
        cwd=str(repo_dir),
        capture_output=True,
        timeout=30,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise RuntimeError(
        f"git merge-base --is-ancestor failed (exit {result.returncode}): "
        f"{result.stderr.decode(errors='replace').strip()}"
    )


# Keywords that indicate a read-only / exploratory task — no worktree needed
READ_ONLY_KEYWORDS = [
    "explore",
    "validate",
    "research",
    "search",
    "audit",
    "review",
    "read",
    "check",
    "inspect",
    "analyze",
]


def find_worktree_by_topic(repo_dir, topic: str) -> Optional[dict]:
    """Find the first non-main worktree that relates to *topic*.

    Search order:
        1. Branch name contains *topic* (case-insensitive).
        2. Any of the last 10 commit messages on the branch contains *topic*.

    Returns the first matching worktree dict (same shape as list_worktrees),
    or None if no worktree matches.
    """
    repo_dir = Path(repo_dir).resolve()
    lower_topic = topic.lower()

    for wt in list_worktrees(repo_dir):
        if wt["is_main"]:
            continue

        # Phase 1: branch name match
        branch = wt["branch"] or ""
        if lower_topic in branch.lower():
            return wt

        # Phase 2: recent commit messages on the branch
        ref = branch or wt["commit"]
        result = subprocess.run(
            ["git", "log", "--oneline", "-10", ref],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and lower_topic in result.stdout.lower():
            return wt

    return None


def should_use_worktree(task_description: str) -> bool:
    """Return True if the task warrants an isolated worktree.

    Scans *task_description* for read-only keywords (case-insensitive).
    Returns False when a read-only keyword is found; True otherwise
    (including when the description is empty).
    """
    lower = task_description.lower()
    for keyword in READ_ONLY_KEYWORDS:
        if keyword in lower:
            return False
    return True
