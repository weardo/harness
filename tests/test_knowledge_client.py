"""Tests for knowledge query + planner injection."""

import json
from unittest.mock import patch, MagicMock

import pytest

from src.core.control_plane import ControlPlaneClient
from src.core.knowledge_client import format_knowledge_for_planner


class TestQueryKnowledge:
    def test_returns_chunks_when_enabled(self, monkeypatch):
        monkeypatch.setenv("HARNESS_CONTROL_PLANE_URL", "http://localhost:7842")
        cp = ControlPlaneClient()

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps([
            {"chunk_type": "failure_mode", "content": "FTS triggers omitted"},
            {"chunk_type": "pattern", "content": "Explicit SQL DDL in spec"},
        ]).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            results = cp.query_knowledge("TypeScript backend", limit=5)
            assert len(results) == 2
            assert results[0]["chunk_type"] == "failure_mode"

    def test_returns_empty_when_disabled(self, monkeypatch):
        monkeypatch.delenv("HARNESS_CONTROL_PLANE_URL", raising=False)
        cp = ControlPlaneClient()
        results = cp.query_knowledge("anything")
        assert results == []

    def test_returns_empty_on_network_error(self, monkeypatch):
        monkeypatch.setenv("HARNESS_CONTROL_PLANE_URL", "http://localhost:7842")
        cp = ControlPlaneClient()

        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            results = cp.query_knowledge("anything")
            assert results == []


class TestGetAllChunks:
    def test_returns_chunks_when_enabled(self, monkeypatch):
        monkeypatch.setenv("HARNESS_CONTROL_PLANE_URL", "http://localhost:7842")
        cp = ControlPlaneClient()

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps([
            {"chunk_type": "failure_mode", "content": "Missing DDL"},
        ]).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            results = cp.get_all_chunks()
            assert len(results) == 1

    def test_returns_empty_when_disabled(self, monkeypatch):
        monkeypatch.delenv("HARNESS_CONTROL_PLANE_URL", raising=False)
        cp = ControlPlaneClient()
        results = cp.get_all_chunks()
        assert results == []

    def test_returns_empty_on_network_error(self, monkeypatch):
        monkeypatch.setenv("HARNESS_CONTROL_PLANE_URL", "http://localhost:7842")
        cp = ControlPlaneClient()

        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            results = cp.get_all_chunks()
            assert results == []


class TestFormatKnowledgeForPlanner:
    def test_formats_failure_modes_and_patterns(self):
        chunks = [
            {"chunk_type": "failure_mode", "content": "FTS triggers omitted", "project_id": "llm-obs"},
            {"chunk_type": "pattern", "content": "Use explicit SQL DDL", "project_id": "llm-obs"},
            {"chunk_type": "convention", "content": "node:20-slim over alpine", "project_id": None},
        ]
        result = format_knowledge_for_planner(chunks)
        assert "LEARNINGS FROM PREVIOUS RUNS" in result
        assert "FAILURE MODES" in result
        assert "FTS triggers omitted" in result
        assert "PATTERNS" in result
        assert "CONVENTIONS" in result

    def test_empty_chunks_returns_empty_string(self):
        result = format_knowledge_for_planner([])
        assert result == ""

    def test_failure_modes_appear_before_patterns(self):
        chunks = [
            {"chunk_type": "pattern", "content": "Pattern first"},
            {"chunk_type": "failure_mode", "content": "Failure second"},
        ]
        result = format_knowledge_for_planner(chunks)
        fm_pos = result.index("FAILURE MODES")
        pat_pos = result.index("PATTERNS")
        assert fm_pos < pat_pos, "Failure modes should appear before patterns"

    def test_chunks_with_empty_content_skipped(self):
        chunks = [
            {"chunk_type": "failure_mode", "content": ""},
            {"chunk_type": "pattern", "content": "Good pattern"},
        ]
        result = format_knowledge_for_planner(chunks)
        assert "FAILURE MODES" not in result
        assert "PATTERNS" in result
        assert "Good pattern" in result
