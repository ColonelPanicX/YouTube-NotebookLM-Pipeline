"""
nlm_router.py — NotebookLM ingestion and report generation.

Two-phase design (deliberate):

  Phase 1 — ingest_video()
      Adds a video URL as a source to the appropriate NLM notebook.
      Does not generate a report. Run once per video.

  Phase 2 — generate_notebook_report()
      Generates a Briefing Doc for a completed notebook and saves it to
      reports/<slug>-<period>.md. Run once per notebook after all videos
      for that period are ingested.

Authentication
--------------
Uses notebooklm-py (https://github.com/teng-lin/notebooklm-py).
Run `notebooklm login` once to store your Google session cookie.
No API key required — authentication piggybacks on your Google account.

Notebook naming
---------------
Monthly:   "<label> - YYYY-MM"    e.g. "My Channel - 2026-04"
Quarterly: "<label> - QN YYYY"    e.g. "My Channel - Q1 2026"

Sealing
-------
NotebookLM caps notebooks at ~180 sources. When a notebook hits
NOTEBOOK_SEAL_THRESHOLD, it is sealed and subsequent videos for the same
period go into a new notebook automatically.
"""

import logging
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

import db
from notebooklm import NotebookLMClient, ReportFormat

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).parent / "reports"
_REPORT_TIMEOUT = 600  # seconds — NLM can be slow on large notebooks


# ---------------------------------------------------------------------------
# Client session helper
# ---------------------------------------------------------------------------

@asynccontextmanager
async def client_session() -> AsyncIterator[NotebookLMClient]:
    """Open a single authenticated NLM session for the caller to reuse."""
    async with await NotebookLMClient.from_storage() as client:
        yield client


# ---------------------------------------------------------------------------
# Period label helpers
# ---------------------------------------------------------------------------

def _period_label(channel_label: str, ref_dt: datetime, period: str) -> str:
    if period == "quarterly":
        quarter = (ref_dt.month - 1) // 3 + 1
        return f"{channel_label} - Q{quarter} {ref_dt.year}"
    return f"{channel_label} - {ref_dt.year}-{ref_dt.month:02d}"


# ---------------------------------------------------------------------------
# Phase 1: source ingestion
# ---------------------------------------------------------------------------

async def ingest_video(
    client: NotebookLMClient,
    video_id: str,
    video_url: str,
    video_title: str,
    channel_id: str,
    channel_label: str,
    channel_slug: str,
    conn: aiosqlite.Connection,
    published_at: str | None = None,
    period: str = "monthly",
) -> tuple[str, str]:
    """
    Add a single video to its NLM notebook and mark it processed.

    Returns (notebook_id, period_label).
    """
    if published_at:
        try:
            ref_dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except ValueError:
            ref_dt = datetime.now(timezone.utc)
    else:
        ref_dt = datetime.now(timezone.utc)

    label = _period_label(channel_label, ref_dt, period)
    logger.info("Routing %s → notebook %r", video_id, label)

    # Find or create the notebook for this channel/period
    existing = await db.get_active_notebook(conn, channel_id, label)
    if existing:
        notebook_id = existing["notebook_id"]
    else:
        logger.info("Creating notebook: %r", label)
        notebook = await client.notebooks.create(title=label)
        notebook_id = notebook.id
        await db.register_notebook(conn, notebook_id, channel_id, channel_slug, label)

    source = await client.sources.add_url(notebook_id, video_url, wait=True)
    logger.info("Source ingested: %s", source.id)

    await db.mark_video_processed(conn, video_id, channel_id, notebook_id, video_title, published_at)
    return notebook_id, label


# ---------------------------------------------------------------------------
# Phase 2: report generation
# ---------------------------------------------------------------------------

async def generate_notebook_report(
    client: NotebookLMClient,
    notebook_id: str,
    slug: str,
    period_label: str,
) -> Path:
    """
    Generate a Briefing Doc for notebook_id and save it to reports/.

    The period_label is the full label string, e.g. "My Channel - 2026-04".
    The saved filename strips the channel prefix: reports/<slug>-2026-04.md.
    """
    # Strip "<label> - " prefix to get the bare period (e.g. "2026-04")
    period_key = period_label.split(" - ", 1)[-1].replace(" ", "-")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{slug}-{period_key}.md"

    logger.info("Generating Briefing Doc for %s (%s)…", notebook_id[:8], period_key)

    status = await client.artifacts.generate_report(
        notebook_id,
        report_format=ReportFormat.BRIEFING_DOC,
    )
    if not status.task_id:
        raise RuntimeError(
            f"NLM rejected report generation for {notebook_id}: {status.error}"
        )

    final = await client.artifacts.wait_for_completion(
        notebook_id, status.task_id, timeout=_REPORT_TIMEOUT
    )
    if final.is_failed:
        raise RuntimeError(
            f"NLM report generation failed for {notebook_id}: {final.error}"
        )

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".md")
    os.close(tmp_fd)
    try:
        await client.artifacts.download_report(
            notebook_id, tmp_path, artifact_id=final.task_id
        )
        content = Path(tmp_path).read_text(encoding="utf-8")
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    report_path.write_text(content, encoding="utf-8")
    logger.info("Report saved: %s (%d chars)", report_path.name, len(content))
    return report_path
