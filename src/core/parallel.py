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
from uuid import uuid4


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

    Knowledge sharing: points agents to a shared directory where all agents
    write discoveries. Agents read what they need on demand — no caps, no
    pre-filtering.
    """
    feature_id = feature.get("id", "unknown")
    desc = feature.get("description", "")
    scope = feature.get("scope", [])
    deps = feature.get("depends_on", [])
    ac = feature.get("acceptance_criteria", feature.get("ac", ""))

    # Detect revalidation mode: scope files already exist in the worktree
    revalidation = False
    if scope:
        existing = 0
        for s in scope:
            p = worktree_dir / s
            if p.exists() and (p.is_dir() or p.stat().st_size > 50):
                existing += 1
        revalidation = existing >= len(scope) * 0.5  # >50% of scope files exist

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

    mode_str = "REVALIDATION" if revalidation else "FRESH"

    lines = [
        f"# Task Brief: {feature_id}",
        "",
        f"**Feature:** {feature_id}",
        f"**Description:** {desc}",
        f"**Mode:** {mode_str}",
        "",
    ]

    if revalidation:
        lines += [
            "## REVALIDATION MODE",
            "",
            "Code for this task ALREADY EXISTS in the worktree. This is a re-evaluation run.",
            "",
            "**Generator rules:**",
            "- Do NOT rewrite or reimplement existing code.",
            "- Do NOT revert changes made by later tasks (fields removed, functions renamed, etc).",
            "- Only fix genuinely broken or missing functionality.",
            "- If the code satisfies the INTENT of the acceptance criteria, mark it passing.",
            "",
            "**Evaluator rules:**",
            "- Check if the INTENT of each criterion is met, not the exact letter.",
            "- If a later commit deliberately changed/removed something, that is evolution — PASS it.",
            "- Only FAIL if core functionality is genuinely broken or completely absent.",
            "",
        ]

    if ac:
        lines += ["## Acceptance Criteria", "", ac if isinstance(ac, str) else "\n".join(f"- {a}" for a in ac), ""]

    if scope:
        lines += ["## File Scope", ""] + [f"- `{s}`" for s in scope] + [""]

    if deps:
        lines += ["## Dependencies (already done)", ""] + [f"- {d}" for d in deps] + [""]

    lines += [f"## Project Progress", "", progress, ""]

    # Two-tier shared knowledge: persistent (across runs) + per-run (task briefs)
    # state_dir = <project>/.harness/runs/<run-id>, so .parent.parent = <project>/.harness/
    harness_root = Path(state_dir).parent.parent
    persistent_knowledge = harness_root / "knowledge"
    persistent_knowledge.mkdir(parents=True, exist_ok=True)
    run_knowledge = Path(state_dir) / "knowledge"
    run_knowledge.mkdir(parents=True, exist_ok=True)

    lines += [
        "## Shared Knowledge (READ BEFORE CODING)",
        "",
        "### Persistent learnings (across all runs)",
        f"Directory: `{persistent_knowledge}`",
        "Contains curated patterns, conventions, and gotchas that persist across runs.",
        "Read ALL .md files here before starting — they contain critical patterns.",
        "",
        "### This run's discoveries (from other agents)",
        f"Directory: `{run_knowledge}`",
        "Contains discoveries from agents working on tasks in this run.",
        "Read files here for recent patterns from parallel agents.",
        "",
        "### Your contribution",
        f"When you finish, write your discoveries to `{run_knowledge}/{feature_id}.md`.",
        "What to write: SDK import patterns, component conventions, gotchas, key decisions.",
        "",
    ]

    if relay_context:
        lines += ["## Discoveries from Prior Agents", "", relay_context, ""]

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

    if _branch_exists(project_dir, branch_name):
        if branch_has_commits(project_dir, branch_name):
            branch_name = _unique_worker_branch_name(project_dir, worker_id)
        else:
            subprocess.run(
                ["git", "branch", "-D", branch_name],
                cwd=project_dir,
                capture_output=True,
                text=True,
            )

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

    # Copy .env* files from the parent project's submodules into the worktree.
    # .env files are git-ignored by convention, so `git worktree add` never
    # brings them along. Without this step, agents running integration tests
    # inside the worktree hit "dial tcp [::1]:5432: connection refused" because
    # they have no DB host/port config.
    _copy_env_files(project_dir, worktree_dir)

    # Regenerate and link the local SDK so admin-ui tasks have up-to-date
    # connector types without needing to run `make sdk` manually.
    _link_local_sdk(project_dir, worktree_dir)

    return worktree_dir, branch_name


def _branch_exists(project_dir: Path, branch_name: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _unique_worker_branch_name(project_dir: Path, worker_id: int) -> str:
    base_name = f"harness/worker-{worker_id}"
    while True:
        candidate = f"{base_name}-{uuid4().hex[:8]}"
        if not _branch_exists(project_dir, candidate):
            return candidate


def _copy_env_files(project_dir: Path, worktree_dir: Path) -> None:
    """Copy `.env*` files from the parent project's submodules into the
    corresponding worktree submodules.

    Runs after `_init_submodules_local` so submodule directories exist. Uses
    a conservative allow-list of repos known to need DB/service credentials:
    `go-backend`, `go-worker`, `go-consumer`. Missing directories are silently
    skipped (project may not use monorepo layout).

    Copies:
      - .env           (current active profile, written by `make use-local`)
      - .env.local     (dev profile with Docker ports — source of truth for
                       tests that need Postgres/Mongo/Redis/Temporal)
      - .env.test      (if present — some repos separate test config)

    Does NOT copy `.env.stage` / `.env.production` — those point to remote
    infra and running tests against them from a worker is unsafe.
    """
    import shutil
    SUBMODULES = ("go-backend", "go-worker", "go-consumer")
    ENV_FILES = (".env", ".env.local", ".env.test")

    copied = 0
    for sm in SUBMODULES:
        src_dir = project_dir / sm
        dst_dir = worktree_dir / sm
        if not src_dir.is_dir() or not dst_dir.is_dir():
            continue
        for name in ENV_FILES:
            src = src_dir / name
            if not src.is_file():
                continue
            dst = dst_dir / name
            try:
                shutil.copy2(src, dst)
                copied += 1
            except OSError as e:
                print(f"  ⚠️  Failed to copy {sm}/{name} into {worktree_dir.name}: {e}")

    if copied:
        print(f"  🔑 Copied {copied} env file(s) into {worktree_dir.name}")


def _link_local_sdk(project_dir: Path, worktree_dir: Path) -> None:
    """Run `make sdk` in the go-backend submodule of the worktree.

    This regenerates the OpenAPI-derived TypeScript SDK from Go annotations and
    links it into the admin-ui pnpm workspace so tasks that depend on connector
    SDK types don't fail evaluation due to a stale published package.

    Skips silently if go-backend or its Makefile is absent (e.g. non-monorepo projects).
    """
    go_backend = worktree_dir / "go-backend"
    makefile = go_backend / "Makefile"
    if not makefile.exists():
        return

    import os
    # Ensure ~/go/bin is on PATH so swag-openapi3 is found
    env = os.environ.copy()
    go_bin = str(Path.home() / "go" / "bin")
    env["PATH"] = go_bin + ":" + env.get("PATH", "")

    print(f"  🔧 Linking local SDK in {worktree_dir.name}...")
    result = subprocess.run(
        ["make", "sdk"],
        cwd=str(go_backend),
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    if result.returncode != 0:
        # Non-fatal — log and continue; agent can fall back to published package
        print(f"  ⚠️  make sdk failed in {worktree_dir.name} (non-fatal):")
        print(result.stderr[-500:] if result.stderr else result.stdout[-500:])
    else:
        print(f"  ✅ Local SDK linked in {worktree_dir.name}")


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

    # NEVER delete a worktree with uncommitted changes — even with force=True.
    # A generator may be actively writing files that haven't been committed yet.
    if worktree_dir.exists():
        _dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(worktree_dir),
            capture_output=True, text=True, timeout=10,
        )
        _sub_dirty = subprocess.run(
            ["git", "submodule", "foreach", "--quiet",
             "git", "status", "--porcelain"],
            cwd=str(worktree_dir),
            capture_output=True, text=True, timeout=15,
        )
        if _dirty.stdout.strip() or _sub_dirty.stdout.strip():
            return False  # Preserve — uncommitted work in progress

    # Rescue submodule objects before destroying the worktree.
    # Worktree submodules have their own object store under
    # .git/worktrees/<wt>/modules/<name>/ — removing the worktree
    # deletes those objects, breaking submodule pointer references.
    try:
        _rescue_submodule_objects(project_dir, branch_name)
    except Exception:
        pass  # Best-effort — don't block cleanup on rescue failure

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


def get_resolver_config(config: dict | None) -> tuple[bool, str]:
    """Read LLM conflict-resolver settings from the harness config.

    Returns (enable_llm_resolver, model). Defaults: enabled, haiku-4-5.
    Config shape:
        parallel:
          auto_resolver:
            llm_agent: true              # default true
            llm_agent_model: claude-haiku-4-5
    """
    if not config:
        return True, "claude-haiku-4-5"
    parallel_cfg = config.get("parallel", {}) or {}
    resolver_cfg = parallel_cfg.get("auto_resolver", {}) or {}
    enabled = resolver_cfg.get("llm_agent", True)
    model = resolver_cfg.get("llm_agent_model", "claude-haiku-4-5")
    return bool(enabled), str(model)


def merge_worktree(
    project_dir: Path,
    branch_name: str,
    *,
    enable_llm_resolver: bool = True,
    llm_model: str = "claude-haiku-4-5",
) -> dict:
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
        merged = result.stdout + result.stderr

        # Try to auto-resolve submodule conflicts.
        # Pattern: "CONFLICT (submodule): Merge conflict in <name>"
        # Fix: merge the branch's submodule commit into the main submodule,
        # then stage the resolved pointer.
        if "CONFLICT (submodule)" in merged:
            resolved = _auto_resolve_submodule_conflicts(
                project_dir, branch_name, merged,
                enable_llm_resolver=enable_llm_resolver,
                llm_model=llm_model,
            )
            if resolved:
                # Check if all conflicts are resolved
                status = subprocess.run(
                    ["git", "diff", "--name-only", "--diff-filter=U"],
                    cwd=project_dir, capture_output=True, text=True,
                )
                if not status.stdout.strip():
                    # All conflicts resolved — commit the merge
                    subprocess.run(
                        ["git", "commit", "--no-edit"],
                        cwd=project_dir, capture_output=True, text=True,
                    )
                    return {"success": True, "conflict": False, "error": ""}

        # Could not auto-resolve — abort
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        return {
            "success": False,
            "conflict": True,
            "error": merged,
        }

    return {
        "success": False,
        "conflict": False,
        "error": result.stderr or result.stdout,
    }


def _try_resolve_json_union(file_path: Path) -> bool:
    """Resolve a JSON merge conflict by taking the union of keys from both sides.

    Used for additive i18n message JSONs (apps/*/messages/*.json) where two
    branches add different keys to the same object. If the same key appears
    on both sides with the same value, the duplicate is dropped. If the same
    key appears with different values, returns False (manual resolution needed).

    Returns True if the file is now valid JSON with no conflict markers.
    """
    import json
    import re
    try:
        text = file_path.read_text()
    except OSError:
        return False
    if "<<<<<<< " not in text:
        return True  # nothing to resolve

    MARKER_HEAD = "<<<<<<< "
    MARKER_SEP = "======="
    MARKER_END = ">>>>>>> "
    key_re = re.compile(r'\s*"([^"]+)"\s*:\s*("(?:[^"\\]|\\.)*"|[^,]+?)\s*,?\s*$')

    def parse_kv(line: str):
        m = key_re.match(line)
        if m:
            return m.group(1), m.group(2).rstrip(',').rstrip()
        return None, None

    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(MARKER_HEAD):
            head_block: list[str] = []
            i += 1
            while i < len(lines) and lines[i] != MARKER_SEP:
                head_block.append(lines[i])
                i += 1
            if i >= len(lines):
                return False
            i += 1  # skip ======
            other_block: list[str] = []
            while i < len(lines) and not lines[i].startswith(MARKER_END):
                other_block.append(lines[i])
                i += 1
            if i >= len(lines):
                return False
            i += 1  # skip >>>>>>>

            head_kv: dict = {}
            for ln in head_block:
                k, v = parse_kv(ln)
                if k:
                    head_kv[k] = v

            filtered_other: list[str] = []
            for ln in other_block:
                k, v = parse_kv(ln)
                if k and k in head_kv:
                    if v == head_kv[k]:
                        continue  # identical duplicate — drop
                    else:
                        return False  # value conflict — manual resolution
                filtered_other.append(ln)

            if head_block:
                last = head_block[-1].rstrip()
                if not last.endswith(','):
                    if last.endswith('"') or last.endswith('}') or re.search(r'[0-9]$|true$|false$|null$', last):
                        head_block[-1] = last + ","
                out.extend(head_block)
                if not filtered_other and out and out[-1].rstrip().endswith(','):
                    out[-1] = out[-1].rstrip()[:-1]
                out.extend(filtered_other)
            else:
                out.extend(filtered_other)
        else:
            out.append(line)
            i += 1

    new_text = "\n".join(out)
    try:
        json.loads(new_text)
    except Exception:
        return False
    file_path.write_text(new_text)
    return True


def _try_auto_resolve_inner_conflicts(
    submodule_dir: Path,
    *,
    enable_llm: bool = True,
    llm_model: str = "claude-haiku-4-5",
) -> bool:
    """Try to auto-resolve file-level conflicts left after `git merge` inside
    a submodule.

    Tier 1 (mechanical): JSON key union for additive i18n messages.
    Tier 2 (LLM):       Haiku conflict resolver for everything else (enable_llm).

    Returns True if ALL conflicted files are resolved, False otherwise.
    """
    status = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=submodule_dir, capture_output=True, text=True,
    )
    conflict_files = [
        f.strip() for f in status.stdout.splitlines() if f.strip()
    ]
    if not conflict_files:
        return True  # nothing to resolve

    # --- Tier 1: mechanical rules -------------------------------------------
    remaining: list[str] = []
    for rel in conflict_files:
        f = submodule_dir / rel
        if "/messages/" in rel and rel.endswith(".json"):
            if _try_resolve_json_union(f):
                subprocess.run(
                    ["git", "add", rel],
                    cwd=submodule_dir, capture_output=True, text=True,
                )
                continue
        remaining.append(rel)

    if not remaining:
        return True

    if not enable_llm:
        return False

    # --- Tier 2: LLM resolver ------------------------------------------------
    try:
        from .conflict_resolver import try_llm_resolve_conflicts
    except ImportError:
        return False

    try:
        resolved, unresolved = try_llm_resolve_conflicts(
            submodule_dir, remaining, model=llm_model,
        )
    except Exception:
        return False

    for rel in resolved:
        subprocess.run(
            ["git", "add", rel],
            cwd=submodule_dir, capture_output=True, text=True,
        )

    return not unresolved


def _auto_resolve_submodule_conflicts(
    project_dir: Path,
    branch_name: str,
    merge_output: str,
    *,
    enable_llm_resolver: bool = True,
    llm_model: str = "claude-haiku-4-5",
) -> bool:
    """Auto-resolve submodule merge conflicts by merging inside each submodule.

    When two branches update a submodule pointer to different commits, git
    can't auto-merge. The fix is:
    1. Find the branch's submodule commit from the merge tree
    2. Merge it into the submodule (which is at main's pointer)
    3. Resolve any inner file conflicts via known patterns (JSON union)
    4. Stage the resolved submodule pointer in the meta-repo

    Returns True if all submodule conflicts were resolved.
    """
    import re
    # Extract conflicted submodule names from merge output
    # Pattern: "CONFLICT (submodule): Merge conflict in go-backend"
    conflicts = re.findall(r"CONFLICT \(submodule\): Merge conflict in (\S+)", merge_output)
    if not conflicts:
        return False

    all_resolved = True
    for sm_name in conflicts:
        sm_dir = project_dir / sm_name
        if not sm_dir.is_dir():
            all_resolved = False
            continue

        # Get the branch's submodule pointer
        branch_ptr = subprocess.run(
            ["git", "ls-tree", branch_name, "--", sm_name],
            cwd=project_dir, capture_output=True, text=True,
        )
        if branch_ptr.returncode != 0 or not branch_ptr.stdout.strip():
            all_resolved = False
            continue

        # Parse: "160000 commit <sha>\t<name>"
        parts = branch_ptr.stdout.strip().split()
        if len(parts) < 3:
            all_resolved = False
            continue
        branch_commit = parts[2]

        # Merge the branch's commit into the submodule
        merge_result = subprocess.run(
            ["git", "merge", "--no-ff", branch_commit, "--no-edit"],
            cwd=sm_dir, capture_output=True, text=True,
        )

        if merge_result.returncode != 0:
            # Inner merge produced conflicts — try auto-resolving known patterns
            # (Tier 1: JSON union) then fall back to the LLM resolver (Tier 2).
            if _try_auto_resolve_inner_conflicts(
                sm_dir, enable_llm=enable_llm_resolver, llm_model=llm_model,
            ):
                # Commit the resolved merge inside the submodule
                commit_result = subprocess.run(
                    ["git", "commit", "--no-verify", "--no-edit"],
                    cwd=sm_dir, capture_output=True, text=True,
                )
                if commit_result.returncode != 0:
                    # Could not commit even after resolution — abort
                    subprocess.run(
                        ["git", "merge", "--abort"], cwd=sm_dir,
                        capture_output=True, text=True,
                    )
                    all_resolved = False
                    continue
            else:
                # Could not auto-resolve all inner conflicts — abort and check
                # whether the branch commit is already an ancestor (no-op merge).
                subprocess.run(
                    ["git", "merge", "--abort"], cwd=sm_dir,
                    capture_output=True, text=True,
                )
                ancestor_check = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", branch_commit, "HEAD"],
                    cwd=sm_dir, capture_output=True, text=True,
                )
                if ancestor_check.returncode != 0:
                    # Real conflict inside submodule — can't auto-resolve
                    all_resolved = False
                    continue

        # Stage the resolved submodule pointer in the meta-repo
        subprocess.run(
            ["git", "add", sm_name],
            cwd=project_dir, capture_output=True, text=True,
        )

    return all_resolved


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
