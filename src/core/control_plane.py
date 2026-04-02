"""
ControlPlaneClient — Optional HTTP client for Harness v2 Control Plane.

Env-gated: only active when HARNESS_CONTROL_PLANE_URL is set.
All HTTP calls are non-blocking (daemon threads, fire-and-forget).
All calls are wrapped in try/except — never affects harness behavior.
Uses stdlib only: os, json, threading, urllib.
"""
import json
import os
import threading
import urllib.parse
import urllib.request


class ControlPlaneClient:
    def __init__(self):
        url = os.environ.get("HARNESS_CONTROL_PLANE_URL", "").strip()
        self.enabled = bool(url)
        self._base_url = url.rstrip("/") if url else ""
        self._api_key = os.environ.get("HARNESS_API_KEY", "")
        self.run_id: str | None = None

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
                return json.loads(raw) if raw else None
        except Exception:
            return None

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
        """
        if not self.enabled:
            return

        def _do():
            try:
                self._ensure_project(project_id, project_dir)
                result = self._post("/api/v1/runs", {"project_id": project_id, "features_planned": 0})
                if result and "id" in result:
                    self.run_id = result["id"]
            except Exception:
                pass

        _do()

    def post_event(self, event_type: str, **kwargs) -> None:
        """Post a run event asynchronously."""
        if not self.enabled or not self.run_id:
            return

        run_id = self.run_id

        def _do():
            try:
                self._post(
                    f"/api/v1/runs/{run_id}/events",
                    {"event_type": event_type, **kwargs},
                )
            except Exception:
                pass

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
