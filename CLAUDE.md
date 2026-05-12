# yt-nlm-pipeline

> **Foundation environment.** Read `.collab/foundation.md` before acting — it's the quick-start: rules, protocols, and resource pointers.
> For deeper environment context, the full manual is at `~/code/.collab/guides/daneel-ops-guide/`.

---

## What This Is

A public-facing, open-source pipeline that monitors YouTube channels, feeds videos into NotebookLM for synthesis, and uses any LLM to extract only new or updated insights into a growing `knowledge-base.md` file. The delta step is the core value: it never re-summarizes from scratch — it only writes what is genuinely new relative to what the file already contains.

## Design Constraints

1. **LLM-agnostic.** The delta step uses a `run_llm(prompt: str) -> str` stub in `delta.py`. No specific LLM SDK is in `pyproject.toml`. Users bring their own.
2. **No external services beyond NLM and YouTube.** No wiki, no database server, no cloud storage — just SQLite and flat files.
3. **Output is portable Markdown.** `knowledge-base.md` is a single file. Multi-topic extension (separate files per topic) is documented in the README as an extension pattern, not built in.
4. **Public audience.** The code, README, and any docs must make no assumptions about the user's stack, interests, or infrastructure. No Nick-specific references.
5. **Starter code, not a framework.** Keep it minimal. Don't add abstraction layers or plugin systems. If it's not needed for the core pipeline, it doesn't belong here.

## Project Structure

```
yt-nlm-pipeline/
├── main.py           — CLI orchestrator (ingest / delta / run subcommands)
├── poller.py         — YouTube Data API v3 polling, Short filtering, dedup
├── nlm_router.py     — NotebookLM ingestion (Phase 1) + Briefing Doc generation (Phase 2)
├── delta.py          — LLM prompt, response parsing, and knowledge-base.md apply logic
├── db.py             — SQLite schema and helpers (processed_videos, notebooks tables)
├── knowledge-base.md — output file (starts with a header comment, grows over time)
├── reports/          — cached NLM Briefing Docs, one per notebook (gitignored)
├── data/             — SQLite database (gitignored)
├── pyproject.toml    — uv/hatch project; no LLM deps
├── .env.example      — YOUTUBE_API_KEY, DB_PATH
└── README.md         — the full guide (setup, usage, LLM examples, gotchas)
```

## Conventions

- **Notebook naming:** `"<channel label> - YYYY-MM"` (monthly) or `"<channel label> - QN YYYY"` (quarterly). The period key stripped from the label is used in report filenames: `reports/<slug>-2026-04.md`.
- **Delta tracking:** `notebooks.delta_run_at` is NULL until the delta has been run for that notebook. `db.get_undelta_notebooks()` drives the delta phase. Do not reprocess already-delta'd notebooks.
- **Sealing:** Notebooks are sealed at 180 sources (NLM limit). `db.mark_video_processed` handles this automatically.
- **Shorts filtering:** Videos ≤60 seconds are skipped in `poller.py`. Duration is fetched via a separate `videos.list` API call batched at 50.
- **No wiki_pages table.** The internal pipeline this was derived from had wiki.js integration — that is intentionally removed here. Do not re-add it.
- **Async pattern:** `main.py`, `poller.py`, `nlm_router.py`, and `db.py` are async (aiosqlite + httpx). `delta.py` is sync — the LLM call is expected to be a simple blocking call. Keep this separation.

---

## Foundation Metadata

| Field | Value |
|---|---|
| Visibility | public |
| Tracker | GitHub Issues |
| Ticket prefix | `YTNLM` |
| Governance | standard |
| Scaffolded | 05.12.2026 |
