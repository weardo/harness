"""
Knowledge Client — Formats knowledge chunks for planner injection.
"""

from typing import Optional


def format_knowledge_for_planner(chunks: list) -> str:
    """Format knowledge chunks into a markdown section for planner context.

    Groups by chunk_type and renders as a structured section the planner
    can use to avoid known failure modes.
    """
    if not chunks:
        return ""

    # Map raw chunk_type values to display headers (plural form)
    TYPE_HEADERS = {
        "failure_mode": "FAILURE MODES",
        "pattern": "PATTERNS",
        "convention": "CONVENTIONS",
        "retro_section": "RETRO SECTIONS",
    }

    grouped: dict[str, list[str]] = {}
    for chunk in chunks:
        raw_type = chunk.get("chunk_type", "other")
        ctype = TYPE_HEADERS.get(raw_type, raw_type.upper().replace("_", " "))
        content = chunk.get("content", "").strip()
        if content:
            grouped.setdefault(ctype, []).append(content)

    if not grouped:
        return ""

    lines = [
        "",
        "## LEARNINGS FROM PREVIOUS RUNS",
        "",
        "The following failure modes, patterns, and conventions were learned from past harness runs.",
        "You MUST review each failure mode and ensure your feature list guards against it.",
        "",
    ]

    # Failure modes first (most important)
    priority_order = ["FAILURE MODES", "PATTERNS", "CONVENTIONS", "RETRO SECTIONS"]
    seen = set()
    for ctype in priority_order:
        if ctype in grouped:
            seen.add(ctype)
            lines.append(f"### {ctype}")
            lines.append("")
            for i, content in enumerate(grouped[ctype], 1):
                lines.append(f"{i}. {content}")
                lines.append("")

    # Any remaining types
    for ctype, contents in grouped.items():
        if ctype not in seen:
            lines.append(f"### {ctype}")
            lines.append("")
            for i, content in enumerate(contents, 1):
                lines.append(f"{i}. {content}")
                lines.append("")

    return "\n".join(lines)
