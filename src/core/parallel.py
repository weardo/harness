"""
Parallel Agent Teams — Phase 2
================================

Groups features by dependency layers and runs multiple generator agents
concurrently in isolated git worktrees.
"""

import asyncio
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Optional


def group_by_dependency(features: list) -> list[list[dict]]:
    """Group features into dependency layers for parallel execution.

    Features with no dependencies go in layer 0.
    Features depending on layer 0 features go in layer 1, etc.

    Returns list of layers, where each layer is a list of features
    that can be executed in parallel.
    """
    if not features:
        return []

    # Build dependency graph
    id_to_feature = {f["id"]: f for f in features}
    remaining = {f["id"] for f in features if not f.get("passes") and not f.get("blocked")}

    # Track which features are resolved (passed, blocked, or already assigned to a layer)
    resolved = {f["id"] for f in features if f.get("passes") or f.get("blocked")}

    layers = []
    max_iterations = len(features) + 1  # Prevent infinite loop on circular deps

    for _ in range(max_iterations):
        if not remaining:
            break

        # Find features whose dependencies are all resolved
        layer = []
        for fid in list(remaining):
            feature = id_to_feature[fid]
            deps = feature.get("depends_on", [])
            if all(d in resolved for d in deps):
                layer.append(feature)

        if not layer:
            # Circular dependency or unresolvable — put remaining in final layer
            layer = [id_to_feature[fid] for fid in remaining]
            layers.append(layer)
            break

        layers.append(layer)
        for f in layer:
            remaining.discard(f["id"])
            resolved.add(f["id"])

    return layers


def _init_submodules_local(project_dir: Path, worktree_dir: Path) -> None:
    """Init submodules in a worktree from the parent repo's local cache.

    By default, git submodule update in a worktree clones each submodule
    from the remote — even though .git/modules/ already has every object.
    We override each submodule URL to point at the local .git/modules/<name>
    cache, then init. Pure local file clone, zero network access.
    """
    modules_dir = (project_dir / ".git" / "modules").resolve()
    if not modules_dir.exists():
        return

    # Get list of submodule names from .gitmodules
    result = subprocess.run(
        ["git", "config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"],
        cwd=worktree_dir,
        capture_output=True, text=True,
    )
    if not result.stdout.strip():
        return

    for line in result.stdout.strip().splitlines():
        # Format: submodule.<name>.path <path>
        key, sm_path = line.split(None, 1)
        sm_name = key.split(".")[1]
        local_cache = modules_dir / sm_name

        if not local_cache.exists():
            continue

        # Point submodule URL at local cache instead of remote
        subprocess.run(
            ["git", "config", f"submodule.{sm_name}.url", str(local_cache)],
            cwd=worktree_dir,
            capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "-c", "protocol.file.allow=always",
             "submodule", "update", "--init", "--", sm_path],
            cwd=worktree_dir,
            capture_output=True, text=True,
        )
        # Worktree submodules may have .git pointer but empty working tree.
        # git checkout fails in this state; reset --hard forces file population.
        sm_full = worktree_dir / sm_path
        if sm_full.is_dir():
            entries = [e.name for e in sm_full.iterdir()]
            if entries == [".git"] or not entries:
                subprocess.run(
                    ["git", "reset", "--hard", "HEAD"],
                    cwd=sm_full,
                    capture_output=True, text=True,
                )


