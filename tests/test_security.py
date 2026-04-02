"""Tests for security.py — bash command allowlist and validation."""

import pytest

from src.core.security import (
    validate_command,
    check_dangerous_patterns,
    extract_commands,
    validate_sensitive_command,
)


class TestExtractCommands:
    def test_simple_command(self):
        assert extract_commands("ls -la") == ["ls"]

    def test_piped_commands(self):
        assert extract_commands("cat file.txt | grep pattern | wc -l") == ["cat", "grep", "wc"]

    def test_chained_commands(self):
        assert extract_commands("npm install && npm test") == ["npm", "npm"]

    def test_semicolon_separated(self):
        assert extract_commands("ls; pwd; echo hello") == ["ls", "pwd", "echo"]

    def test_or_chain(self):
        assert extract_commands("test -f file || touch file") == ["test", "touch"]

    def test_path_prefix_stripped(self):
        assert extract_commands("/usr/bin/git status") == ["git"]

    def test_env_var_skipped(self):
        assert extract_commands("NODE_ENV=production npm start") == ["npm"]

    def test_empty_command(self):
        assert extract_commands("") == []

    def test_malformed_quotes(self):
        # Should return empty (fail-safe to block)
        assert extract_commands("echo 'unterminated") == []


class TestCheckDangerousPatterns:
    def test_command_substitution_dollar(self):
        blocked, _ = check_dangerous_patterns("echo $(whoami)")
        assert blocked

    def test_command_substitution_backtick(self):
        blocked, _ = check_dangerous_patterns("echo `whoami`")
        assert blocked

    def test_null_bytes(self):
        blocked, _ = check_dangerous_patterns("echo \x00hello")
        assert blocked

    def test_oversized_command(self):
        blocked, _ = check_dangerous_patterns("echo " + "x" * 11_000)
        assert blocked

    def test_sudo(self):
        blocked, _ = check_dangerous_patterns("sudo rm -rf /")
        assert blocked

    def test_rm_rf_root(self):
        blocked, _ = check_dangerous_patterns("rm -rf /")
        assert blocked

    def test_force_push(self):
        blocked, _ = check_dangerous_patterns("git push --force origin main")
        assert blocked

    def test_no_verify(self):
        blocked, _ = check_dangerous_patterns("git commit --no-verify -m 'skip hooks'")
        assert blocked

    def test_safe_command(self):
        blocked, _ = check_dangerous_patterns("npm test")
        assert not blocked


class TestValidateCommand:
    def test_allowed_command(self):
        ok, _ = validate_command("ls -la")
        assert ok

    def test_allowed_pipeline(self):
        ok, _ = validate_command("cat file.txt | grep test | wc -l")
        assert ok

    def test_allowed_chain(self):
        ok, _ = validate_command("npm install && npm test")
        assert ok

    def test_blocked_command(self):
        ok, reason = validate_command("ruby script.rb")
        assert not ok
        assert "not in the allowlist" in reason

    def test_blocked_dangerous(self):
        ok, reason = validate_command("echo $(cat /etc/passwd)")
        assert not ok
        assert "substitution" in reason

    def test_git_operations(self):
        ok, _ = validate_command("git add . && git commit -m 'test'")
        assert ok

    def test_npm_operations(self):
        ok, _ = validate_command("npm run dev")
        assert ok

    def test_python_operations(self):
        ok, _ = validate_command("python3 -m pytest tests/")
        assert ok

    def test_empty_command_allowed(self):
        ok, _ = validate_command("")
        assert ok

    def test_custom_allowlist(self):
        ok, _ = validate_command("ruby script.rb", allowed_commands={"ruby"})
        assert ok


class TestSensitiveCommands:
    def test_pkill_node_allowed(self):
        ok, _ = validate_sensitive_command("pkill", "pkill node")
        assert ok

    def test_pkill_vite_allowed(self):
        ok, _ = validate_sensitive_command("pkill", "pkill vite")
        assert ok

    def test_pkill_random_blocked(self):
        ok, _ = validate_sensitive_command("pkill", "pkill sshd")
        assert not ok

    def test_chmod_plus_x_allowed(self):
        ok, _ = validate_sensitive_command("chmod", "chmod +x init.sh")
        assert ok

    def test_chmod_777_blocked(self):
        ok, _ = validate_sensitive_command("chmod", "chmod 777 /etc/passwd")
        assert not ok

    def test_rm_safe_path(self):
        ok, _ = validate_sensitive_command("rm", "rm -rf node_modules/")
        assert ok

    def test_rm_system_path_blocked(self):
        ok, _ = validate_sensitive_command("rm", "rm -rf /etc")
        assert not ok

    def test_rm_home_blocked(self):
        ok, _ = validate_sensitive_command("rm", "rm -rf ~")
        assert not ok
