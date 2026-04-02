"""
Security Hooks — Bash Command Allowlist
========================================

PreToolUse hook that validates bash commands against an allowlist.
Blocks command substitution, subshells, and dangerous patterns.

Based on Anthropic quickstart security.py + autonomous-coding-harness enhancements.
"""

import os
import re
import shlex
from typing import Optional


# Default allowed commands for development tasks
DEFAULT_ALLOWED_COMMANDS = {
    # File inspection
    "ls", "cat", "head", "tail", "wc", "grep", "find", "sort", "uniq",
    # File operations
    "cp", "mv", "mkdir", "chmod", "touch", "rm",
    # Directory
    "pwd", "cd", "echo", "printf",
    # Node.js
    "npm", "node", "npx",
    # Python
    "python", "python3", "pip", "pip3", "pytest",
    # Other stacks
    "go", "cargo", "make",
    # Version control
    "git",
    # Process management
    "ps", "lsof", "sleep", "pkill", "kill",
    # Scripts
    "init.sh", "bash", "sh",
    # Testing
    "curl", "wget",
}

# Commands needing extra validation
SENSITIVE_COMMANDS = {"pkill", "kill", "chmod", "rm"}

# Allowed pkill targets (dev processes only)
ALLOWED_PKILL_TARGETS = {"node", "npm", "npx", "vite", "next", "python", "uvicorn", "gunicorn"}


def validate_command(command_string: str, allowed_commands: Optional[set] = None) -> tuple[bool, str]:
    """Validate a bash command against the allowlist.

    Returns (is_allowed, reason).
    """
    allowed = allowed_commands or DEFAULT_ALLOWED_COMMANDS

    if not command_string or not command_string.strip():
        return True, ""

    # Block command substitution patterns
    blocked, reason = check_dangerous_patterns(command_string)
    if blocked:
        return False, reason

    # Extract and validate each command in the pipeline/chain
    commands = extract_commands(command_string)
    if not commands:
        return False, "Could not parse command (malformed shell syntax)"

    for cmd in commands:
        if cmd not in allowed:
            return False, f"Command '{cmd}' is not in the allowlist"

        # Extra validation for sensitive commands
        if cmd in SENSITIVE_COMMANDS:
            ok, reason = validate_sensitive_command(cmd, command_string)
            if not ok:
                return False, reason

    return True, ""


def check_dangerous_patterns(command_string: str) -> tuple[bool, str]:
    """Check for dangerous shell patterns. Returns (is_blocked, reason)."""
    # Command substitution: $(...) or backticks
    if re.search(r'\$\(', command_string):
        return True, "Command substitution $(...) is blocked"
    if '`' in command_string:
        return True, "Backtick command substitution is blocked"

    # Null bytes
    if '\x00' in command_string:
        return True, "Null bytes in command are blocked"

    # Oversized commands (potential injection payload)
    if len(command_string) > 10_000:
        return True, "Command exceeds 10KB size limit"

    # Specific dangerous patterns
    dangerous = [
        (r'\bsudo\b', "sudo is blocked"),
        (r'rm\s+-rf\s+/', "rm -rf / is blocked"),
        (r'git\s+push\s+.*--force', "git push --force is blocked"),
        (r'git\s+commit\s+.*--no-verify', "git commit --no-verify is blocked"),
        (r'>\s*/dev/sd', "Writing to block devices is blocked"),
        (r'mkfs\.', "Filesystem formatting is blocked"),
    ]
    for pattern, reason in dangerous:
        if re.search(pattern, command_string):
            return True, reason

    return False, ""


def extract_commands(command_string: str) -> list[str]:
    """Extract base command names from a shell command string.

    Handles pipes, chaining (&&, ||, ;), and path prefixes.
    """
    commands = []

    # Split on command separators
    segments = re.split(r'\s*(?:&&|\|\||;)\s*', command_string)

    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue

        # Handle pipes: each pipe segment has its own command
        pipe_parts = segment.split("|")
        for part in pipe_parts:
            part = part.strip()
            if not part:
                continue

            try:
                tokens = shlex.split(part)
            except ValueError:
                return []  # Malformed — fail-safe to block

            if not tokens:
                continue

            # Find the command token (skip env vars, redirections)
            for token in tokens:
                # Skip variable assignments (VAR=value)
                if "=" in token and not token.startswith("=") and not token.startswith("-"):
                    continue
                # Skip redirections
                if token in (">", ">>", "<", "2>", "2>>", "&>"):
                    continue
                # Skip flags
                if token.startswith("-"):
                    continue
                # Skip shell keywords
                if token in ("if", "then", "else", "fi", "for", "while", "do", "done", "case", "esac", "in", "{", "}", "!"):
                    continue

                # Extract base command name (strip path)
                cmd = os.path.basename(token)
                if cmd:
                    commands.append(cmd)
                break

    return commands


def validate_sensitive_command(cmd: str, full_command: str) -> tuple[bool, str]:
    """Extra validation for sensitive commands."""
    if cmd in ("pkill", "kill"):
        # Only allow killing dev processes
        try:
            tokens = shlex.split(full_command)
        except ValueError:
            return False, f"{cmd}: could not parse arguments"

        for i, t in enumerate(tokens):
            if t in ("pkill", "kill") and i + 1 < len(tokens):
                target = tokens[i + 1]
                # Skip flags
                while target.startswith("-") and i + 2 < len(tokens):
                    i += 1
                    target = tokens[i + 1]
                if target not in ALLOWED_PKILL_TARGETS and not target.isdigit():
                    return False, f"{cmd}: target '{target}' is not an allowed process"
        return True, ""

    if cmd == "chmod":
        # Only allow +x variants
        if "+x" not in full_command and "755" not in full_command and "700" not in full_command:
            return False, "chmod: only +x/755/700 variants are allowed"
        return True, ""

    if cmd == "rm":
        # Block rm -rf with dangerous paths
        if re.search(r'rm\s+.*-.*r.*\s+(/|~|\$HOME|\.\.|/etc|/usr|/var|/sys)', full_command):
            return False, "rm: refusing to remove system directories"
        return True, ""

    return True, ""


async def bash_security_hook(input_data: dict, tool_use_id: str = None, context=None) -> dict:
    """PreToolUse hook for the Claude Agent SDK.

    Validates bash commands against the allowlist.
    Returns SDK hook format for allow/deny decisions.
    """
    tool_input = input_data.get("tool_input", {})
    command = tool_input.get("command", "")

    if not command:
        return {}  # Allow (no command to validate)

    is_allowed, reason = validate_command(command)

    if is_allowed:
        return {}  # Allow

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"Security: {reason}",
        }
    }