def write_task_brief(
    worktree_dir: Path,
    feature: dict,
    state_dir: Path,
    work_plan=None,
    relay_context: str = "",
) -> Path:
    """Write a slim TASK_BRIEF.md into the worktree with only what the agent needs.

    Replaces having agents read 150KB+ feature_list.json and work_plan.json.
    The brief is ~2-5KB — everything the agent needs to implement one task.
    """
    feature_id = feature.get("id", "unknown")
    desc = feature.get("description", "")
    scope = feature.get("scope", [])
    deps = feature.get("depends_on", [])
    ac = feature.get("acceptance_criteria", feature.get("ac", ""))

    # Get progress summary without reading the full feature_list
    try:
        import json
        fl = json.loads((state_dir / "feature_list.json").read_text())
        total = len(fl)
        passing = sum(1 for f in fl if f.get("passes"))
        blocked = sum(1 for f in fl if f.get("blocked"))
        progress = f"{passing}/{total} passing, {blocked} blocked, {total - passing - blocked} remaining"
    except Exception:
        progress = "(unknown)"

    lines = [
        f"# Task Brief: {feature_id}",
        "",
        f"**Feature:** {feature_id}",
        f"**Description:** {desc}",
        "",
    ]

    if ac:
        lines += ["## Acceptance Criteria", "", ac if isinstance(ac, str) else "\n".join(f"- {a}" for a in ac), ""]

    if scope:
        lines += ["## File Scope", ""] + [f"- `{s}`" for s in scope] + [""]

    if deps:
        lines += ["## Dependencies (already done)", ""] + [f"- {d}" for d in deps] + [""]

    lines += [f"## Project Progress", "", progress, ""]

    if relay_context:
        lines += ["## Discoveries from Prior Waves", "", relay_context, ""]

    brief_path = worktree_dir / "TASK_BRIEF.md"
    try:
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text("\n".join(lines))
    except OSError:
        pass  # Non-fatal — agent falls back to reading state files
    return brief_path


def create_worktree(project_dir: Path, worker_id: int, base_branch: str = "HEAD") -> tuple[Path, str]:
    """Create an isolated git worktree for a worker.

    Returns (worktree_path, branch_name).
    """
    project_dir = Path(project_dir)
    worktree_dir = project_dir / ".worktrees" / f"worker-{worker_id}"
    branch_name = f"harness/worker-{worker_id}"

    # Clean up stale worktree if exists
    if worktree_dir.exists():
        cleanup_worktree(project_dir, worktree_dir, branch_name)

    # Create worktree
    subprocess.run(
        ["git", "worktree", "add", str(worktree_dir), "-b", branch_name],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=True,
    )

    # Init submodules using the parent repo's .git/modules/ as reference.
    # Without --reference, git clones each submodule from remote per worktree.
    _init_submodules_local(project_dir, worktree_dir)

    return worktree_dir, branch_name


