"""
delta.py — Knowledge base updater.

Compares a NotebookLM Briefing Doc against the current knowledge-base.md
and applies only what is genuinely new or meaningfully updated. Nothing is
ever removed automatically.

LLM configuration
-----------------
Wire up your LLM by replacing the body of run_llm() below. The function
receives a single prompt string and must return a string. See README for
examples using Claude, OpenAI, and Ollama.
"""

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM interface — configure this
# ---------------------------------------------------------------------------

def run_llm(prompt: str) -> str:
    """
    Call your LLM with *prompt* and return the response text.

    The prompt will ask for JSON output. The response must be a valid JSON
    object with an "operations" array. See README for setup examples.

    Raises NotImplementedError until configured.
    """
    raise NotImplementedError(
        "Configure run_llm() in delta.py before running the delta step. "
        "See the README for examples using Claude, OpenAI, and Ollama."
    )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM = """\
You are a knowledge base curator. Your job is to extract insights from a YouTube \
channel briefing document and merge them into an existing markdown knowledge base.

Rules:
1. ADD — if the briefing document contains useful information not already in the \
knowledge base, add it as a new section.
2. UPDATE — if the briefing document contains newer or more complete information \
about a topic already in the knowledge base, rewrite that section. Integrate new \
information naturally — no "as of [date]" annotations or change history. The \
knowledge base reflects current understanding, not a changelog.
3. NEVER REMOVE — if a section is not mentioned in the briefing document, leave it \
completely unchanged. Do not include it in your output.
4. DISCARD — if information in the briefing document is already fully and accurately \
covered in the knowledge base, ignore it.

Voice:
- Clear, direct, practical. No hype, filler, or marketing language.
- Short paragraphs. Action-oriented steps where appropriate.
- Tables for comparisons. Fenced code blocks for commands or examples.
- Do not invent facts, commands, or links not present in the source material.

Output format: return valid JSON only — no text, no markdown fences, outside the JSON.

{
  "operations": [
    {
      "type": "add" | "update",
      "heading": "Section heading (plain text, no markdown)",
      "content": "Full markdown content for this section, not including the heading line"
    }
  ]
}

If there are no changes to make, return: {"operations": []}"""

_USER = """\
CURRENT KNOWLEDGE BASE:
{kb_content}

---

BRIEFING DOCUMENT (channel: {channel_label}, period: {period}):
{report_content}

Produce the JSON diff now."""


# ---------------------------------------------------------------------------
# Operation application
# ---------------------------------------------------------------------------

def _apply_operations(kb_content: str, operations: list[dict]) -> str:
    """Apply a list of ADD/UPDATE operations to knowledge base markdown."""
    for op in operations:
        heading = op.get("heading", "").strip()
        content = op.get("content", "").strip()
        op_type = op.get("type", "")

        if not heading or not content:
            continue

        heading_line = f"## {heading}"

        if op_type == "add":
            kb_content = kb_content.rstrip("\n") + f"\n\n{heading_line}\n\n{content}\n"

        elif op_type == "update":
            lines = kb_content.split("\n")
            start = next(
                (i for i, line in enumerate(lines) if line.strip() == heading_line),
                None,
            )
            if start is None:
                # Heading not found — treat as add
                kb_content = kb_content.rstrip("\n") + f"\n\n{heading_line}\n\n{content}\n"
            else:
                end = next(
                    (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
                    len(lines),
                )
                new_lines = lines[:start] + [heading_line, "", content, ""] + lines[end:]
                kb_content = "\n".join(new_lines)

    return kb_content


def _parse_response(raw: str) -> list[dict]:
    """Extract and parse the JSON operations array from the LLM response."""
    # Strip markdown fences if present
    fenced = re.search(r"```(?:json)?\s*\n([\s\S]*?)\n```", raw)
    if fenced:
        raw = fenced.group(1).strip()
    elif not raw.strip().startswith("{"):
        # Find the last {...} block as a fallback
        start = raw.rfind("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            raw = raw[start:end]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"delta.py: LLM returned invalid JSON:\n{raw[:500]}") from exc

    return data.get("operations", [])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_delta(
    report_content: str,
    kb_path: Path,
    channel_label: str,
    period: str,
) -> str:
    """
    Compare *report_content* against *kb_path* and return updated knowledge
    base content. Does not write the file — caller is responsible.

    Parameters
    ----------
    report_content : str
        NLM Briefing Doc markdown for the period being processed.
    kb_path : Path
        Path to knowledge-base.md (read for current content; may not exist yet).
    channel_label : str
        Human-readable channel name, used in the prompt for context.
    period : str
        Period label (e.g. "2026-04"), used in the prompt for context.

    Returns
    -------
    str
        Updated knowledge base content, ready to write back to kb_path.
    """
    kb_content = kb_path.read_text(encoding="utf-8") if kb_path.exists() else ""

    prompt = f"{_SYSTEM}\n\n" + _USER.format(
        kb_content=kb_content or "(empty — knowledge base does not exist yet)",
        channel_label=channel_label,
        period=period,
        report_content=report_content,
    )

    logger.info("Running delta for %s / %s", channel_label, period)
    raw = run_llm(prompt)
    operations = _parse_response(raw)

    if not operations:
        logger.info("No changes for %s / %s", channel_label, period)
        return kb_content

    logger.info(
        "%d operation(s): %s",
        len(operations),
        ", ".join(f"{o.get('type')}:{o.get('heading', '')[:30]}" for o in operations),
    )
    return _apply_operations(kb_content, operations)
