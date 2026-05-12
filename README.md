# YouTube → NotebookLM Knowledge Pipeline

A two-phase pipeline that monitors YouTube channels for new videos, feeds them into [NotebookLM](https://notebooklm.google.com/) for synthesis, and uses an LLM of your choice to extract only what is genuinely new into a growing `knowledge-base.md` file.

The core idea: instead of re-summarizing everything on every run, the delta step compares the latest briefing doc against what you already know and writes only what's new or meaningfully updated. Nothing is ever removed automatically.

```
YouTube
  └─ poller.py ──→ NotebookLM notebooks
                         └─ Briefing Doc ──→ delta.py ──→ knowledge-base.md
                                                ↑
                                         your LLM of choice
```

State (which videos and notebooks have been processed) is tracked in a local SQLite database.

---

## Why NotebookLM?

NotebookLM accepts YouTube URLs directly. It fetches transcripts, understands video structure, and synthesizes across an entire notebook of videos into a single Briefing Doc. This pipeline handles the scheduling, deduplication, and state tracking; NotebookLM handles the summarization.

---

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- A [YouTube Data API v3 key](https://console.cloud.google.com/) (free tier; ~10,000 units/day)
- A Google account with [NotebookLM](https://notebooklm.google.com/) access
- An LLM you can call (Claude, OpenAI, Ollama, or any other)

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/ColonelPanicX/YouTube-NotebookLM-Pipeline
cd yt-nlm-pipeline
uv sync
```

Or with pip:

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Open `.env` and add your YouTube API key:

```
YOUTUBE_API_KEY=AIza...
```

### 3. Log into NotebookLM

```bash
uv run notebooklm login
```

A browser window opens. Sign in with your Google account. Your session cookie is saved locally — you only need to do this once (sessions last roughly 30 days).

### 4. Wire up your LLM

Open `delta.py` and replace the body of `run_llm()` with a call to your LLM. Examples are in the **LLM Setup** section below.

### 5. Add a channel

Open `main.py` and edit the `CHANNELS` list:

```python
CHANNELS = [
    {
        "handle": "@mkbhd",           # YouTube channel handle
        "label": "MKBHD",             # used in NLM notebook titles
        "slug": "mkbhd",              # used in report filenames
        "period": "monthly",          # "monthly" or "quarterly" — see below
    },
]
```

**Choosing a period:**

| Period | When to use |
|--------|-------------|
| `monthly` | Active channels — roughly 10+ videos/month. Keeps individual briefings focused. |
| `quarterly` | Slower channels — under ~30 videos/quarter. Fewer notebooks, richer briefings. |

---

## Running the pipeline

### Full run (ingest new videos + generate reports + run delta)

```bash
uv run python main.py
```

### Ingest only (Phase 1 — add new videos to NLM, generate reports, no delta)

```bash
uv run python main.py ingest
```

### Delta only (Phase 2 — apply existing reports to knowledge-base.md)

```bash
uv run python main.py delta
```

### Options

| Flag | Effect |
|------|--------|
| `--channel @Handle` | Process a single channel only |
| `--since YYYY-MM-DD` | Skip videos published before this date |
| `--dry-run` | Poll for new videos but do not ingest |

**Example — add a new channel and backfill the last year:**

```bash
# See what would be ingested without touching NLM
uv run python main.py ingest --channel @mkbhd --since 2025-01-01 --dry-run

# Ingest for real
uv run python main.py ingest --channel @mkbhd --since 2025-01-01

# Run delta to update knowledge-base.md
uv run python main.py delta --channel @mkbhd
```

---

## LLM Setup

Open `delta.py` and find `run_llm()` at the top of the file. Replace the `raise NotImplementedError` with a call to your LLM. The function receives one string (the full prompt) and must return one string (the LLM's response).

### Claude (Anthropic SDK)

```bash
uv add anthropic
```

```python
import anthropic

_client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

def run_llm(prompt: str) -> str:
    message = _client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text
```

### OpenAI

```bash
uv add openai
```

```python
from openai import OpenAI

_client = OpenAI()  # reads OPENAI_API_KEY from env

def run_llm(prompt: str) -> str:
    response = _client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content
```

### Ollama (local)

Requires [Ollama](https://ollama.com/) running locally with a model pulled (`ollama pull llama3.2`).

```bash
uv add httpx  # already included
```

```python
import httpx

def run_llm(prompt: str) -> str:
    resp = httpx.post(
        "http://localhost:11434/api/generate",
        json={"model": "llama3.2", "prompt": prompt, "stream": False},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["response"]
```

> **Note on local models:** Smaller models (7B–13B) sometimes return JSON wrapped in markdown fences or with preamble text. `delta.py` handles the common cases, but if you see parse errors, check the raw output and adjust accordingly.

---

## What the output looks like

`knowledge-base.md` starts empty and grows over time. Sections are added and updated in place; nothing is ever removed automatically.

```markdown
# Knowledge Base

## Topic: How to do X

Content extracted from the briefing doc about X.

## Topic: Why Y matters

Content about Y, updated when the briefing doc had new information.
```

Each time you run the delta, the LLM reads the current file and the latest briefing doc, identifies what's genuinely new, and writes only those changes. The file stays readable and non-redundant.

---

## Extending the pipeline

### Multi-topic knowledge bases

The default setup puts all extracted insights from all channels into one `knowledge-base.md`. For channels that cover distinct topics, you may want separate files — `productivity.md`, `health.md`, etc.

To do this, define a topics list in `delta.py`. Each topic gets a description (telling the LLM what belongs there) and its own output path. Pass the topic description into the prompt as additional context, and write to the appropriate file instead of a single `KB_PATH`.

This is how you scale from a general catch-all to a structured multi-page knowledge base without changing the core pipeline.

### Scheduling

The pipeline is stateless between runs — run it daily, weekly, or on demand. A simple cron entry works well:

```cron
0 18 * * * cd /path/to/yt-nlm-pipeline && uv run python main.py >> logs/pipeline.log 2>&1
```

See the **Gotchas** section before enabling cron if you're using a cloud LLM — usage limits can cause problems mid-run.

---

## Gotchas

These are things that will bite you eventually. Better to know now.

### NLM session expires after ~30 days

NotebookLM authentication is cookie-based. When the session expires, ingestion will fail with an authentication error. Re-run `notebooklm login` to refresh. Consider adding a calendar reminder.

### NotebookLM processes sources asynchronously

The `wait=True` flag in `nlm_router.py` polls until ingestion is complete, but large notebooks (50+ sources) can take several minutes per video. Don't run two pipeline instances against the same NLM account simultaneously — they will conflict.

### YouTube API quota is 10,000 units/day (free tier)

Each channel poll costs roughly 100 quota units (playlist fetch + duration lookup to filter Shorts). Ten channels = ~1,000 units per run, well within quota. Backfilling years of history from multiple channels in a single day will approach or exceed the limit. Spread backfills across days using `--since`.

### Batching long backfills

When you first add a channel with a long history, do not try to ingest everything in one run. Use `--since YYYY-MM-DD` to process one year at a time and run the delta after each batch. If an LLM usage limit interrupts a run mid-way, you lose less work and can resume cleanly.

### NotebookLM has a ~180-source limit per notebook

This is a hard NLM limit. High-volume channels (daily uploads) can hit it within a month. The pipeline handles this automatically: when a notebook reaches the threshold it is sealed, and new videos for the same period go into a fresh notebook. Each sealed notebook still gets its own Briefing Doc.

### Zero-op delta runs are normal

If a channel's content for a given period has nothing new relative to your knowledge base — pure interviews, sponsored content, topics you don't care about — the LLM will return no operations. This is expected, not an error. The notebook is still marked as delta'd so it won't be processed again.

### LLM JSON reliability varies by model

The delta prompt asks for strict JSON output. GPT-4-class and Claude models handle this reliably. Smaller local models sometimes add preamble, wrap the JSON in markdown fences, or produce malformed output. `delta.py` handles the common failure modes, but if you see repeated parse errors, log the raw LLM output and inspect it.

### Do not re-run the delta on the same notebook twice

The pipeline tracks which notebooks have been delta'd (`delta_run_at` in the database). If you re-run `delta`, only unprocessed notebooks are touched. Do not manually clear `delta_run_at` unless you want to re-apply a notebook — re-applying will not duplicate content (ADD operations for existing headings become UPDATEs), but it wastes LLM calls.

### Cron + cloud LLMs can be expensive

If you enable cron and have a large backlog, the pipeline may make many LLM calls in rapid succession. Cloud LLMs (Claude, OpenAI) have rate limits and usage caps. A single misfired cron run processing hundreds of notebooks can exhaust a daily or monthly budget. Always test interactively before enabling cron, and add rate-limiting or batch size caps if you're running unattended at scale.

---

## Project structure

```
yt-nlm-pipeline/
├── main.py           — orchestrator and CLI
├── poller.py         — YouTube API polling + Short filtering
├── nlm_router.py     — NotebookLM ingestion and report generation
├── delta.py          — LLM-powered knowledge base updater
├── db.py             — SQLite state management
├── knowledge-base.md — output (grows over time)
├── reports/          — cached NLM Briefing Docs (one per notebook)
├── data/             — SQLite database
├── pyproject.toml
└── .env.example
```

---

## License

MIT
