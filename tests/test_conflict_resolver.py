"""Tests for the LLM-based conflict resolver (Tier 2 fallback).

Covers pure logic (marker detection, validation, fence stripping, prompt building)
without invoking the LLM. LLM-invoking paths are tested with a stubbed
`_invoke_haiku` via `unittest.mock`.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest import mock

from src.core import conflict_resolver as cr


def _run(coro):
    """Helper — run an async coroutine in a fresh loop (avoids pytest-asyncio)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Marker detection + fence stripping
# ---------------------------------------------------------------------------

def test_has_markers_positive():
    text = "a\n<<<<<<< HEAD\nb\n=======\nc\n>>>>>>> branch\n"
    assert cr._has_markers(text) is True


def test_has_markers_negative():
    text = 'import { A, B } from "x";\n'
    assert cr._has_markers(text) is False


def test_strip_code_fence_triple_backtick():
    text = "```typescript\nconst x = 1;\n```"
    assert cr._strip_code_fence(text) == "const x = 1;"


def test_strip_code_fence_no_fence():
    text = "const x = 1;\n"
    assert cr._strip_code_fence(text) == "const x = 1;"


# ---------------------------------------------------------------------------
# JSON validation
# ---------------------------------------------------------------------------

def test_validate_json_valid():
    assert cr._validate_json('{"a": 1, "b": "two"}') is True


def test_validate_json_invalid():
    assert cr._validate_json('{"a": 1, "b":}') is False


# ---------------------------------------------------------------------------
# Light TS structural validator
# ---------------------------------------------------------------------------

def test_ts_validator_balanced():
    text = 'import { A } from "x";\nfunction f() { return { a: [1, 2] }; }\n'
    assert cr._validate_ts_basic(text) is True


def test_ts_validator_unbalanced_brace():
    assert cr._validate_ts_basic("function f() { return 1;") is False


def test_ts_validator_ignores_braces_in_strings():
    text = 'const s = "}";\nconst t = `hello`;\n'
    assert cr._validate_ts_basic(text) is True


def test_ts_validator_ignores_line_comments():
    text = "const x = 1; // trailing } brace\n"
    assert cr._validate_ts_basic(text) is True


def test_ts_validator_ignores_block_comments():
    text = "/* { unbalanced */ const x = 1;\n"
    assert cr._validate_ts_basic(text) is True


# ---------------------------------------------------------------------------
# Validate dispatch by extension
# ---------------------------------------------------------------------------

def test_validate_rejects_markers(tmp_path: Path):
    f = tmp_path / "messages.json"
    text = '{\n  "a": 1,\n<<<<<<< HEAD\n  "b": 2\n=======\n  "c": 3\n>>>>>>> x\n}\n'
    assert cr._validate(f, text) is False


def test_validate_empty_string(tmp_path: Path):
    f = tmp_path / "x.ts"
    assert cr._validate(f, "") is False


def test_validate_json_file_dispatch(tmp_path: Path):
    f = tmp_path / "messages.json"
    assert cr._validate(f, '{"a": 1}') is True
    assert cr._validate(f, '{"a":}') is False


def test_validate_unknown_extension(tmp_path: Path):
    f = tmp_path / "README.md"
    # No syntax check — accept if no markers
    assert cr._validate(f, "# Heading\nParagraph.\n") is True


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def test_build_prompt_includes_file_tags(tmp_path: Path):
    f = tmp_path / "api.ts"
    body = 'export const x = "hello";\n'
    prompt = cr._build_prompt(f, body)
    assert "<file>" in prompt
    assert "</file>" in prompt
    assert body in prompt
    assert "api.ts" in prompt


# ---------------------------------------------------------------------------
# resolve_file_with_llm — with stubbed LLM invocation
# ---------------------------------------------------------------------------

def test_resolve_file_no_markers_returns_true(tmp_path: Path):
    f = tmp_path / "clean.json"
    f.write_text('{"a": 1}\n')
    assert _run(cr.resolve_file_with_llm(f)) is True
    assert f.read_text() == '{"a": 1}\n'


def test_resolve_file_too_large_returns_false(tmp_path: Path):
    f = tmp_path / "huge.ts"
    f.write_text("<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> b\n" + "a" * 250_000)
    assert _run(cr.resolve_file_with_llm(f, max_bytes=1000)) is False


