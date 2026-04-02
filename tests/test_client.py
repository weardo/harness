"""Tests for client.py — SDK client factory."""

import json
import tempfile
from pathlib import Path

import pytest

from src.core.client import (
    load_config,
    get_browser_tools,
    get_mcp_servers,
    write_security_settings,
    create_client_options,
    PLAYWRIGHT_TOOLS,
    PUPPETEER_TOOLS,
)


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def config():
    return {
        "model": "claude-sonnet-4-6",
        "effort": "high",
        "evaluator": {
            "browser_tool": "playwright",
            "browser_verification": "auto",
        },
        "generator": {
            "max_turns_per_session": 1000,
        },
        "security": {
            "sandbox": True,
            "permission_mode": "acceptEdits",
            "deny_reads": [".env", ".env.*"],
        },
    }


class TestLoadConfig:
    def test_loads_yaml(self, tmp_dir):
        path = tmp_dir / "config.yaml"
        path.write_text("model: test-model\neffort: low\n")
        config = load_config(path)
        assert config["model"] == "test-model"
        assert config["effort"] == "low"

    def test_missing_file(self, tmp_dir):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_dir / "nope.yaml")


class TestGetBrowserTools:
    def test_playwright(self):
        tools = get_browser_tools({"evaluator": {"browser_tool": "playwright"}})
        assert tools == PLAYWRIGHT_TOOLS

    def test_puppeteer(self):
        tools = get_browser_tools({"evaluator": {"browser_tool": "puppeteer"}})
        assert tools == PUPPETEER_TOOLS

    def test_default_playwright(self):
        tools = get_browser_tools({})
        assert tools == PLAYWRIGHT_TOOLS


class TestGetMcpServers:
    def test_playwright_server(self):
        servers = get_mcp_servers({"evaluator": {"browser_tool": "playwright"}})
        assert "playwright" in servers

    def test_never_skips_browser(self):
        servers = get_mcp_servers({"evaluator": {"browser_verification": "never"}})
        assert servers == {}


class TestWriteSecuritySettings:
    def test_creates_settings_file(self, tmp_dir, config):
        path = write_security_settings(tmp_dir, config)
        assert path.exists()
        with open(path) as f:
            settings = json.load(f)
        assert settings["sandbox"]["enabled"] is True
        assert "Read(./**)" in settings["permissions"]["allow"]

    def test_deny_reads(self, tmp_dir, config):
        path = write_security_settings(tmp_dir, config)
        with open(path) as f:
            settings = json.load(f)
        deny = settings["permissions"]["deny"]
        assert "Read(.env)" in deny


class TestCreateClientOptions:
    def test_creates_options(self, tmp_dir, config):
        opts = create_client_options(tmp_dir, config)
        assert opts["model"] == "claude-sonnet-4-6"
        assert opts["max_turns"] == 1000
        assert "Read" in opts["allowed_tools"]
        assert "Bash" in opts["allowed_tools"]

    def test_model_override(self, tmp_dir, config):
        opts = create_client_options(tmp_dir, config, model_override="claude-opus-4-6")
        assert opts["model"] == "claude-opus-4-6"

    def test_settings_file_created(self, tmp_dir, config):
        opts = create_client_options(tmp_dir, config)
        assert Path(opts["settings"]).exists()

    def test_mcp_servers_included(self, tmp_dir, config):
        opts = create_client_options(tmp_dir, config)
        assert "playwright" in opts["mcp_servers"]

    def test_hooks_included(self, tmp_dir, config):
        opts = create_client_options(tmp_dir, config)
        assert "PreToolUse" in opts["hooks"]
