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

    return worktree_dir, branch_name


def cleanup_worktree(project_dir: Path, worktree_dir: Path, branch_name: str) -> None:
    """Remove a git worktree and its branch."""
    project_dir = Path(project_dir)
    worktree_dir = Path(worktree_dir)

    # Remove worktree
    subprocess.run(
        ["git", "worktree", "remove", str(worktree_dir), "--force"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )

    # Delete branch
    subprocess.run(
        ["git", "branch", "-D", branch_name],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )


def merge_worktree(project_dir: Path, branch_name: str) -> dict:
    """Merge a worker branch back to the current branch.

    Returns {success: bool, conflict: bool, error: str}.
    """
    project_dir = Path(project_dir)

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
