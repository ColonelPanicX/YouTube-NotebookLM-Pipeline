"""
poller.py — YouTube channel monitor.

Fetches a channel's upload history from the YouTube Data API and returns
videos not yet recorded in the pipeline database.

Public API
----------
poll_channel(channel_handle, api_key, conn) -> list[VideoMeta]
    Returns unprocessed videos, oldest-first.
"""

import logging
import re
from dataclasses import dataclass

import httpx

import db

logger = logging.getLogger(__name__)

_YT_API = "https://www.googleapis.com/youtube/v3"
_PAGE_SIZE = 50

# Videos at or under this duration are treated as Shorts and skipped.
_SHORT_MAX_SECONDS = 60


@dataclass
class VideoMeta:
    video_id: str
    title: str
    channel_id: str
    published_at: str  # ISO 8601
    url: str


async def _get_channel_info(client: httpx.AsyncClient, handle: str, api_key: str) -> dict:
    """Resolve a channel handle to its channel ID and uploads playlist ID."""
    resp = await client.get(
        f"{_YT_API}/channels",
        params={
            "part": "snippet,contentDetails",
            "forHandle": handle.lstrip("@"),
            "key": api_key,
        },
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    if not items:
        raise ValueError(f"No YouTube channel found for handle: {handle!r}")
    item = items[0]
    return {
        "channel_id": item["id"],
        "title": item["snippet"]["title"],
        "uploads_playlist_id": item["contentDetails"]["relatedPlaylists"]["uploads"],
    }


async def _list_playlist_videos(
    client: httpx.AsyncClient, playlist_id: str, api_key: str
) -> list[dict]:
    """Fetch all videos from an uploads playlist, handling pagination."""
    videos: list[dict] = []
    next_page_token: str | None = None

    while True:
        params: dict = {
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": _PAGE_SIZE,
            "key": api_key,
        }
        if next_page_token:
            params["pageToken"] = next_page_token

        resp = await client.get(f"{_YT_API}/playlistItems", params=params)
        resp.raise_for_status()
        data = resp.json()
        videos.extend(data.get("items", []))

        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    return videos


def _parse_iso8601_duration(duration: str) -> int:
    """Return total seconds from an ISO 8601 duration string (e.g. PT1M30S)."""
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    if not match:
        return 0
    return int(match.group(1) or 0) * 3600 + int(match.group(2) or 0) * 60 + int(match.group(3) or 0)


async def _get_video_durations(
    client: httpx.AsyncClient, video_ids: list[str], api_key: str
) -> dict[str, int]:
    """Return {video_id: duration_seconds} in batches of 50."""
    durations: dict[str, int] = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        resp = await client.get(
            f"{_YT_API}/videos",
            params={"part": "contentDetails", "id": ",".join(batch), "key": api_key},
        )
        resp.raise_for_status()
        for item in resp.json().get("items", []):
            raw = item.get("contentDetails", {}).get("duration", "")
            durations[item["id"]] = _parse_iso8601_duration(raw)
    return durations


async def poll_channel(channel_handle: str, api_key: str, conn) -> list[VideoMeta]:
    """
    Poll a YouTube channel for videos not yet in the pipeline database.

    Parameters
    ----------
    channel_handle : str
        YouTube channel handle, with or without leading "@". Example: "@mkbhd"
    api_key : str
        YouTube Data API v3 key.
    conn : aiosqlite.Connection
        Open database connection.

    Returns
    -------
    list[VideoMeta]
        Unprocessed videos, sorted oldest-first. Empty if up-to-date.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        channel_info = await _get_channel_info(client, channel_handle, api_key)
        channel_id = channel_info["channel_id"]
        playlist_id = channel_info["uploads_playlist_id"]
        logger.info("Channel: %r  id=%s", channel_info["title"], channel_id)

        raw_items = await _list_playlist_videos(client, playlist_id, api_key)
        logger.info("Found %d uploads (including Shorts)", len(raw_items))

        all_videos = [
            VideoMeta(
                video_id=item["snippet"]["resourceId"]["videoId"],
                title=item["snippet"].get("title", ""),
                channel_id=channel_id,
                published_at=item["snippet"].get("publishedAt", ""),
                url=f"https://www.youtube.com/watch?v={item['snippet']['resourceId']['videoId']}",
            )
            for item in raw_items
        ]

        durations = await _get_video_durations(client, [v.video_id for v in all_videos], api_key)

    # Filter Shorts (≤60s). Unknown duration = keep (not a Short).
    all_videos = [
        v for v in all_videos
        if durations.get(v.video_id, _SHORT_MAX_SECONDS + 1) > _SHORT_MAX_SECONDS
    ]
    logger.info("%d full-length videos after Short filter", len(all_videos))

    processed_ids = await db.get_processed_video_ids(conn, channel_id)
    new_videos = [v for v in all_videos if v.video_id not in processed_ids]
    new_videos.sort(key=lambda v: v.published_at)

    logger.info("%d new video(s) to process", len(new_videos))
    return new_videos
