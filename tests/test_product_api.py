"""Tests for the product-facing HTTP API."""

import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from src.core.run_registry import RunRegistry
from src.product.api import ProductApiApplication
from src.product.services.request_service import RequestService


class FakeLaunchService:
    def __init__(self):
        self.calls = []
        self.reconciled = 0

    def reconcile(self, request_service):
        self.reconciled += 1
        return []

    def launch_request_run(self, request_id, *, config_path=None, overrides=None):
        self.calls.append(
            {
                "config_path": config_path,
                "request_id": request_id,
                "overrides": overrides,
            }
        )
        return {
            "id": "launch-123",
            "request_id": request_id,
            "status": "running",
            "log_path": "/tmp/launch-123.log",
        }


class FakeHandler:
    def __init__(self, method: str, path: str, payload: dict | None = None):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.command = method
        self.path = path
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status_code = None
        self.response_headers = {}

    def send_response(self, code: int, message: str | None = None):
        self.status_code = code

    def send_header(self, key: str, value: str):
        self.response_headers[key] = value

    def end_headers(self):
        return


def _dispatch_json(app: ProductApiApplication, method: str, path: str, payload: dict | None = None):
    handler = FakeHandler(method, path, payload)
    app._handle_request(handler)
    body = handler.wfile.getvalue().decode("utf-8")
    return handler.status_code, json.loads(body)


def _dispatch_text(app: ProductApiApplication, method: str, path: str, payload: dict | None = None):
    handler = FakeHandler(method, path, payload)
    app._handle_request(handler)
    return handler.status_code, handler.response_headers, handler.wfile.getvalue().decode("utf-8")


def test_product_api_supports_requests_runs_and_events(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        harness_dir = project_dir / ".harness"
        harness_dir.mkdir()
        (harness_dir / "config.yaml").write_text("models: {}\n")

        fake_launch_service = FakeLaunchService()
        app = ProductApiApplication(project_dir, port=0, launch_service=fake_launch_service)

        monkeypatch.setattr("src.product.api.maybe_detect_control_plane", lambda: None)

        status, health = _dispatch_json(app, "GET", "/health")
        assert status == 200
        assert health == {"status": "ok"}

        status, created = _dispatch_json(
            app,
            "POST",
            "/api/requests",
            {
                "title": "Build auth",
                "description": "Add auth flow",
                "acceptance_criteria": ["Login works"],
                "linked_paths": ["docs/spec.md"],
            },
        )
        assert status == 201
        request_id = created["id"]

        status, request_record = _dispatch_json(app, "GET", f"/api/requests/{request_id}")
        assert status == 200
        assert request_record["title"] == "Build auth"

        status, context = _dispatch_json(app, "GET", f"/api/requests/{request_id}/context")
        assert status == 200
        assert context["request_id"] == request_id

        status, run_result = _dispatch_json(
            app,
            "POST",
            f"/api/requests/{request_id}/runs",
            {"max_cost": 7, "model": "gpt-5.4"},
        )
        assert status == 202
        assert run_result["launch_id"] == "launch-123"
        assert run_result["status"] == "running"
        assert fake_launch_service.calls[0]["overrides"] == {
            "max_cost_usd": 7.0,
            "model": "gpt-5.4",
        }

        service = RequestService(project_dir)
        updated = service.get_request(request_id)
        assert updated["status"] == "running"
        assert updated["metadata"]["active_launch_id"] == "launch-123"
        assert updated["metadata"]["launch_log_path"] == "/tmp/launch-123.log"

        registry = RunRegistry(harness_dir)
        run_id = registry.create_run("api detail")
        run_dir = registry.run_dir(run_id)
        (run_dir / "logs").mkdir()
        (run_dir / "logs" / "run.jsonl").write_text(
            json.dumps({"ts": "2026-04-08T10:00:00+00:00", "event": "step", "context": "planner"})
            + "\n"
        )

        status, runs = _dispatch_json(app, "GET", "/api/runs")
        assert status == 200
        assert any(run["run_id"] == run_id for run in runs)

        status, run_detail = _dispatch_json(app, "GET", f"/api/runs/{run_id}")
        assert status == 200
        assert run_detail["run_id"] == run_id

        status, events = _dispatch_json(app, "GET", f"/api/runs/{run_id}/events")
        assert status == 200
        assert events[0]["phase"] == "planner"
        assert fake_launch_service.reconciled >= 3


def test_product_api_serves_static_shell_and_assets():
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        app = ProductApiApplication(project_dir, port=0)

        status, headers, html = _dispatch_text(app, "GET", "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert headers["Cache-Control"] == "no-store, max-age=0"
        assert "Harness Product Prototype" in html
        assert "window.__HARNESS_PRODUCT_CONFIG__" in html
        assert project_dir.name in html

        status, headers, js = _dispatch_text(app, "GET", "/app.js")
        assert status == 200
        assert headers["Content-Type"] in {"text/javascript", "application/javascript"}
        assert headers["Cache-Control"] == "no-store, max-age=0"
        assert "renderRequestsView" in js

        status, headers, css = _dispatch_text(app, "GET", "/styles.css")
        assert status == 200
        assert headers["Content-Type"] == "text/css"
        assert headers["Cache-Control"] == "no-store, max-age=0"
        assert "--accent" in css


def test_product_api_uses_non_threaded_http_server():
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        app = ProductApiApplication(project_dir, port=0)
        captured = {}

        class FakeServer:
            def __init__(self, address, handler_cls):
                captured["address"] = address
                captured["handler_cls"] = handler_cls

        with patch("src.product.api.HTTPServer", FakeServer):
            server = app.create_server()

        assert isinstance(server, FakeServer)
        assert captured["address"][1] == 0
        assert captured["handler_cls"].__name__ == "Handler"


def test_product_api_returns_404_for_missing_request():
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        app = ProductApiApplication(project_dir, port=0)
        status, body = _dispatch_json(app, "GET", "/api/requests/req-missing")
        assert status == 404
        assert body["error"] == "Request not found"


def test_product_api_rejects_duplicate_start_for_running_request():
    with tempfile.TemporaryDirectory() as d:
        project_dir = Path(d)
        harness_dir = project_dir / ".harness"
        harness_dir.mkdir()
        (harness_dir / "config.yaml").write_text("models: {}\n")

        app = ProductApiApplication(project_dir, port=0, launch_service=FakeLaunchService())
        request_service = RequestService(project_dir)
        record = request_service.create_request(
            project_id="demo",
            title="Build auth",
            description="Add auth flow",
            metadata={"active_launch_id": "launch-123"},
        )
        request_service.update_request(record["id"], status="running", metadata={"active_launch_id": "launch-123"})

        status, body = _dispatch_json(app, "POST", f"/api/requests/{record['id']}/runs", {})
        assert status == 409
        assert body["error"] == "Request already has a run in progress."
