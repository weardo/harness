"""
Claude SDK Client Factory
==========================

Creates configured ClaudeSDKClient instances with security, MCP, and sandbox.
Based on Anthropic quickstart client.py.
"""

import json
import os
from pathlib import Path
from typing import Optional

import yaml

from .security import bash_security_hook


# Playwright MCP tools
PLAYWRIGHT_TOOLS = [
    "mcp__playwright__browser_navigate",
    "mcp__playwright__browser_snapshot",
    "mcp__playwright__browser_click",
    "mcp__playwright__browser_fill_form",
    "mcp__playwright__browser_take_screenshot",
    "mcp__playwright__browser_evaluate",
    "mcp__playwright__browser_press_key",
]

# Puppeteer MCP tools
PUPPETEER_TOOLS = [
    "mcp__puppeteer__puppeteer_navigate",
    "mcp__puppeteer__puppeteer_screenshot",
    "mcp__puppeteer__puppeteer_click",
    "mcp__puppeteer__puppeteer_fill",
    "mcp__puppeteer__puppeteer_select",
    "mcp__puppeteer__puppeteer_hover",
    "mcp__puppeteer__puppeteer_evaluate",
]

BUILTIN_TOOLS = [
    "Read", "Write", "Edit", "Glob", "Grep", "Bash",
]


def load_config(config_path: Path) -> dict:
    """Load harness config from YAML file."""
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")
    with open(config_path) as f:
        return yaml.safe_load(f)


def get_browser_tools(config: dict) -> list[str]:
    """Get browser automation tool names based on config."""
    evaluator_config = config.get("evaluator", {})
    browser_tool = evaluator_config.get("browser_tool", "playwright")
    if browser_tool == "playwright":
        return PLAYWRIGHT_TOOLS
    elif browser_tool == "puppeteer":
        return PUPPETEER_TOOLS
    return []


def get_mcp_servers(config: dict) -> dict:
    """Get MCP server configuration based on config."""
    evaluator_config = config.get("evaluator", {})
    browser_tool = evaluator_config.get("browser_tool", "playwright")
    browser_verification = evaluator_config.get("browser_verification", "auto")

    servers = {}
    if browser_verification != "never":
        if browser_tool == "playwright":
            servers["playwright"] = {
                "command": "npx",
                "args": ["@playwright/mcp"],
            }
        elif browser_tool == "puppeteer":
            servers["puppeteer"] = {
                "command": "npx",
                "args": ["puppeteer-mcp-server"],
            }
    return servers


def write_security_settings(project_dir: Path, config: dict) -> Path:
    """Write .claude_settings.json with security configuration.

    Returns path to the settings file.
    """
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)

    security_config = config.get("security", {})
    browser_tools = get_browser_tools(config)

    settings = {
        "sandbox": {
            "enabled": security_config.get("sandbox", True),
            "autoAllowBashIfSandboxed": True,
        },
        "permissions": {
            "defaultMode": security_config.get("permission_mode", "acceptEdits"),
            "allow": [
                "Read(./**)",
                "Write(./**)",
                "Edit(./**)",
                "Glob(./**)",
                "Grep(./**)",
                "Bash(*)",
                *browser_tools,
            ],
            "deny": [
                *[f"Read({p})" for p in security_config.get("deny_reads", [])],
            ],
        },
    }

    settings_path = project_dir / ".claude_settings.json"
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)

    return settings_path


def create_client_options(
    project_dir: Path,
    config: dict,
    model_override: Optional[str] = None,
    system_prompt: str = "You are an expert full-stack developer building a production-quality application.",
) -> dict:
    """Create options dict for ClaudeSDKClient.

    Returns a dict that can be passed to ClaudeCodeOptions(**options).
    This avoids importing the SDK at module level (so tests work without it).
    """
    project_dir = Path(project_dir)
    model = model_override or config.get("model", "claude-sonnet-4-6")
    browser_tools = get_browser_tools(config)
    mcp_servers = get_mcp_servers(config)
    settings_path = write_security_settings(project_dir, config)

    generator_config = config.get("generator", {})

    return {
        "model": model,
        "system_prompt": system_prompt,
        "allowed_tools": [*BUILTIN_TOOLS, *browser_tools],
        "mcp_servers": mcp_servers,
        "max_turns": generator_config.get("max_turns_per_session", 1000),
        "cwd": str(project_dir.resolve()),
        "settings": str(settings_path.resolve()),
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [bash_security_hook]},
            ],
        },
    }