def test_resolve_file_llm_success(tmp_path: Path):
    f = tmp_path / "messages.json"
    conflicted = (
        '{\n'
        '  "a": 1,\n'
        '<<<<<<< HEAD\n'
        '  "b": 2\n'
        '=======\n'
        '  "c": 3\n'
        '>>>>>>> branch\n'
        '}\n'
    )
    resolved = '{\n  "a": 1,\n  "b": 2,\n  "c": 3\n}\n'
    f.write_text(conflicted)

    async def fake_invoke(prompt, model):
        return resolved

    with mock.patch.object(cr, "_invoke_haiku", side_effect=fake_invoke):
        assert _run(cr.resolve_file_with_llm(f)) is True

    assert json.loads(f.read_text()) == {"a": 1, "b": 2, "c": 3}


def test_resolve_file_llm_returns_cannot_resolve(tmp_path: Path):
    f = tmp_path / "api.ts"
    conflicted = '<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> b\n'
    f.write_text(conflicted)

    async def fake_invoke(prompt, model):
        return cr.CANNOT_RESOLVE

    with mock.patch.object(cr, "_invoke_haiku", side_effect=fake_invoke):
        assert _run(cr.resolve_file_with_llm(f)) is False

    assert f.read_text() == conflicted


def test_resolve_file_llm_invalid_output_rejected(tmp_path: Path):
    f = tmp_path / "messages.json"
    conflicted = '{\n<<<<<<< HEAD\n"a": 1\n=======\n"b": 2\n>>>>>>> b\n}\n'
    f.write_text(conflicted)

    async def fake_invoke(prompt, model):
        return '{"a": 1, "b":}'   # invalid JSON

    with mock.patch.object(cr, "_invoke_haiku", side_effect=fake_invoke):
        assert _run(cr.resolve_file_with_llm(f)) is False

    assert f.read_text() == conflicted


def test_resolve_file_llm_returns_none_on_error(tmp_path: Path):
    f = tmp_path / "api.ts"
    f.write_text('<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> b\n')

    async def fake_invoke(prompt, model):
        return None

    with mock.patch.object(cr, "_invoke_haiku", side_effect=fake_invoke):
        assert _run(cr.resolve_file_with_llm(f)) is False


def test_resolve_file_llm_strips_code_fence(tmp_path: Path):
    f = tmp_path / "messages.json"
    f.write_text('{\n<<<<<<< HEAD\n"a": 1\n=======\n"b": 2\n>>>>>>> b\n}\n')

    async def fake_invoke(prompt, model):
        # LLM wrapped response in fence despite instructions
        return '```json\n{"a": 1, "b": 2}\n```'

    with mock.patch.object(cr, "_invoke_haiku", side_effect=fake_invoke):
        assert _run(cr.resolve_file_with_llm(f)) is True

    assert json.loads(f.read_text()) == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# try_llm_resolve_conflicts — batch wrapper
# ---------------------------------------------------------------------------

def test_try_llm_resolve_conflicts_partial_success(tmp_path: Path):
    f1 = tmp_path / "a.json"
    f2 = tmp_path / "b.json"
    f1.write_text('{\n<<<<<<< HEAD\n"a": 1\n=======\n"b": 2\n>>>>>>> x\n}\n')
    f2.write_text('{\n<<<<<<< HEAD\n"c": 3\n=======\n"c": 4\n>>>>>>> x\n}\n')

    async def fake_invoke(prompt, model):
        if "a.json" in prompt:
            return '{"a": 1, "b": 2}'
        return cr.CANNOT_RESOLVE

    with mock.patch.object(cr, "_invoke_haiku", side_effect=fake_invoke):
        resolved, unresolved = cr.try_llm_resolve_conflicts(
            tmp_path, ["a.json", "b.json"]
        )

    assert resolved == ["a.json"]
    assert unresolved == ["b.json"]


# ---------------------------------------------------------------------------
# get_resolver_config (from parallel module — defaults and config reading)
# ---------------------------------------------------------------------------

def test_get_resolver_config_defaults():
    from src.core.parallel import get_resolver_config
    enabled, model = get_resolver_config(None)
    assert enabled is True
    assert model == "claude-haiku-4-5"


def test_get_resolver_config_explicit_disable():
    from src.core.parallel import get_resolver_config
    cfg = {"parallel": {"auto_resolver": {"llm_agent": False}}}
    enabled, _model = get_resolver_config(cfg)
    assert enabled is False


def test_get_resolver_config_custom_model():
    from src.core.parallel import get_resolver_config
    cfg = {"parallel": {"auto_resolver": {"llm_agent_model": "claude-sonnet-4-6"}}}
    _enabled, model = get_resolver_config(cfg)
    assert model == "claude-sonnet-4-6"