def branch_has_commits(project_dir: Path, branch_name: str) -> bool:
    """Check if a branch has commits ahead of main/HEAD."""
    result = subprocess.run(
        ["git", "log", "HEAD.." + branch_name, "--oneline"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def cleanup_worktree(
    project_dir: Path,
    worktree_dir: Path,
    branch_name: str,
    *,
    force: bool = False,
) -> bool:
    """Remove a git worktree and its branch.

    If force=False (default), refuses to delete a branch that has commits
    ahead of main. Returns True if cleanup happened, False if preserved.

    Normal completion after merge passes force=True.
    Sweep and stale cleanup should pass force=False to preserve work.
    """
    project_dir = Path(project_dir)
    worktree_dir = Path(worktree_dir)

    if not force and branch_has_commits(project_dir, branch_name):
        return False  # Preserve — branch has unmerged work

    # Remove worktree
    subprocess.run(
        ["git", "worktree", "remove", str(worktree_dir), "--force"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )

    # Clean up leftover directory (macOS .DS_Store etc)
    import shutil
    if worktree_dir.exists():
        shutil.rmtree(worktree_dir, ignore_errors=True)

    # Delete branch
    subprocess.run(
        ["git", "branch", "-D", branch_name],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    return True


def _rescue_submodule_objects(project_dir: Path, branch_name: str) -> None:
    """Fetch submodule commits from a worktree branch into the main submodule stores.

    Worktree submodules get their own object store under
    .git/worktrees/<wt>/modules/<name>/. When the worktree is removed,
    those objects are deleted — even if the meta-repo still references them
    via submodule pointers. This function copies them into the persistent
    .git/modules/<name>/ store before that happens.
    """
    modules_dir = (project_dir / ".git" / "modules").resolve()
    if not modules_dir.exists():
        return

    # Find the worktree directory for this branch
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=project_dir, capture_output=True, text=True,
    )
    wt_dir = None
    for block in result.stdout.split("\n\n"):
        lines = block.strip().splitlines()
        path_line = next((l for l in lines if l.startswith("worktree ")), None)
        branch_line = next((l for l in lines if l.startswith("branch ")), None)
        if path_line and branch_line and branch_name in branch_line:
            wt_dir = Path(path_line.split(" ", 1)[1])
            break
    if not wt_dir or not wt_dir.exists():
        return

    # For each submodule, check if the worktree has a separate object store
    # and fetch those objects into the main store
    gitmodules = wt_dir / ".gitmodules"
    if not gitmodules.exists():
        return

    sm_result = subprocess.run(
        ["git", "config", "--file", str(gitmodules), "--get-regexp", r"^submodule\..*\.path$"],
        cwd=wt_dir, capture_output=True, text=True,
    )
    if not sm_result.stdout.strip():
        return

    for line in sm_result.stdout.strip().splitlines():
        key, sm_path = line.split(None, 1)
        sm_name = key.split(".")[1]

        main_sm_gitdir = modules_dir / sm_name
        wt_sm_dir = wt_dir / sm_path

        if not main_sm_gitdir.exists() or not wt_sm_dir.exists():
            continue

        # Resolve the worktree submodule's actual git dir
        dot_git = wt_sm_dir / ".git"
        if not dot_git.exists():
            continue

        if dot_git.is_file():
            # .git file points to the real gitdir
            content = dot_git.read_text().strip()
            if content.startswith("gitdir:"):
                wt_sm_gitdir = (wt_sm_dir / content.split(":", 1)[1].strip()).resolve()
            else:
                continue
        else:
            wt_sm_gitdir = dot_git

        if not wt_sm_gitdir.exists() or wt_sm_gitdir == main_sm_gitdir:
            continue

        # Fetch all objects from the worktree's submodule into the main store
        subprocess.run(
            ["git", "-C", str(main_sm_gitdir), "fetch", str(wt_sm_gitdir)],
            capture_output=True, text=True,
        )


def merge_worktree(project_dir: Path, branch_name: str) -> dict:
    """Merge a worker branch back to the current branch.

    Returns {success: bool, conflict: bool, error: str}.
    """
    project_dir = Path(project_dir)

    # Rescue submodule objects from the worktree before merging.
    # Without this, worktree cleanup deletes the only copy of submodule commits.
    _rescue_submodule_objects(project_dir, branch_name)

    result = subprocess.run(
        ["git", "merge", "--no-ff", branch_name, "-m", f"Merge {branch_name}"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        return {"success": True, "conflict": False, "error": ""}

    # Check for merge conflict
    if "CONFLICT" in result.stdout or "CONFLICT" in result.stderr:
        # Abort the merge
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        return {
            "success": False,
            "conflict": True,
            "error": result.stdout + result.stderr,
        }

    return {
        "success": False,
        "conflict": False,
        "error": result.stderr or result.stdout,
    }


async def run_parallel_layer(
    features: list,
    project_dir: Path,
    config: dict,
    run_generator_fn,
    max_workers: int = 3,
) -> list[dict]:
    """Run a layer of features in parallel using worktrees.

    Args:
        features: list of features in this layer (all independent)
        project_dir: the main project directory
        config: harness config
        run_generator_fn: async function(worktree_dir, feature, config) -> result
        max_workers: max concurrent workers

    Returns list of {feature_id, result, worktree, branch, merged}.
    """
    workers_to_run = features[:max_workers]
    results = []

    # Create worktrees
    worktrees = []
    for i, feature in enumerate(workers_to_run):
        try:
            wt_dir, branch = create_worktree(project_dir, i)
            worktrees.append((feature, wt_dir, branch))
        except subprocess.CalledProcessError as e:
            results.append({
                "feature_id": feature["id"],
                "result": {"status": "error", "error": str(e)},
                "merged": False,
            })

    # Run generators in parallel
    tasks = []
    for feature, wt_dir, branch in worktrees:
        task = asyncio.create_task(
            run_generator_fn(wt_dir, feature, config)
        )
        tasks.append((task, feature, wt_dir, branch))

    # Wait for all workers
    for task, feature, wt_dir, branch in tasks:
        try:
            result = await task
        except Exception as e:
            result = {"status": "error", "error": str(e)}

        # Merge back
        merge_result = merge_worktree(project_dir, branch)
        cleanup_worktree(project_dir, wt_dir, branch)

        results.append({
            "feature_id": feature["id"],
            "result": result,
            "merged": merge_result["success"],
            "conflict": merge_result.get("conflict", False),
        })

    return results
