#!/usr/bin/env python3
"""Harness status — single-shot or continuous status display.

Collects all run metrics, agent status, worktree health, and feature progress
in one pass. Outputs structured JSON for the skill to format, or human-readable
text with --pretty.

Usage:
    python3 status.py [--run RUN_ID] [--pretty] [--project PATH]
    python3 status.py --watch [--interval 5]     # continuous mode (like top/htop)
    python3 status.py -w                         # shorthand
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def find_project_root() -> Path:
    """Find the project root containing .harness/.

    Search order: CWD (walk up), then script location (walk up).
    CWD is checked first since `python3 ~/harness/.../status.py` is the
    common invocation — the script lives outside the project.
    """
    # Check CWD and its parents first
    cwd = Path.cwd().resolve()
    p = cwd
    while p != p.parent:
        if (p / ".harness").is_dir():
            return p
        p = p.parent
    # Fallback: walk up from script location
    p = Path(__file__).resolve().parent
    while p != p.parent:
        if (p / ".harness").is_dir():
            return p
        p = p.parent
    return cwd


def find_run_dir(project: Path, run_id: str | None) -> Path | None:
    runs_dir = project / ".harness" / "runs"
    if run_id:
        d = runs_dir / run_id
        return d if d.is_dir() else None
    # Latest from runs.json
    runs_json = project / ".harness" / "runs.json"
    if runs_json.exists():
        try:
            runs = json.loads(runs_json.read_text())
            if runs:
                latest = runs[-1] if isinstance(runs, list) else None
                if latest:
                    rid = latest.get("id") or latest.get("run_id", "")
                    d = runs_dir / rid
                    if d.is_dir():
                        return d
        except (json.JSONDecodeError, KeyError):
            pass
    # Fallback: single run dir
    if runs_dir.is_dir():
        dirs = sorted(runs_dir.iterdir())
        if len(dirs) == 1:
            return dirs[0]
        if dirs:
            return dirs[-1]
    # Legacy
    legacy = project / ".harness" / "state"
    if legacy.is_dir():
        return legacy
    return None


def read_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        return None


def count_features(run_dir: Path) -> dict:
    """Count features from work_plan.json and feature_list.json."""
    result = {"wp_done": 0, "wp_blocked": 0, "wp_total": 0, "fl_passing": 0, "fl_total": 0}

    # work_plan.json — recursive task count
    wp = read_json(run_dir / "work_plan.json")
    if wp:
        def count_tasks(node):
            done, blocked, total = 0, 0, 0
            for phase in node.get("phases", [node] if "epics" in node else []):
                for epic in phase.get("epics", []):
                    for story in epic.get("stories", []):
                        for task in story.get("tasks", []):
                            total += 1
                            s = task.get("status", "pending")
                            if s == "done":
                                done += 1
                            elif s == "blocked":
                                blocked += 1
            return done, blocked, total

        d, b, t = count_tasks(wp)
        result["wp_done"] = d
        result["wp_blocked"] = b
        result["wp_total"] = t

    # feature_list.json
    fl = read_json(run_dir / "feature_list.json")
    if isinstance(fl, list):
        result["fl_total"] = len(fl)
        result["fl_passing"] = sum(1 for f in fl if f.get("passes"))

    return result


def get_orchestrator() -> dict | None:
    """Find orchestrator (run.py) process."""
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "run.py"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except subprocess.CalledProcessError:
        return None
    pids = out.split()
    if not pids:
        return None
    pid = pids[0]
    try:
        ps = subprocess.check_output(
            ["ps", "-p", pid, "-o", "pid=,etime="], text=True, stderr=subprocess.DEVNULL
        ).strip()
        parts = ps.split()
        return {"pid": int(parts[0]), "elapsed": parts[1] if len(parts) > 1 else "?"}
    except (subprocess.CalledProcessError, IndexError, ValueError):
        return {"pid": int(pid), "elapsed": "?"}


def get_agents(project: Path) -> list[dict]:
    """Find claude -p processes and map to worktrees."""
    try:
        out = subprocess.check_output(
            ["bash", "-c", "ps aux | grep 'claude -p' | grep -v grep"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        return []

    agents = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 11:
            continue
        pid = parts[1]
        cpu = parts[2]
        # Get elapsed time
        try:
            ps_out = subprocess.check_output(
                ["ps", "-p", pid, "-o", "etime="], text=True, stderr=subprocess.DEVNULL
            ).strip()
        except subprocess.CalledProcessError:
            ps_out = "?"
        # Get cwd
        try:
            lsof_out = subprocess.check_output(
                ["lsof", "-p", pid], text=True, stderr=subprocess.DEVNULL
            )
            cwd = ""
            for l in lsof_out.splitlines():
                if "cwd" in l:
                    cwd = l.split()[-1]
                    break
        except subprocess.CalledProcessError:
            cwd = ""

        worker = ""
        if ".worktrees/" in cwd:
            worker = cwd.split(".worktrees/")[-1].rstrip("/")
        elif cwd.rstrip("/") == str(project).rstrip("/"):
            worker = "(main)"

        agents.append({
            "pid": int(pid),
            "cpu": cpu,
            "elapsed": ps_out,
            "cwd": cwd,
            "worker": worker,
        })

    return agents


def get_worktrees(project: Path, run_dir: Path, agents: list[dict]) -> list[dict]:
    """List worktrees with status classification."""
    try:
        out = subprocess.check_output(
            ["git", "-C", str(project), "worktree", "list", "--porcelain"],
            text=True, stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return []

    # Parse porcelain output
    worktrees_raw = []
    current = {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            if current:
                worktrees_raw.append(current)
            current = {"path": line[9:]}
        elif line.startswith("branch "):
            ref = line[7:]  # refs/heads/harness/worker-4954
            current["ref"] = ref
            # Strip refs/heads/ prefix for short name
            current["branch"] = ref.removeprefix("refs/heads/")
        elif line == "bare":
            current["bare"] = True
    if current:
        worktrees_raw.append(current)

    # Filter to harness worktrees only
    harness_wts = [w for w in worktrees_raw if w.get("branch", "").startswith("harness/")]

    # Load assignments
    assignments = read_json(run_dir / "fleet" / "worker_assignments.json") or {}
    assign_map = assignments.get("assignments", assignments) if isinstance(assignments, dict) else {}

    agent_cwds = {a["cwd"].rstrip("/") for a in agents}
    orch = get_orchestrator()

    results = []
    for wt in harness_wts:
        path = wt["path"]
        branch = wt.get("branch", "")
        worker_name = branch.replace("harness/", "") if branch.startswith("harness/") else branch

        # Count commits ahead of main
        try:
            count = subprocess.check_output(
                ["git", "-C", str(project), "rev-list", "--count", f"main..{branch}"],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
            commits = int(count)
        except (subprocess.CalledProcessError, ValueError):
            commits = 0

        # Latest commit message
        commit_msg = ""
        if commits > 0:
            try:
                commit_msg = subprocess.check_output(
                    ["git", "-C", str(project), "log", "--oneline", "-1", branch],
                    text=True, stderr=subprocess.DEVNULL,
                ).strip()
            except subprocess.CalledProcessError:
                pass

        # Assignment info
        a = assign_map.get(worker_name, {})
        feature_id = a.get("feature_id", "?")
        a_status = a.get("status", "unknown")
        a_live = a.get("live_state", "")

        # Classify — status field is the authoritative state machine value.
        # merge_failed is distinct from failed (eval-fail) so each routes differently
        # in the recovery path.
        has_agent = path.rstrip("/") in agent_cwds
        if has_agent:
            status = "active"
        elif commits > 0 and a_status == "merge_failed":
            status = "merge-failed"
        elif commits > 0 and a_status == "failed" and orch:
            # live_state may indicate eval-done (eval passed but merge pending)
            if a_live == "eval-done":
                status = "pending-merge"
            else:
                status = "eval-failed"
        elif commits > 0 and a_status in ("failed", "merge_failed") and not orch:
            status = "pending-recovery"
        elif commits > 0 and a_status in ("completed",):
            status = "completed"
        elif commits > 0:
            status = "pending-merge"
        else:
            status = "empty"

        results.append({
            "worker": worker_name,
            "status": status,
            "feature_id": feature_id,
            "commits": commits,
            "commit_msg": commit_msg,
            "assignment_status": a_status,
        })

    return results


def get_wave_info(run_dir: Path) -> dict | None:
    session = read_json(run_dir / "fleet" / "session.json")
    if not session:
        return None
    wq = session.get("work_queue", [])
    pending = sum(1 for w in wq if w.get("status") == "pending")
    complete = sum(1 for w in wq if w.get("status") == "complete")
    failed = sum(1 for w in wq if w.get("status") in ("failed", "requeued"))
    return {
        "current_wave": session.get("current_wave"),
        "total_layers": session.get("total_layers"),
        "pending": pending,
        "complete": complete,
        "failed": failed,
        "discoveries": len(session.get("discoveries", [])),
        "queue": wq,
        "completed_features": session.get("completed_features", []),
        "failed_features": session.get("failed_features", []),
    }


def get_worker_assignments(run_dir: Path) -> list[dict]:
    """Load worker assignments with computed duration."""
    data = read_json(run_dir / "fleet" / "worker_assignments.json") or {}
    assignments = data.get("assignments", {})
    results = []
    now = datetime.now(timezone.utc)
    from datetime import datetime as dt
    for worker_id, a in assignments.items():
        # Prefer live_state_at (updated on every transition) over assigned_at
        # (set once at worker creation, often hours stale on long-running workers).
        ts_field = a.get("live_state_at") or a.get("assigned_at", "")
        duration = ""
        if ts_field:
            try:
                t = dt.fromisoformat(ts_field)
                delta = now - t
                mins = int(delta.total_seconds() / 60)
                secs = int(delta.total_seconds() % 60)
                duration = f"{mins}m {secs}s" if mins else f"{secs}s"
            except (ValueError, TypeError):
                duration = "?"
        results.append({
            "worker": worker_id,
            "feature_id": a.get("feature_id", "?"),
            "status": a.get("status", "?"),
            "phase": a.get("phase", "generator"),
            # live_state is the real-time state (gen-running, eval-done, merging, ...)
            # phase is the orchestrator's "intent" — what role was last dispatched
            "live_state": a.get("live_state", ""),
            "scope": a.get("scope", []),
            "duration": duration,
            "agent_id": a.get("agent_id", "?"),
        })
    return results


def get_phase_progress(run_dir: Path) -> list[dict]:
    """Break down task counts per phase."""
    wp = read_json(run_dir / "work_plan.json")
    if not wp:
        return []
    phases = []
    for phase in wp.get("phases", []):
        done = 0
        blocked = 0
        pending = 0
        total = 0
        for epic in phase.get("epics", []):
            for story in epic.get("stories", []):
                for task in story.get("tasks", []):
                    total += 1
                    s = task.get("status", "pending")
                    if s == "done":
                        done += 1
                    elif s == "blocked":
                        blocked += 1
                    else:
                        pending += 1
        phases.append({
            "id": phase.get("id", "?"),
            "name": phase.get("name", "?"),
            "done": done,
            "blocked": blocked,
            "pending": pending,
            "total": total,
        })
    return phases


def get_circuit_breaker(run_dir: Path) -> dict | None:
    cb = read_json(run_dir / "circuit_breaker.json")
    if not cb:
        return None
    return {"state": cb.get("state", "?"), "total_opens": cb.get("total_opens", 0)}


def fmt_num(n: int | float) -> str:
    return f"{int(n):,}"


def fmt_ms(ms: int | float) -> str:
    s = int(ms / 1000)
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {sec}s"
    return f"{m}m {sec}s"


def _bar(done: int, total: int, width: int = 20) -> str:
    """Render a simple progress bar."""
    if total == 0:
        return "[" + " " * width + "]"
    filled = int(width * done / total)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _trunc(s: str, maxlen: int) -> str:
    return s if len(s) <= maxlen else s[:maxlen - 1] + "~"


def pretty_print(data: dict):
    """Human-readable status output."""
    run_id = data.get("run_id", "?")
    state = data.get("state", {})
    cb = state.get("cost_breakdown", {})

    iteration = state.get("iteration", 0)
    drain = data.get("draining", False)

    # Header
    orch = data.get("orchestrator")
    orch_str = f"PID {orch['pid']} up {orch['elapsed']}" if orch else "NOT RUNNING"
    print(f"=== {run_id} === iter {iteration} | orchestrator: {orch_str}{' (DRAINING)' if drain else ''}")
    print()

    # ── Cost & Tokens (compact) ──────────────────────────
    total_cost = state.get("total_cost_usd", 0)
    gen_cost = cb.get("generator", 0)
    eval_cost = cb.get("evaluator", 0)
    turns = cb.get("total_turns", 0)
    api_ms = cb.get("total_api_ms", 0)
    cr = cb.get("cache_read_tokens", 0)
    cc = cb.get("cache_creation_tokens", 0)
    hit = int(cr / (cr + cc) * 100) if (cr + cc) else 0
    api_str = f" | API {fmt_ms(api_ms)}" if api_ms else ""
    print(f"Cost: ${total_cost:.2f} (gen ${gen_cost:.2f} / eval ${eval_cost:.2f}) | {fmt_num(turns)} turns | cache {hit}%{api_str}")
    print()

    # ── Phase Progress ───────────────────────────────────
    phases = data.get("phase_progress", [])
    if phases:
        print("Phases:")
        for p in phases:
            bar = _bar(p["done"], p["total"])
            blocked_str = f" ({p['blocked']} blocked)" if p["blocked"] else ""
            print(f"  {p['id']:10s} {bar} {p['done']:3d}/{p['total']:3d}  {p['name']}{blocked_str}")
        total_done = sum(p["done"] for p in phases)
        total_all = sum(p["total"] for p in phases)
        pct = int(total_done / total_all * 100) if total_all else 0
        print(f"  {'TOTAL':10s} {_bar(total_done, total_all)} {total_done:3d}/{total_all:3d}  ({pct}%)")
        print()

    # ── Active Workers ───────────────────────────────────
    # A worker is "active" if its live_state indicates an in-flight phase.
    # Don't filter on `status` alone — `status="failed"` can coexist with an
    # actively-running agent when the orchestrator is retrying (e.g., after
    # a merge failure the status is "failed" but a fresh evaluator is running).
    ACTIVE_LIVE_STATES = {
        "assigned", "gen-running", "gen-done",
        "eval-running", "eval-done", "merging",
    }
    workers = sorted(data.get("worker_assignments", []), key=lambda w: w.get("worker", ""))
    running = [
        w for w in workers
        if w.get("live_state") in ACTIVE_LIVE_STATES
        or (not w.get("live_state") and w.get("status") == "running")
    ]
    if running:
        print(f"Active Workers: {len(running)}")
        for w in running:
            # Prefer real-time live_state when available; fall back to phase
            live = w.get("live_state", "")
            if live:
                # Compact display: gen-running → GEN, eval-running → EVAL,
                # gen-done → GEN✓, eval-done → EVAL✓, merging → MERGE, etc.
                tag_map = {
                    "assigned": "INIT",
                    # Generator lifecycle
                    "gen-running": "GEN ", "gen-done": "GEN✓",
                    "gen-timeout": "GEN!",
                    "gen-failed-with-commits": "GFAIL",
                    "gen-failed-no-commits": "GNULL",
                    # Evaluator lifecycle
                    "eval-running": "EVAL ", "eval-done": "EVAL✓",
                    "eval-timeout": "EVAL!",
                    # Eval failure variants — distinct remediation strategies
                    "eval-failed-retrying": "RETRY",   # has budget, retrying same worktree
                    "eval-failed-blocked": "BLOCK",    # exhausted retries
                    "eval-failed-recovering": "RECOV", # dispatched a recovery generator
                    # Merge lifecycle
                    "merging": "MERGE", "merge-success": "MRG✓",
                    "merge-conflict": "CONF", "merge-failed": "MRG✗",
                }
                phase_tag = tag_map.get(live, live[:5].upper())
            else:
                phase_tag = "GEN" if w.get("phase", "generator") == "generator" else "EVAL"
            scope_str = ", ".join(s.split("/")[-2] + "/" if s.endswith("/") else s.split("/")[-1] for s in w["scope"][:3])
            if len(w["scope"]) > 3:
                scope_str += f" +{len(w['scope'])-3}"
            print(f"  {w['worker']:14s} {w['feature_id']:10s} {phase_tag:5s} {w['duration']:>8s}  [{scope_str}]")
    else:
        print("Active Workers: none")

    # OS-level agents (sorted by worker to match active workers order)
    agents = sorted(data.get("agents", []), key=lambda a: a.get("worker", ""))
    if agents:
        for a in agents:
            w = a.get("worker", "?")
            print(f"    pid {a['pid']}  {w}  {a['elapsed']}  {a['cpu']}% CPU")
    print()

    # ── Wave Queue ───────────────────────────────────────
    wave = data.get("wave")
    if wave:
        cw = wave.get("current_wave", "?")
        tl = wave.get("total_layers", "?")
        print(f"Wave {cw}/{tl}: {wave['complete']} done, {wave['pending']} pending, {wave['failed']} failed")

        queue = sorted(wave.get("queue", []), key=lambda q: q.get("feature_id", ""))
        if queue:
            # Cross-reference with worker assignments to detect running tasks
            running_fids = {w["feature_id"] for w in workers if w["status"] == "running"}

            # Group by status (promote assigned-pending to running)
            q_pending = [q for q in queue if q.get("status") == "pending" and q["feature_id"] not in running_fids]
            q_running = [q for q in queue if q.get("status") not in ("pending", "complete", "failed", "requeued")
                         or q["feature_id"] in running_fids]
            q_done = [q for q in queue if q.get("status") == "complete"]
            q_failed = [q for q in queue if q.get("status") in ("failed", "requeued")]

            if q_running:
                for q in q_running:
                    print(f"    > {q['feature_id']:10s} RUNNING   {_trunc(q.get('desc', ''), 60)}")
            if q_pending:
                for q in q_pending[:5]:
                    print(f"    . {q['feature_id']:10s} pending   {_trunc(q.get('desc', ''), 60)}")
                if len(q_pending) > 5:
                    print(f"    ... and {len(q_pending) - 5} more pending")
            if q_failed:
                for q in q_failed:
                    print(f"    x {q['feature_id']:10s} FAILED    {_trunc(q.get('desc', ''), 60)}")
            if q_done:
                # Show last 3 completed
                recent = q_done[-3:]
                for q in recent:
                    print(f"    + {q['feature_id']:10s} done      {_trunc(q.get('desc', ''), 60)}")
                if len(q_done) > 3:
                    print(f"    ... {len(q_done) - 3} more completed")
        print()

    # ── Worktrees ────────────────────────────────────────
    wts = sorted(data.get("worktrees", []), key=lambda w: w.get("worker", ""))
    if wts:
        # Build worker->phase lookup from assignments
        phase_map = {w["worker"]: w.get("phase", "generator") for w in workers}
        print("Worktrees:")
        icons = {"active": ">", "eval-failed": "x", "merge-failed": "M",
                 "pending-recovery": "?", "pending-merge": "~",
                 "completed": "+", "empty": "."}
        for w in wts:
            icon = icons.get(w["status"], "?")
            msg = f"{w['commits']}c" if w["commits"] else "0c"
            feat_str = w.get("feature_id", "?")
            phase = phase_map.get(w["worker"], "")
            phase_str = f" ({phase})" if phase and w["status"] == "active" else ""
            cm = f"  {w['commit_msg']}" if w.get("commit_msg") else ""
            print(f"  {icon} {w['worker']:14s} {w['status']:16s} {feat_str:10s} {msg}{phase_str}{cm}")
        print()

    # ── Recently Completed ───────────────────────────────
    if wave:
        completed = wave.get("completed_features", [])
        if completed:
            recent = completed[-10:]
            print(f"Recently Completed ({len(completed)} total): {', '.join(recent)}")
            print()

        failed_feats = wave.get("failed_features", [])
        if failed_feats:
            print(f"Failed Features: {', '.join(failed_feats)}")
            print()

    # ── Circuit Breaker ──────────────────────────────────
    cb_info = data.get("circuit_breaker")
    if cb_info and cb_info.get("total_opens", 0) > 0:
        print(f"Circuit Breaker: {cb_info['state']} ({cb_info['total_opens']} opens)")


def _gather_and_display(project: Path, run_id: str = None, pretty: bool = True):
    """Gather status data and display. Returns True if orchestrator is running."""
    run_dir = find_run_dir(project, run_id)

    if not run_dir:
        if pretty:
            print("No harness runs found.")
        else:
            json.dump({"error": "No harness runs found"}, sys.stdout)
        return False

    state = read_json(run_dir / "state.json") or {}
    features = count_features(run_dir)
    agents = get_agents(project)
    worktrees = get_worktrees(project, run_dir, agents)
    orchestrator = get_orchestrator()
    wave = get_wave_info(run_dir)
    circuit_breaker = get_circuit_breaker(run_dir)
    worker_assignments = get_worker_assignments(run_dir)
    phase_progress = get_phase_progress(run_dir)
    draining = (run_dir / "drain").exists()

    data = {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "draining": draining,
        "state": state,
        "features": features,
        "agents": agents,
        "worktrees": worktrees,
        "orchestrator": orchestrator,
        "wave": wave,
        "circuit_breaker": circuit_breaker,
        "worker_assignments": worker_assignments,
        "phase_progress": phase_progress,
    }

    if pretty:
        pretty_print(data)
    else:
        json.dump(data, sys.stdout, indent=2, default=str)

    return (orchestrator or {}).get("running", False)


def main():
    parser = argparse.ArgumentParser(description="Harness status gatherer")
    parser.add_argument("--run", help="Run ID to inspect")
    parser.add_argument("--pretty", action="store_true", help="Human-readable output")
    parser.add_argument("--project", help="Project root path")
    parser.add_argument("-w", "--watch", action="store_true",
                        help="Continuous mode — refresh like top/htop. Ctrl+C to stop.")
    parser.add_argument("--interval", type=int, default=1,
                        help="Refresh interval in seconds for --watch (default: 1)")
    args = parser.parse_args()

    project = Path(args.project) if args.project else find_project_root()

    if args.watch:
        # Continuous mode — uses the alternate screen buffer (like top/htop/vim/less).
        # This swaps to a fresh "page" while running and restores the user's terminal
        # content on exit. No scrollback pollution, no flicker, no leftover-line tracking.
        import io
        try:
            # Enter alternate screen buffer + hide cursor
            sys.stdout.write("\033[?1049h\033[?25l")
            sys.stdout.flush()
            while True:
                # Render to buffer first so the clear+write happens in one flush
                # (avoids visible flicker on slow renders).
                buf = io.StringIO()
                _old_stdout = sys.stdout
                sys.stdout = buf
                print(f"[watch — every {args.interval}s, Ctrl+C to stop]  "
                      f"{datetime.now().strftime('%H:%M:%S')}\n")
                is_running = _gather_and_display(project, args.run, pretty=True)
                if not is_running:
                    print("\nOrchestrator not running. Watching for restart...")
                sys.stdout = _old_stdout

                # Clear screen + home cursor + write full content in one write call
                _old_stdout.write("\033[2J\033[H" + buf.getvalue())
                _old_stdout.flush()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass
        finally:
            # Restore cursor + leave alternate screen (returns user to their original terminal)
            sys.stdout.write("\033[?25h\033[?1049l")
            sys.stdout.flush()
            print("Watch stopped.")
    else:
        result = _gather_and_display(project, args.run, pretty=args.pretty)
        if not result and not args.pretty:
            sys.exit(1)


if __name__ == "__main__":
    main()
