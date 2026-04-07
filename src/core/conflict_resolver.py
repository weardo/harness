"""LLM-based merge conflict resolver (Tier 2 fallback).

Used when mechanical rule-based resolvers in `parallel.py` cannot resolve a
file-level conflict inside a submodule. Spawns a short, bounded Haiku session
per conflicted file, validates the output, and writes it back.

Design doc: docs/plans/2026-04-07-submodule-merge-conflict-resolution.md
Tier position: after mechanical rules (Tier 1), before abort (Tier 4).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

CONFLICT_MARKERS = ("<<<<<<< ", "=======", ">>>>>>> ")

# Sentinel the LLM is instructed to emit when a conflict is genuinely
# unresolvable (semantic conflict, not additive). We treat this as failure
# and fall through to the next tier.
CANNOT_RESOLVE = "<<<CANNOT_RESOLVE>>>"

SYSTEM_PROMPT = """You are a git merge conflict resolver. You receive a single
file that contains one or more conflict markers produced by `git merge`. Your
job is to output the FULL resolved file content — nothing else.

Rules:
1. If both sides ADDED different content (imports, keys, exports, interfaces,
   hooks, function definitions) that can coexist, take the UNION — keep both.
2. If both sides MODIFIED the same line / expression / body differently, output
   the literal string <<<CANNOT_RESOLVE>>> and nothing else.
3. NEVER invent code beyond what appears in the conflict blocks. Only choose
   which side to keep, or merge both sides verbatim.
4. Preserve the file's original formatting, indentation, and trailing newline.
5. Do not add commentary, explanations, markdown fences, or diff markers.
6. Output must be the full file body, resolved — no conflict markers remaining.

The file will be provided between <file> tags. Respond with only the resolved
file contents (no tags, no prose)."""


def _has_markers(text: str) -> bool:
    return any(m in text for m in CONFLICT_MARKERS)


def _strip_code_fence(text: str) -> str:
    """LLMs sometimes wrap output in ``` fences despite instructions. Strip them."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n", 1)
        text = lines[1] if len(lines) > 1 else ""
        if text.endswith("```"):
            text = text[: -3].rstrip("\n")
    return text


def _validate_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except Exception:
        return False


def _validate_ts_basic(text: str) -> bool:
    """Cheap structural check for TS/JS files — balanced braces/parens/brackets.
    Avoids a full tsc call; trades precision for speed/cost."""
    pairs = {"{": "}", "(": ")", "[": "]"}
    stack: list[str] = []
    in_str: Optional[str] = None
    escape = False
    in_line_cmt = False
    in_block_cmt = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_line_cmt:
            if c == "\n":
                in_line_cmt = False
        elif in_block_cmt:
            if c == "*" and nxt == "/":
                in_block_cmt = False
                i += 1
        elif in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == in_str:
                in_str = None
        else:
            if c == "/" and nxt == "/":
                in_line_cmt = True
                i += 1
            elif c == "/" and nxt == "*":
                in_block_cmt = True
                i += 1
            elif c in ('"', "'", "`"):
                in_str = c
            elif c in pairs:
                stack.append(pairs[c])
            elif c in pairs.values():
                if not stack or stack[-1] != c:
                    return False
                stack.pop()
        i += 1
    return not stack and in_str is None and not in_block_cmt


def _validate(file_path: Path, text: str) -> bool:
    """Post-LLM validation. Rejects output that still has markers, is empty,
    or fails a light-weight parse check for the file type."""
    if not text or _has_markers(text):
        return False
    suffix = file_path.suffix.lower()
    if suffix == ".json":
        return _validate_json(text)
    if suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        return _validate_ts_basic(text)
    # Unknown type: accept if no markers
    return True


def _build_prompt(file_path: Path, text: str) -> str:
    rel = file_path.name
    return (
        f"File: {rel}\n"
        f"Size: {len(text)} bytes\n\n"
        f"<file>\n{text}\n</file>\n\n"
        f"Respond with the resolved file contents only."
    )


