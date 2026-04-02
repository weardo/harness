"""Tests for discovery.py -- HANDOFF parsing, brief compression, relay context."""

import pytest

from src.core.discovery import (
    parse_handoff,
    extract_decisions,
    extract_files,
    extract_failures,
    compress_discovery,
    write_brief,
    get_relay_context,
    list_briefs,
)


# ---------------------------------------------------------------------------
# Fixtures / sample data
# ---------------------------------------------------------------------------

SAMPLE_OUTPUT_WITH_HANDOFF = """\
I implemented the auth module and added tests.
All 12 tests passing now.

---HANDOFF---
- Implemented JWT auth middleware
- Added rate limiting to /api/login
- TODO: refresh token rotation not started
---

Some trailing text after the block.
"""

SAMPLE_OUTPUT_NO_HANDOFF = """\
I worked on the feature and made good progress.
Decided to use PostgreSQL instead of MySQL.
Created src/api/auth.py and test/test_auth.py.
Error: redis connection timed out during integration test.
"""

SAMPLE_OUTPUT_HANDOFF_EOF = """\
Done with the feature.

--- HANDOFF ---
- Built the dashboard component
- Wired up API routes
"""

SAMPLE_OUTPUT_MULTIPLE_HANDOFFS = """\
First pass done.

---HANDOFF---
- First handoff item A
- First handoff item B
---

More work after that.

---HANDOFF---
- Second handoff item X
---
"""


# ---------------------------------------------------------------------------
# parse_handoff
# ---------------------------------------------------------------------------

class TestParseHandoff:
    def test_well_formed_block(self):
        result = parse_handoff(SAMPLE_OUTPUT_WITH_HANDOFF)
        assert result["found"] is True
        assert len(result["items"]) == 3
        assert "Implemented JWT auth middleware" in result["items"][0]
        assert result["raw"] != ""

    def test_no_block(self):
        result = parse_handoff(SAMPLE_OUTPUT_NO_HANDOFF)
        assert result["found"] is False
        assert result["items"] == []
        assert result["raw"] == ""

    def test_block_at_eof_without_closing(self):
        result = parse_handoff(SAMPLE_OUTPUT_HANDOFF_EOF)
        assert result["found"] is True
        assert len(result["items"]) == 2
        assert "Built the dashboard component" in result["items"][0]

    def test_multiple_blocks_first_wins(self):
        result = parse_handoff(SAMPLE_OUTPUT_MULTIPLE_HANDOFFS)
        assert result["found"] is True
        assert "First handoff item A" in result["items"][0]
        assert all("Second" not in item for item in result["items"])

    def test_extra_whitespace_around_markers(self):
        text = "output\n---  HANDOFF  ---\n- item one\n- item two\n---\n"
        result = parse_handoff(text)
        assert result["found"] is True
        assert len(result["items"]) == 2

    def test_empty_string(self):
        result = parse_handoff("")
        assert result["found"] is False
        assert result["items"] == []


# ---------------------------------------------------------------------------
# extract_decisions
# ---------------------------------------------------------------------------

class TestExtractDecisions:
    def test_finds_decision_keywords(self):
        text = (
            "We decided to use PostgreSQL.\n"
            "The team chose React over Vue.\n"
            "Then picked the fastest option.\n"
            "Unrelated line here.\n"
        )
        decisions = extract_decisions(text)
        assert len(decisions) == 3
        assert "PostgreSQL" in decisions[0]
        assert "React" in decisions[1]

    def test_caps_at_five(self):
        lines = [f"We decided on option {i}" for i in range(10)]
        text = "\n".join(lines)
        decisions = extract_decisions(text)
        assert len(decisions) == 5

    def test_skips_long_lines(self):
        short = "Decided to use short option."
        long_line = "Decided " + "x" * 200
        text = f"{short}\n{long_line}\n"
        decisions = extract_decisions(text)
        assert len(decisions) == 1
        assert "short option" in decisions[0]

    def test_strips_bullet_prefix(self):
        text = "- Chose option A\n* Decided on B\n"
        decisions = extract_decisions(text)
        assert decisions[0] == "Chose option A"
        assert decisions[1] == "Decided on B"


# ---------------------------------------------------------------------------
# extract_files
# ---------------------------------------------------------------------------

class TestExtractFiles:
    def test_finds_src_paths(self):
        text = "Modified src/api/auth.py and lib/utils.ts for the feature."
        files = extract_files(text)
        assert "src/api/auth.py" in files
        assert "lib/utils.ts" in files

    def test_caps_at_ten(self):
        paths = [f"src/file{i}.py" for i in range(20)]
        text = " ".join(paths)
        files = extract_files(text)
        assert len(files) == 10

    def test_deduplicates(self):
        text = "Changed src/app.py and then src/app.py again."
        files = extract_files(text)
        assert files.count("src/app.py") == 1

    def test_various_prefixes(self):
        text = (
            "app/routes.ts components/Button.tsx "
            "pages/index.js test/test_auth.py "
            "spec/helpers.rb api/v2/users.go"
        )
        files = extract_files(text)
        assert len(files) == 6


# ---------------------------------------------------------------------------
# extract_failures
# ---------------------------------------------------------------------------

class TestExtractFailures:
    def test_finds_failure_keywords(self):
        text = (
            "Test failed for auth module.\n"
            "Error: connection refused.\n"
            "The build broke after merge.\n"
            "Everything else is fine.\n"
        )
        failures = extract_failures(text)
        assert len(failures) == 3
        assert "auth module" in failures[0]

    def test_caps_at_three(self):
        lines = [f"Error on step {i}" for i in range(10)]
        text = "\n".join(lines)
        failures = extract_failures(text)
        assert len(failures) == 3

    def test_finds_blocked(self):
        text = "Blocked by missing API key.\n"
        failures = extract_failures(text)
        assert len(failures) == 1
        assert "API key" in failures[0]


