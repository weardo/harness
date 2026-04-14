"""Stdlib HTTP API for the early Harness product prototype."""

from __future__ import annotations

import argparse
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

try:
    from product.launch import (
        build_execution_overrides,
        load_project_env,
        maybe_detect_control_plane,
        resolve_config_path,
    )
    from product.services.context_assembler import ContextAssembler
    from product.services.run_launch_service import RunLaunchService
    from product.services.request_service import RequestService
    from product.services.run_query_service import RunQueryService
    from product.services.run_service import RunService
except ImportError:  # pragma: no cover - package import path under pytest
    from src.product.launch import (
        build_execution_overrides,
        load_project_env,
        maybe_detect_control_plane,
        resolve_config_path,
    )
    from src.product.services.context_assembler import ContextAssembler
    from src.product.services.run_launch_service import RunLaunchService
    from src.product.services.request_service import RequestService
    from src.product.services.run_query_service import RunQueryService
    from src.product.services.run_service import RunService


class ProductApiApplication:
    """Small local API for requests, context, runs, and run events."""

    def __init__(
        self,
        project_dir: Path,
        *,
        host: str = "127.0.0.1",
        port: int = 7856,
        request_service_cls: type[RequestService] = RequestService,
        context_assembler: Optional[ContextAssembler] = None,
        run_service: Optional[RunService] = None,
        run_query_service_cls: type[RunQueryService] = RunQueryService,
        launch_service: Optional[RunLaunchService] = None,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.host = host
        self.port = port
        self.request_service = request_service_cls(self.project_dir)
        self.context_assembler = context_assembler or ContextAssembler()
        self.run_service = run_service or RunService()
        self.run_query_service = run_query_service_cls(self.project_dir)
        self.launch_service = launch_service or RunLaunchService(self.project_dir)
        self.static_dir = Path(__file__).resolve().parent / "static"

    def create_server(self) -> HTTPServer:
        """Create a configured HTTP server for this application.

        The prototype intentionally uses a non-threaded server because the
        current Harness orchestrator installs signal handlers during run
        execution, and Python only allows that on the main thread.
        """
        app = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                app._handle_request(self)

            def do_POST(self) -> None:  # noqa: N802
                app._handle_request(self)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A003
                return

        return HTTPServer((self.host, self.port), Handler)

    def serve_forever(self) -> None:
        """Run the API until interrupted."""
        server = self.create_server()
        address = server.server_address
        print(f"Harness product API listening on http://{address[0]}:{address[1]}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()

    def _handle_request(self, handler: BaseHTTPRequestHandler) -> None:
        method = handler.command.upper()
        parsed = urlparse(handler.path)
        path = parsed.path.rstrip("/") or "/"
        segments = [segment for segment in path.split("/") if segment]
        query = parse_qs(parsed.query)

        try:
            if method == "GET" and path == "/":
                return self._serve_index(handler)
            if method == "GET" and self._is_static_asset_path(path):
                return self._serve_static_asset(handler, path)
            if method == "GET" and path == "/health":
                return self._send_json(handler, HTTPStatus.OK, {"status": "ok"})
            if segments[:2] == ["api", "requests"]:
                return self._handle_requests(handler, method, segments)
            if segments[:2] == ["api", "runs"]:
                return self._handle_runs(handler, method, segments, query)
            return self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except ValueError as exc:
            return self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive API guard
            return self._send_json(
                handler,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"Internal server error: {exc}"},
            )

    def _handle_requests(
        self,
        handler: BaseHTTPRequestHandler,
        method: str,
        segments: list[str],
    ) -> None:
        self.launch_service.reconcile(self.request_service)
        if len(segments) == 2:
            if method == "GET":
                return self._send_json(handler, HTTPStatus.OK, self.request_service.list_requests())
            if method == "POST":
                payload = self._read_json_body(handler)
                record = self.request_service.create_request(
                    project_id=str(payload.get("project_id") or self.project_dir.name),
                    title=str(payload["title"]),
                    description=str(payload["description"]),
                    source=str(payload.get("source") or "manual"),
                    priority=str(payload.get("priority") or "normal"),
                    acceptance_criteria=list(payload.get("acceptance_criteria") or []),
                    linked_paths=list(payload.get("linked_paths") or []),
                    metadata=dict(payload.get("metadata") or {}),
                )
                return self._send_json(handler, HTTPStatus.CREATED, record)

        if len(segments) >= 3:
            request_id = segments[2]
            record = self.request_service.get_request(request_id)
            if record is None:
                return self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "Request not found"})

            if len(segments) == 3 and method == "GET":
                return self._send_json(handler, HTTPStatus.OK, record)

            if len(segments) == 4 and segments[3] == "context" and method == "GET":
                bundle = self.context_assembler.assemble(record, self.project_dir)
                return self._send_json(handler, HTTPStatus.OK, bundle)

            if len(segments) == 4 and segments[3] == "runs" and method == "POST":
                payload = self._read_json_body(handler, allow_empty=True)
                return self._start_request_run(handler, request_id, record, payload)

        return self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _handle_runs(
        self,
        handler: BaseHTTPRequestHandler,
        method: str,
        segments: list[str],
        query: dict[str, list[str]],
    ) -> None:
        self.launch_service.reconcile(self.request_service)
        if method != "GET":
            return self._send_json(handler, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Method not allowed"})

        if len(segments) == 2:
            runs = self.run_query_service.list_runs()
            limit = self._query_int(query, "limit")
            if limit is not None:
                runs = runs[:limit]
            return self._send_json(handler, HTTPStatus.OK, runs)

        run_id = segments[2]
        if len(segments) == 3:
            run = self.run_query_service.get_run(run_id)
            if run is None:
                return self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "Run not found"})
            return self._send_json(handler, HTTPStatus.OK, run)

        if len(segments) == 4 and segments[3] == "events":
            return self._send_json(handler, HTTPStatus.OK, self.run_query_service.list_run_events(run_id))

        return self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _start_request_run(
        self,
        handler: BaseHTTPRequestHandler,
        request_id: str,
        record: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        if record.get("status") == "running":
            return self._send_json(
                handler,
                HTTPStatus.CONFLICT,
                {"error": "Request already has a run in progress."},
            )

        config_path = resolve_config_path(
            self.project_dir,
            Path(payload["config"]) if payload.get("config") else None,
        )
        if config_path is None:
            return self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "Config not found"})

        overrides = build_execution_overrides(
            max_cost=self._maybe_float(payload.get("max_cost")),
            max_duration=self._maybe_int(payload.get("max_duration")),
            max_iterations=self._maybe_int(payload.get("max_iterations")),
            model=self._maybe_str(payload.get("model")),
            planner_model=self._maybe_str(payload.get("planner_model")),
        )
        metadata = dict(record.get("metadata", {}) or {})
        metadata.pop("launch_error", None)
        self.request_service.update_request(request_id, status="running", metadata=metadata)
        try:
            launch = self.launch_service.launch_request_run(
                request_id,
                config_path=config_path,
                overrides=overrides or None,
            )
        except Exception as exc:
            metadata["launch_error"] = str(exc)
            self.request_service.update_request(request_id, status="blocked", metadata=metadata)
            return self._send_json(
                handler,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"Failed to launch run: {exc}"},
            )

        metadata["active_launch_id"] = launch["id"]
        metadata["last_launch_id"] = launch["id"]
        metadata["launch_log_path"] = launch["log_path"]
        self.request_service.update_request(request_id, status="running", metadata=metadata)
        return self._send_json(
            handler,
            HTTPStatus.ACCEPTED,
            {
                "launch_id": launch["id"],
                "request_id": request_id,
                "status": "running",
                "summary": "Run launched in background.",
                "log_path": launch["log_path"],
            },
        )

    def _read_json_body(self, handler: BaseHTTPRequestHandler, *, allow_empty: bool = False) -> dict[str, Any]:
        content_length = int(handler.headers.get("Content-Length", "0") or 0)
        if content_length == 0:
            if allow_empty:
                return {}
            raise ValueError("Request body is required")
        raw = handler.rfile.read(content_length)
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON body: {exc.msg}") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _serve_index(self, handler: BaseHTTPRequestHandler) -> None:
        index_path = self.static_dir / "index.html"
        html = index_path.read_text(encoding="utf-8")
        config_script = """
<script>
  window.__HARNESS_PRODUCT_CONFIG__ = {
    apiBase: '/api',
    projectName: %s
  };
</script>
""".strip() % json.dumps(self.project_dir.name)
        injected = html.replace("</head>", f"{config_script}\n</head>")
        self._send_bytes(
            handler,
            HTTPStatus.OK,
            injected.encode("utf-8"),
            content_type="text/html; charset=utf-8",
        )

    def _serve_static_asset(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        asset_name = path.lstrip("/")
        asset_path = (self.static_dir / asset_name).resolve()
        static_root = self.static_dir.resolve()
        try:
            asset_path.relative_to(static_root)
        except ValueError:
            return self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "Not found"})
        if not asset_path.exists() or not asset_path.is_file():
            return self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "Not found"})

        content_type, _ = mimetypes.guess_type(str(asset_path))
        self._send_bytes(
            handler,
            HTTPStatus.OK,
            asset_path.read_bytes(),
            content_type=content_type or "application/octet-stream",
        )

    def _send_json(self, handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self._send_bytes(handler, status, body, content_type="application/json")

    def _send_bytes(
        self,
        handler: BaseHTTPRequestHandler,
        status: HTTPStatus,
        body: bytes,
        *,
        content_type: str,
    ) -> None:
        handler.send_response(status.value)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store, max-age=0")
        handler.send_header("Pragma", "no-cache")
        handler.end_headers()
        handler.wfile.write(body)

    @staticmethod
    def _is_static_asset_path(path: str) -> bool:
        return path in {"/app.js", "/styles.css"}

    @staticmethod
    def _query_int(query: dict[str, list[str]], key: str) -> Optional[int]:
        values = query.get(key)
        if not values:
            return None
        return int(values[0])

    @staticmethod
    def _maybe_int(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        return int(value)

    @staticmethod
    def _maybe_float(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        return float(value)

    @staticmethod
    def _maybe_str(value: Any) -> Optional[str]:
        if value is None or value == "":
            return None
        return str(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Harness product API")
    parser.add_argument("--project-dir", type=Path, default=Path("."), help="Project directory")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind")
    parser.add_argument("--port", type=int, default=7856, help="Port to bind")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    app = ProductApiApplication(args.project_dir, host=args.host, port=args.port)
    app.serve_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI execution
    raise SystemExit(main())
