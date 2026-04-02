"""Tests for ControlPlaneClient."""
import os
import threading
import time
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from src.core.control_plane import ControlPlaneClient


class TestControlPlaneClientEnabled:
    def test_disabled_when_no_env_var(self, monkeypatch):
        monkeypatch.delenv("HARNESS_CONTROL_PLANE_URL", raising=False)
        cp = ControlPlaneClient()
        assert cp.enabled is False

    def test_enabled_when_env_var_set(self, monkeypatch):
        monkeypatch.setenv("HARNESS_CONTROL_PLANE_URL", "http://localhost:7842")
        cp = ControlPlaneClient()
        assert cp.enabled is True

    def test_noop_when_disabled(self, monkeypatch):
        monkeypatch.delenv("HARNESS_CONTROL_PLANE_URL", raising=False)
        cp = ControlPlaneClient()
        # Should return immediately without error
        cp.create_run("proj-1")
        cp.post_event("feature_pass", feature_id="f1")
        cp.ingest_retro("/nonexistent/path.md")


class TestCreateRun:
    def test_create_run_posts_to_api(self, monkeypatch):
        monkeypatch.setenv("HARNESS_CONTROL_PLANE_URL", "http://localhost:7842")
        cp = ControlPlaneClient()

        mock_response = MagicMock()
        mock_response.read.return_value = b'{"id": "run-abc"}'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response) as mock_open:
            cp.create_run("test-project")
            # Give any thread time to run
            time.sleep(0.1)
            # create_run makes 2 HTTP calls: GET /projects/{id} (ensure_project) + POST /runs
            assert mock_open.call_count == 2
            # The last call must be the POST to /api/v1/runs
            call_args = mock_open.call_args
            req = call_args[0][0]
            assert "/api/v1/runs" in req.full_url

    def test_create_run_stores_run_id(self, monkeypatch):
        monkeypatch.setenv("HARNESS_CONTROL_PLANE_URL", "http://localhost:7842")
        cp = ControlPlaneClient()

        mock_response = MagicMock()
        mock_response.read.return_value = b'{"id": "run-xyz"}'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            cp.create_run("test-project")
            time.sleep(0.1)
            assert cp.run_id == "run-xyz"


class TestPostEvent:
    def test_post_event_fires_daemon_thread(self, monkeypatch):
        monkeypatch.setenv("HARNESS_CONTROL_PLANE_URL", "http://localhost:7842")
        cp = ControlPlaneClient()
        cp.run_id = "run-abc"

        fired = threading.Event()

        def fake_urlopen(req, timeout=None):
            fired.set()
            raise ConnectionError("mock error")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            cp.post_event("feature_pass", feature_id="f1")
            fired.wait(timeout=1.0)
            assert fired.is_set()

    def test_post_event_does_not_raise_on_connection_error(self, monkeypatch):
        monkeypatch.setenv("HARNESS_CONTROL_PLANE_URL", "http://localhost:7842")
        cp = ControlPlaneClient()
        cp.run_id = "run-abc"

        with patch("urllib.request.urlopen", side_effect=ConnectionError("refused")):
            # Should not raise
            cp.post_event("feature_fail", error="something broke")
            time.sleep(0.1)


class TestIngestRetro:
    def test_ingest_retro_reads_file_and_posts(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HARNESS_CONTROL_PLANE_URL", "http://localhost:7842")
        cp = ControlPlaneClient()
        cp.run_id = "run-abc"

        retro_file = tmp_path / "retro.md"
        retro_file.write_text("## Patterns\nFound a great pattern.")

        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true}'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response) as mock_open:
            cp.ingest_retro(str(retro_file))
            time.sleep(0.1)
            mock_open.assert_called_once()
            req = mock_open.call_args[0][0]
            assert "/api/v1/knowledge/ingest" in req.full_url

    def test_ingest_retro_does_not_raise_on_connection_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HARNESS_CONTROL_PLANE_URL", "http://localhost:7842")
        cp = ControlPlaneClient()

        retro_file = tmp_path / "retro.md"
        retro_file.write_text("content")

        with patch("urllib.request.urlopen", side_effect=ConnectionError("refused")):
            cp.ingest_retro(str(retro_file))
            time.sleep(0.1)
