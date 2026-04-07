"""
ControlPlaneClient — Optional HTTP client for Harness v2 Control Plane.

Env-gated: only active when HARNESS_CONTROL_PLANE_URL is set.
All HTTP calls are non-blocking (daemon threads, fire-and-forget).
All calls are wrapped in try/except — never affects harness behavior.
Uses stdlib only: os, json, threading, urllib.

Offline buffering: when the control plane is down, events are spooled to
.harness/event_buffer.jsonl. A drain thread replays them when the control
plane comes back online.
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path


class ControlPlaneClient:
    def __init__(self, state_dir: Path | str | None = None):
        url = os.environ.get("HARNESS_CONTROL_PLANE_URL", "").strip()
        self.enabled = bool(url)
        self._base_url = url.rstrip("/") if url else ""
        self._api_key = os.environ.get("HARNESS_API_KEY", "")
        self.run_id: str | None = None
        self._cp_available = True  # optimistic; flipped on first failure
        self._buffer_path: Path | None = None
        self._buffer_lock = threading.Lock()
        self._drain_started = False
        if state_dir:
            p = Path(state_dir)
            p.mkdir(parents=True, exist_ok=True)
            self._buffer_path = p / "event_buffer.jsonl"

    def _post(self, path: str, body: dict) -> dict | None:
        """Synchronous HTTP POST — call from a daemon thread."""
        url = self._base_url + path
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Harness-Key": self._api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read()
                if not self._cp_available:
                    self._cp_available = True
                    self._start_drain()
                return json.loads(raw) if raw else None
        except Exception:
            self._cp_available = False
            return None

    def _buffer_event(self, path: str, body: dict) -> None:
        """Append a failed event to the local buffer file."""
        if not self._buffer_path:
            return
        entry = json.dumps({"path": path, "body": body})
        with self._buffer_lock:
            with open(self._buffer_path, "a") as f:
                f.write(entry + "\n")

    def _start_drain(self) -> None:
        """Start a background thread to replay buffered events."""
        if self._drain_started or not self._buffer_path:
            return
        self._drain_started = True
        t = threading.Thread(target=self._drain_buffer, daemon=True)
        t.start()

    def _drain_buffer(self) -> None:
        """Replay buffered events to the control plane."""
        if not self._buffer_path or not self._buffer_path.exists():
            self._drain_started = False
            return
        try:
            with self._buffer_lock:
                lines = self._buffer_path.read_text().strip().split("\n")
                self._buffer_path.unlink(missing_ok=True)
        except Exception:
            self._drain_started = False
            return

        replayed = 0
        failed_lines = []
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                result = self._post(entry["path"], entry["body"])
                if result is not None:
                    replayed += 1
                else:
                    failed_lines.append(line)
            except Exception:
                failed_lines.append(line)

        # Re-buffer any that still failed
        if failed_lines and self._buffer_path:
            with self._buffer_lock:
                with open(self._buffer_path, "a") as f:
                    for fl in failed_lines:
                        f.write(fl + "\n")

        if replayed > 0:
            print(f"  [control-plane] Drained {replayed} buffered event(s)")
        self._drain_started = False

    def _fire(self, fn):
        """Run fn in a daemon thread — fire-and-forget."""
        t = threading.Thread(target=fn, daemon=True)
        t.start()

    def _get(self, path: str) -> dict | list | None:
        """Synchronous HTTP GET."""
        url = self._base_url + path
        req = urllib.request.Request(
            url,
            headers={"X-Harness-Key": self._api_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except Exception:
            return None

    def _ensure_project(self, project_id: str, project_dir: str = "") -> None:
        """Register project if it doesn't exist yet."""
        existing = self._get(f"/api/v1/projects/{urllib.parse.quote(project_id)}")
        if existing and "id" in existing:
            return
        self._post("/api/v1/projects", {
            "id": project_id,
            "name": project_id,
            "path": project_dir,
        })

    def create_run(self, project_id: str, project_dir: str = "") -> None:
        """Register a new run with the control plane.

        Auto-registers the project if it doesn't exist yet.
        Drains any buffered events from a previous run on successful connection.
        """
        if not self.enabled:
            return

        def _do():
            try:
                self._ensure_project(project_id, project_dir)
                result = self._post("/api/v1/runs", {"project_id": project_id, "features_planned": 0})
                if result and "id" in result:
                    self.run_id = result["id"]
                    # Drain leftover events from previous run
                    if self._buffer_path and self._buffer_path.exists():
                        self._start_drain()
            except Exception:
                pass

        _do()

    def resume_run(self, project_id: str, project_dir: str = "") -> None:
        """Attach to the most recent non-terminal run instead of creating a new one.

        Looks for a 'running' or 'pending' run for this project. If found,
        reuses its ID. Falls back to create_run if none found.
        """
        if not self.enabled:
            return

        try:
            self._ensure_project(project_id, project_dir)
            runs = self._get(f"/api/v1/runs?project_id={project_id}")
            if runs:
                for run in runs:
                    if run.get("status") in ("running", "pending"):
                        self.run_id = run["id"]
                        if self._buffer_path and self._buffer_path.exists():
                            self._start_drain()
                        return
        except Exception:
            pass

        # No resumable run found — create a new one
        self.create_run(project_id, project_dir)

    def push_work_plan(self, work_plan_data: dict) -> None:
        """Push hierarchical work plan (phases/epics/stories/tasks) to control plane."""
        if not self.enabled or not self.run_id:
            return

        run_id = self.run_id
        path = f"/api/v1/runs/{run_id}/work-plan"

        def _do():
            try:
                self._post(path, work_plan_data)
            except Exception:
                pass

        self._fire(_do)

    def post_event(self, event_type: str, **kwargs) -> None:
        """Post a run event asynchronously. Buffers locally if control plane is down."""
        if not self.enabled or not self.run_id:
            return

        run_id = self.run_id
        path = f"/api/v1/runs/{run_id}/events"
        body = {"type": event_type, **kwargs}

        def _do():
            try:
                result = self._post(path, body)
                if result is None:
                    self._buffer_event(path, body)
            except Exception:
                self._buffer_event(path, body)

        self._fire(_do)

    def ingest_retro(self, retro_path: str) -> None:
        """Ingest a retrospective markdown file asynchronously."""
        if not self.enabled:
            return

        run_id = self.run_id

        def _do():
            try:
                with open(retro_path, "r", encoding="utf-8") as f:
                    content = f.read()
                self._post(
                    "/api/v1/knowledge/ingest",
                    {"run_id": run_id, "content": content},
                )
            except Exception:
                pass

        self._fire(_do)

    def query_knowledge(self, query: str, limit: int = 10, chunk_type: str = None) -> list:
        """Query knowledge base synchronously. Returns list of chunk dicts."""
        if not self.enabled:
            return []
        try:
            params = f"q={urllib.parse.quote(query)}&limit={limit}"
            if chunk_type:
                params += f"&type={chunk_type}"
            url = self._base_url + f"/api/v1/knowledge/search?{params}"
            req = urllib.request.Request(url, headers={"X-Harness-Key": self._api_key})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception:
            return []

    def get_all_chunks(self, project_id: str = None) -> list:
        """Get all knowledge chunks (no search query needed). Returns list of chunk dicts."""
        if not self.enabled:
            return []
        try:
            params = ""
            if project_id:
                params = f"?project_id={urllib.parse.quote(project_id)}"
            url = self._base_url + f"/api/v1/knowledge/chunks{params}"
            req = urllib.request.Request(url, headers={"X-Harness-Key": self._api_key})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception:
            return []
