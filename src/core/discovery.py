"""
Discovery -- HANDOFF parsing, brief compression, and relay context for parallel agents.

Ported from Citadel's core/fleet/compress-discovery.js and parse-handoff.js.
"""

import re
from pathlib import Path
from typing import Optional


def parse_handoff(output: str) -> dict:
    """Parse ---HANDOFF--- block from generator output.

    Returns {found: bool, items: list[str], raw: str}.
    Handles: no block, block at end of output, block with no closing ---.
    """
    pattern = r"---\s*HANDOFF\s*---\s*\n([\s\S]*?)(?:\n---|$)"
    match = re.search(pattern, output, re.IGNORECASE)
    if not match:
        return {"found": False, "items": [], "raw": ""}

    raw = match.group(1).strip()
    items = [
        re.sub(r"^[-*]\s*", "", line).strip()
        for line in raw.split("\n")
        if line.strip()
    ]
    return {"found": True, "items": items, "raw": raw}


def extract_decisions(text: str) -> list[str]:
    """Extract decision-related lines (decided, chose, picked, etc). Max 5."""
    decisions = []
    for line in text.split("\n"):
        if re.search(r"\b(decided|decision|chose|chosen|picked)\b", line, re.IGNORECASE):
            if line.strip() and len(line) < 200:
                decisions.append(re.sub(r"^[-*]\s*", "", line).strip())
    return decisions[:5]


def extract_files(text: str) -> list[str]:
    """Extract file paths matching src/lib/app/test/etc patterns. Max 10."""
    matches = re.findall(
        r"(?:src|lib|app|pages|components|api|test|spec)/[\w\-./]+\.\w+",
        text,
    )
    seen: set[str] = set()
    unique: list[str] = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            unique.append(m)
    return unique[:10]


def extract_failures(text: str) -> list[str]:
    """Extract failure-related lines (failed, error, broke, blocked, etc). Max 3."""
    failures = []
    for line in text.split("\n"):
        if re.search(
            r"\b(failed|error|broke|broken|couldn't|cannot|blocked)\b",
            line,
            re.IGNORECASE,
        ):
            if line.strip() and len(line) < 200:
                failures.append(re.sub(r"^[-*]\s*", "", line).strip())
    return failures[:3]


def compress_discovery(
    output: str,
    agent_id: str,
    feature_id: str,
    status: str = "",
) -> str:
    """Compress full agent output to ~500-token markdown brief.

    Structure:
    ## Agent: {agent_id} | Feature: {feature_id}
    **Status:** {status}
    **Built:** {from handoff items}
    **Decisions:** {extracted decisions}
    **Files:** {extracted file paths}
    **Failures:** {extracted failures}
    """
    handoff = parse_handoff(output)
    decisions = extract_decisions(output)
    files = extract_files(output)
    failures = extract_failures(output)

    # Infer status if not provided
    effective_status = status or ("partial" if failures else "complete")

    lines = [f"## Agent: {agent_id} | Feature: {feature_id}"]
    lines.append(f"**Status:** {effective_status}")

    if handoff["found"] and handoff["items"]:
        lines.append(f"**Built:** {'. '.join(handoff['items'][:2])}")
        if len(handoff["items"]) > 2:
            lines.append(f"**Remaining:** {'; '.join(handoff['items'][2:])}")

    if decisions:
        lines.append("**Decisions:**")
        for d in decisions:
            lines.append(f"- {d}")

    if failures:
        lines.append("**Failures:**")
        for f in failures:
            lines.append(f"- {f}")

    if files:
        lines.append(f"**Files:** {', '.join(files)}")

    brief = "\n".join(lines)

    # Trim to ~2000 chars (~500 tokens) if needed
    if len(brief) > 2000:
        brief = brief[:1997] + "..."

    return brief


def write_brief(brief: str, wave: int, agent_id: str, state_dir: Path) -> Path:
    """Write brief to .harness/fleet/briefs/w{wave}-{agent_id}.md.

    Returns path to written file. Creates directories as needed.
    """
    briefs_dir = Path(state_dir).parent / "fleet" / "briefs"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    path = briefs_dir / f"w{wave}-{agent_id}.md"
    path.write_text(brief)
    return path


def get_relay_context(state_dir: Path, current_wave: int) -> str:
    """Concatenate all briefs from waves < current_wave.

    Returns:
    === DISCOVERY RELAY ===
    Discoveries from previous waves that you MUST take into account:

    {brief contents}

    === END DISCOVERY RELAY ===

    Returns "" if no prior briefs exist.
    """
    prior = list_briefs(Path(state_dir), wave=None)
    # Filter to waves strictly less than current_wave and sort by wave number
    relevant: list[tuple[int, Path]] = []
    for p in prior:
        wave_num = _parse_wave_from_filename(p.name)
        if wave_num is not None and wave_num < current_wave:
            relevant.append((wave_num, p))
    relevant.sort(key=lambda t: t[0])

    if not relevant:
        return ""

    parts = []
    for _, p in relevant:
        parts.append(p.read_text())

    body = "\n\n".join(parts)
    return (
        "=== DISCOVERY RELAY ===\n"
        "Discoveries from previous waves that you MUST take into account:\n\n"
        f"{body}\n\n"
        "=== END DISCOVERY RELAY ==="
    )


def list_briefs(state_dir: Path, wave: Optional[int] = None) -> list[Path]:
    """List all brief files, optionally filtered by wave number."""
    briefs_dir = Path(state_dir).parent / "fleet" / "briefs"
    if not briefs_dir.exists():
        return []

    files = sorted(briefs_dir.glob("w*-*.md"))
    if wave is None:
        return files

    return [f for f in files if _parse_wave_from_filename(f.name) == wave]


def _parse_wave_from_filename(name: str) -> Optional[int]:
    """Extract wave number from a brief filename like 'w2-agent-a.md'."""
    m = re.match(r"w(\d+)-", name)
    if m:
        return int(m.group(1))
    return None