async def _invoke_haiku(prompt: str, model: str) -> Optional[str]:
    """Run a single-shot Haiku session with no tools. Returns the assistant's
    text response, or None on error/timeout.

    Uses ClaudeSDKClient (subscription-friendly) when available; falls back to
    the `claude` CLI path used elsewhere in the harness.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return await _invoke_via_cli(prompt, model)

    try:
        from claude_code_sdk import ClaudeCodeOptions, ClaudeSDKClient
    except ImportError:
        return await _invoke_via_cli(prompt, model)

    options = ClaudeCodeOptions(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=[],       # pure text, no tools
        max_turns=1,
        permission_mode="bypassPermissions",
    )

    output = ""
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if type(msg).__name__ == "AssistantMessage" and hasattr(msg, "content"):
                    for block in msg.content:
                        if type(block).__name__ == "TextBlock" and hasattr(block, "text"):
                            output += block.text
    except Exception:
        return None

    return output or None


def _run_claude_cli_blocking(prompt: str, model: str) -> Optional[str]:
    """Invoke `claude --print` as a subprocess for a single-shot response.
    Uses subprocess.run with argv list (no shell) to avoid injection."""
    argv = [
        "claude",
        "--print",
        "--model", model,
        "--append-system-prompt", SYSTEM_PROMPT,
        "--permission-mode", "bypassPermissions",
    ]
    try:
        proc = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
            shell=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout or None


async def _invoke_via_cli(prompt: str, model: str) -> Optional[str]:
    """Async wrapper around the blocking CLI call."""
    return await asyncio.to_thread(_run_claude_cli_blocking, prompt, model)


async def resolve_file_with_llm(
    file_path: Path,
    model: str = "claude-haiku-4-5",
    max_bytes: int = 200_000,
) -> bool:
    """Attempt to resolve a single conflicted file via LLM.

    Returns True iff the file was rewritten with a valid, marker-free version.
    Caller is responsible for `git add`-ing the file afterwards.
    """
    try:
        original = file_path.read_text(encoding="utf-8")
    except OSError:
        return False

    if not _has_markers(original):
        return True  # already resolved — nothing to do

    if len(original.encode("utf-8")) > max_bytes:
        # Too big — bail to avoid runaway token cost
        return False

    prompt = _build_prompt(file_path, original)
    raw = await _invoke_haiku(prompt, model=model)
    if raw is None:
        return False

    resolved = _strip_code_fence(raw)
    if CANNOT_RESOLVE in resolved:
        return False

    if not _validate(file_path, resolved):
        return False

    # Preserve trailing newline discipline of the original
    if original.endswith("\n") and not resolved.endswith("\n"):
        resolved += "\n"

    try:
        file_path.write_text(resolved, encoding="utf-8")
    except OSError:
        return False
    return True


def resolve_file_sync(
    file_path: Path,
    model: str = "claude-haiku-4-5",
    max_bytes: int = 200_000,
) -> bool:
    """Synchronous wrapper around `resolve_file_with_llm` for callers that are
    already inside a blocking context (like `_auto_resolve_submodule_conflicts`)."""
    try:
        return asyncio.run(
            resolve_file_with_llm(file_path, model=model, max_bytes=max_bytes)
        )
    except RuntimeError:
        # Already inside an event loop — schedule on a new one via thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(
                lambda: asyncio.new_event_loop().run_until_complete(
                    resolve_file_with_llm(file_path, model=model, max_bytes=max_bytes)
                )
            )
            return fut.result()


def try_llm_resolve_conflicts(
    submodule_dir: Path,
    conflict_files: list[str],
    model: str = "claude-haiku-4-5",
    max_bytes_per_file: int = 200_000,
) -> tuple[list[str], list[str]]:
    """Attempt LLM resolution for a batch of conflicted files under a submodule.

    Returns (resolved, unresolved) — lists of relative paths.
    Caller is responsible for `git add`-ing resolved files.
    """
    resolved: list[str] = []
    unresolved: list[str] = []
    for rel in conflict_files:
        abs_path = submodule_dir / rel
        ok = resolve_file_sync(abs_path, model=model, max_bytes=max_bytes_per_file)
        if ok:
            try:
                if not _has_markers(abs_path.read_text(encoding="utf-8")):
                    resolved.append(rel)
                    continue
            except OSError:
                pass
        unresolved.append(rel)
    return resolved, unresolved
