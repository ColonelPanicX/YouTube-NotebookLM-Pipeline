"""
db.py — SQLite state management.

Tables:
  processed_videos — every video that has completed Phase 1 ingestion
  notebooks        — NLM notebooks per channel/period, with delta tracking
"""

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_videos (
    video_id      TEXT PRIMARY KEY,
    channel_id    TEXT NOT NULL,
    notebook_id   TEXT NOT NULL,
    title         TEXT,
    published_at  TEXT,
    processed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notebooks (
    notebook_id   TEXT PRIMARY KEY,
    channel_id    TEXT NOT NULL,
    channel_slug  TEXT NOT NULL,
    period_label  TEXT NOT NULL,   -- e.g. "My Channel - 2026-04"
    source_count  INTEGER NOT NULL DEFAULT 0,
    sealed        INTEGER NOT NULL DEFAULT 0,  -- 1 when full (180 sources)
    delta_run_at  TEXT,                         -- NULL = not yet delta'd
    created_at    TEXT NOT NULL
);
"""

# NLM hard limit is ~180 sources per notebook.
NOTEBOOK_SEAL_THRESHOLD = 180


async def get_connection(db_path: str | Path) -> aiosqlite.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_SCHEMA)
    await conn.commit()
    return conn


async def get_processed_video_ids(conn: aiosqlite.Connection, channel_id: str) -> set[str]:
    async with conn.execute(
        "SELECT video_id FROM processed_videos WHERE channel_id = ?", (channel_id,)
    ) as cur:
        rows = await cur.fetchall()
    return {row["video_id"] for row in rows}


async def mark_video_processed(
    conn: aiosqlite.Connection,
    video_id: str,
    channel_id: str,
    notebook_id: str,
    title: str | None = None,
    published_at: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        """
        INSERT INTO processed_videos (video_id, channel_id, notebook_id, title, published_at, processed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (video_id, channel_id, notebook_id, title, published_at, now),
    )
    await conn.execute(
        "UPDATE notebooks SET source_count = source_count + 1 WHERE notebook_id = ?",
        (notebook_id,),
    )
    await conn.execute(
        "UPDATE notebooks SET sealed = 1 WHERE notebook_id = ? AND source_count >= ?",
        (notebook_id, NOTEBOOK_SEAL_THRESHOLD),
    )
    await conn.commit()


async def get_active_notebook(
    conn: aiosqlite.Connection, channel_id: str, period_label: str
) -> dict | None:
    async with conn.execute(
        """
        SELECT notebook_id, source_count
        FROM notebooks
        WHERE channel_id = ? AND period_label = ? AND sealed = 0
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (channel_id, period_label),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def register_notebook(
    conn: aiosqlite.Connection,
    notebook_id: str,
    channel_id: str,
    channel_slug: str,
    period_label: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        """
        INSERT INTO notebooks
            (notebook_id, channel_id, channel_slug, period_label, source_count, sealed, created_at)
        VALUES (?, ?, ?, ?, 0, 0, ?)
        """,
        (notebook_id, channel_id, channel_slug, period_label, now),
    )
    await conn.commit()


async def get_undelta_notebooks(conn: aiosqlite.Connection, channel_slug: str) -> list[dict]:
    """Return notebooks for a channel that have not been delta'd yet, oldest first."""
    async with conn.execute(
        """
        SELECT notebook_id, period_label
        FROM notebooks
        WHERE channel_slug = ? AND delta_run_at IS NULL
        ORDER BY created_at ASC
        """,
        (channel_slug,),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def mark_notebook_delta_run(conn: aiosqlite.Connection, notebook_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        "UPDATE notebooks SET delta_run_at = ? WHERE notebook_id = ?",
        (now, notebook_id),
    )
    await conn.commit()
