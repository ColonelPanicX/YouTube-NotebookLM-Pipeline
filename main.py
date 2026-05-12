"""
main.py — Pipeline orchestrator.

Usage
-----
Full run (ingest new videos + generate reports + run delta):
    uv run python main.py

Ingest only (Phase 1 — no delta):
    uv run python main.py ingest

Delta only (Phase 2 — process existing reports):
    uv run python main.py delta

Common flags:
    --channel @Handle       Process a single channel only
    --since YYYY-MM-DD      Skip videos published before this date
    --dry-run               Poll for new videos but do not ingest

Configuration
-------------
Add your channels to the CHANNELS list below.
All secrets come from .env (see .env.example).

Required:
    YOUTUBE_API_KEY     — YouTube Data API v3 key
    DB_PATH             — SQLite database path (default: data/pipeline.db)
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

import db
import delta
import nlm_router
import poller

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("main")

KB_PATH = Path("knowledge-base.md")


# ---------------------------------------------------------------------------
# Channel configuration — add your channels here
# ---------------------------------------------------------------------------
# handle  : YouTube channel handle (with or without leading @)
# label   : human-friendly name used in NLM notebook titles
# slug    : short identifier used in report filenames (lowercase, hyphenated)
# period  : "monthly" for active channels (>~10 videos/month)
#           "quarterly" for slower channels (<~30 videos/quarter)
#
# Choosing a period:
#   Monthly notebooks keep individual reports focused and fast to generate.
#   Quarterly notebooks work better for low-volume channels — fewer notebooks
#   to manage and each one has enough content to produce a useful briefing doc.

CHANNELS: list[dict] = [
    {
        "handle": "@YourChannelHandle",
        "label": "Channel Name",
        "slug": "channel-name",
        "period": "monthly",
    },
    # Add more channels here:
    # {
    #     "handle": "@AnotherChannel",
    #     "label": "Another Channel",
    #     "slug": "another-channel",
    #     "period": "quarterly",
    # },
]


# ---------------------------------------------------------------------------
# Phase 1: ingest + report
# ---------------------------------------------------------------------------

async def run_ingest(channels: list[dict], api_key: str, db_path: str, dry_run: bool, since: str | None) -> None:
    conn = await db.get_connection(db_path)
    try:
        for channel in channels:
            handle = channel["handle"]
            label = channel["label"]
            slug = channel["slug"]
            period = channel.get("period", "monthly")

            logger.info("─── %s (%s) ───", label, handle)

            new_videos = await poller.poll_channel(handle, api_key, conn)

            if since:
                new_videos = [v for v in new_videos if v.published_at[:10] >= since]
                logger.info("%d video(s) on or after %s", len(new_videos), since)

            if not new_videos:
                logger.info("Nothing to do.")
                continue

            if dry_run:
                logger.info("[DRY RUN] Would process %d video(s):", len(new_videos))
                for v in new_videos:
                    logger.info("  %s  %s", v.published_at[:10], v.title)
                continue

            # One NLM session for all Phase A + Phase B work on this channel.
            async with nlm_router.client_session() as client:
                # Phase A — add all videos to NLM notebooks
                notebooks_to_report: dict[str, tuple[str, str]] = {}
                for video in new_videos:
                    logger.info("Ingesting [%s] %s", video.published_at[:10], video.title)
                    try:
                        notebook_id, period_label = await nlm_router.ingest_video(
                            client=client,
                            video_id=video.video_id,
                            video_url=video.url,
                            video_title=video.title,
                            channel_id=video.channel_id,
                            channel_label=label,
                            channel_slug=slug,
                            conn=conn,
                            published_at=video.published_at,
                            period=period,
                        )
                        notebooks_to_report.setdefault(notebook_id, (slug, period_label))
                    except Exception:
                        logger.exception("Failed to ingest %s — skipping", video.video_id)

                # Phase B — generate one Briefing Doc per notebook
                for notebook_id, (nb_slug, period_label) in notebooks_to_report.items():
                    try:
                        report_path = await nlm_router.generate_notebook_report(client, notebook_id, nb_slug, period_label)
                        logger.info("Report saved: %s", report_path.name)
                    except Exception:
                        logger.exception("Failed to generate report for notebook %s", notebook_id[:8])
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Phase 2: delta
# ---------------------------------------------------------------------------

async def run_delta(channels: list[dict], db_path: str) -> None:
    conn = await db.get_connection(db_path)
    try:
        for channel in channels:
            slug = channel["slug"]
            label = channel["label"]

            pending = await db.get_undelta_notebooks(conn, slug)
            if not pending:
                logger.info("%s: no pending notebooks to delta.", label)
                continue

            logger.info("%s: %d notebook(s) to delta.", label, len(pending))

            for nb in pending:
                period_key = nb["period_label"].split(" - ", 1)[-1].replace(" ", "-")
                report_path = Path("reports") / f"{slug}-{period_key}.md"

                if not report_path.exists():
                    logger.warning("Report not found: %s — skipping (run ingest first)", report_path.name)
                    continue

                report_content = report_path.read_text(encoding="utf-8")
                try:
                    updated = delta.run_delta(report_content, KB_PATH, label, period_key)
                    KB_PATH.write_text(updated, encoding="utf-8")
                    await db.mark_notebook_delta_run(conn, nb["notebook_id"])
                    logger.info("Delta applied: %s / %s", label, period_key)
                except NotImplementedError:
                    logger.error(
                        "run_llm() is not configured in delta.py. "
                        "See the README for setup instructions."
                    )
                    return
                except Exception:
                    logger.exception("Delta failed for %s / %s", label, period_key)
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="YouTube → NotebookLM → Markdown knowledge pipeline",
    )
    sub = parser.add_subparsers(dest="command")

    for cmd in ("ingest", "delta", "run"):
        p = sub.add_parser(cmd)
        p.add_argument("--channel", metavar="HANDLE", help="Process a single channel only.")
        if cmd != "delta":
            p.add_argument("--since", metavar="YYYY-MM-DD", help="Skip videos before this date.")
            p.add_argument("--dry-run", action="store_true", help="Poll but do not ingest.")

    # Default (no subcommand) runs everything
    parser.add_argument("--channel", metavar="HANDLE")
    parser.add_argument("--since", metavar="YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")

    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    command = args.command or "run"

    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key and command != "delta":
        logger.error("YOUTUBE_API_KEY is not set. Add it to your .env file.")
        sys.exit(1)

    db_path = os.environ.get("DB_PATH", "data/pipeline.db")

    channels = CHANNELS
    if getattr(args, "channel", None):
        handle = args.channel.lower().lstrip("@")
        match = next((c for c in CHANNELS if c["handle"].lower().lstrip("@") == handle), None)
        channels = [match] if match else [{"handle": args.channel, "label": args.channel.lstrip("@"), "slug": handle, "period": "monthly"}]

    since = getattr(args, "since", None)
    dry_run = getattr(args, "dry_run", False)

    if command in ("ingest", "run"):
        await run_ingest(channels, api_key, db_path, dry_run=dry_run, since=since)

    if command in ("delta", "run") and not dry_run:
        await run_delta(channels, db_path)


if __name__ == "__main__":
    asyncio.run(_main())