# ---------------------------------------------------------------------------
# compress_discovery
# ---------------------------------------------------------------------------

class TestCompressDiscovery:
    def test_with_handoff(self):
        brief = compress_discovery(
            SAMPLE_OUTPUT_WITH_HANDOFF,
            agent_id="agent-1",
            feature_id="feat-001",
            status="complete",
        )
        assert "## Agent: agent-1 | Feature: feat-001" in brief
        assert "**Status:** complete" in brief
        assert "**Built:**" in brief

    def test_without_handoff_falls_back(self):
        brief = compress_discovery(
            SAMPLE_OUTPUT_NO_HANDOFF,
            agent_id="agent-2",
            feature_id="feat-002",
        )
        assert "## Agent: agent-2 | Feature: feat-002" in brief
        assert "**Decisions:**" in brief
        assert "**Files:**" in brief
        assert "**Failures:**" in brief
        # No handoff block so no Built section
        assert "**Built:**" not in brief

    def test_output_under_2000_chars(self):
        long_output = "decided X\n" * 100 + "src/a.py\n" * 100
        brief = compress_discovery(long_output, "a", "f")
        assert len(brief) <= 2003  # 2000 + "..."

    def test_infers_partial_status_from_failures(self):
        text = "Error: something broke.\n"
        brief = compress_discovery(text, "a", "f")
        assert "**Status:** partial" in brief

    def test_infers_complete_status_when_no_failures(self):
        text = "All good, no issues.\n"
        brief = compress_discovery(text, "a", "f")
        assert "**Status:** complete" in brief


# ---------------------------------------------------------------------------
# write_brief
# ---------------------------------------------------------------------------

class TestWriteBrief:
    def test_creates_file_at_correct_path(self, tmp_path):
        state_dir = tmp_path / "h1" / "state"
        state_dir.mkdir(parents=True)
        brief_text = "## Agent: x | Feature: y\n**Status:** complete"
        path = write_brief(brief_text, wave=2, agent_id="agent-x", state_dir=state_dir)
        assert path.exists()
        assert path.name == "w2-agent-x.md"
        assert path.read_text() == brief_text

    def test_creates_directories(self, tmp_path):
        state_dir = tmp_path / "h2" / "deep" / "state"
        path = write_brief("content", wave=1, agent_id="a", state_dir=state_dir)
        assert path.exists()
        assert "fleet/briefs" in str(path)


# ---------------------------------------------------------------------------
# get_relay_context
# ---------------------------------------------------------------------------

class TestGetRelayContext:
    def test_no_briefs_returns_empty(self, tmp_path):
        state_dir = tmp_path / "h3" / "state"
        state_dir.mkdir(parents=True)
        result = get_relay_context(state_dir, current_wave=2)
        assert result == ""

    def test_single_prior_wave(self, tmp_path):
        state_dir = tmp_path / "h4" / "state"
        state_dir.mkdir(parents=True)
        write_brief("Brief from wave 1", wave=1, agent_id="a", state_dir=state_dir)
        result = get_relay_context(state_dir, current_wave=2)
        assert "=== DISCOVERY RELAY ===" in result
        assert "Brief from wave 1" in result
        assert "=== END DISCOVERY RELAY ===" in result

    def test_multiple_waves_sorted(self, tmp_path):
        state_dir = tmp_path / "h5" / "state"
        state_dir.mkdir(parents=True)
        write_brief("Wave 2 brief", wave=2, agent_id="a", state_dir=state_dir)
        write_brief("Wave 1 brief", wave=1, agent_id="b", state_dir=state_dir)
        result = get_relay_context(state_dir, current_wave=3)
        pos_w1 = result.index("Wave 1 brief")
        pos_w2 = result.index("Wave 2 brief")
        assert pos_w1 < pos_w2

    def test_excludes_current_and_future_waves(self, tmp_path):
        state_dir = tmp_path / "h6" / "state"
        state_dir.mkdir(parents=True)
        write_brief("Wave 1", wave=1, agent_id="a", state_dir=state_dir)
        write_brief("Wave 3", wave=3, agent_id="b", state_dir=state_dir)
        result = get_relay_context(state_dir, current_wave=2)
        assert "Wave 1" in result
        assert "Wave 3" not in result


# ---------------------------------------------------------------------------
# list_briefs
# ---------------------------------------------------------------------------

class TestListBriefs:
    def test_empty_dir(self, tmp_path):
        state_dir = tmp_path / "h7" / "state"
        state_dir.mkdir(parents=True)
        assert list_briefs(state_dir) == []

    def test_lists_all(self, tmp_path):
        state_dir = tmp_path / "h8" / "state"
        state_dir.mkdir(parents=True)
        write_brief("a", wave=1, agent_id="x", state_dir=state_dir)
        write_brief("b", wave=2, agent_id="y", state_dir=state_dir)
        briefs = list_briefs(state_dir)
        assert len(briefs) == 2

    def test_filtered_by_wave(self, tmp_path):
        state_dir = tmp_path / "h9" / "state"
        state_dir.mkdir(parents=True)
        write_brief("a", wave=1, agent_id="x", state_dir=state_dir)
        write_brief("b", wave=1, agent_id="y", state_dir=state_dir)
        write_brief("c", wave=2, agent_id="z", state_dir=state_dir)
        wave1 = list_briefs(state_dir, wave=1)
        assert len(wave1) == 2
        wave2 = list_briefs(state_dir, wave=2)
        assert len(wave2) == 1
