# 📊 Congiuntura Live

**Aggregator and LLM processor of RSS feeds from European official statistics agencies.**

Monitors press releases from **Eurostat**, **Istat**, **INE** (Spain), **INSEE** (France),
**Destatis** (Germany), and **CSO** (Ireland), deduplicates them, stores them in MongoDB, processes them
with structured LLM extraction via [outlines-cascade](https://pypi.org/project/outlines-cascade/),
and serves a searchable web interface with live updates via HTMX.

---

## Features

### Phase 1 — Feed aggregation (complete)

- **6 statistical agencies** monitored (12 feeds, ~300+ releases)
- **Automatic deduplication** via SHA-256 URL hashing
- **Configurable polling** — default 5 minutes
- **Flexible feed configuration** — edit `config/feeds.toml` without touching code
- **Dockerized** — app + MongoDB as separate containers

### Phase 2 — LLM processing (complete)

- **Structured extraction** via outlines-cascade (topic, country, sentiment, EN summary, key figures)
- **Anti-hallucination design** — the LLM never sees URLs, dates, or publishers; those are
  copied verbatim from the raw feed after generation
- **Content scraping** — trafilatura extracts the full press release text for richer LLM input
- **Configurable extraction model** — edit `config/extraction_model.py` to change what the
  LLM extracts; the web UI auto-generates filter controls from the model
- **Cloud LLM backends** — Groq + LLM7 (both OpenAI-compatible), with cascade failover
- **Incremental processing** — only processes raw items not yet in the processed collection
- **Auto-generated filter UI** — selectors for Literal fields, calendar input for date fields
- **English titles** — every processed release carries `title_en` (original title preserved
  and shown with the translation below it); existing records are backfilled at startup with
  lightweight title-only LLM calls

### Release calendar (new in 0.5.0)

- **`/calendar` page** — scheduled data releases from 6 NSOs + ForexFactory (EUR events)
  in a dedicated calendar view, distinct from the press-release list
- **7 collectors**: Eurostat/Istat/INE (ICS feeds), Destatis (annual calendar via topic
  facets), INSEE (embargo calendar), CSO (PxStat API), ForexFactory (monthly HTML scrape
  with optional `FF_PROXY_URL` proxy)
- **Daily updates** — APScheduler cron at 07:00 UTC; ForexFactory window covers
  (current month − 1) → (current month + 3), history is never deleted
- **Two MongoDB collections** (`nso_releases`, `ff_releases`) with idempotent
  `source_uid` upserts; FF records store actual/forecast/previous and are updated
  as values are published
- **Pure scraping** — calendar records are never touched by any LLM

### Live update indicators (new in 0.5.1)

- **"Last update" badge** on every page (home, raw feeds, calendar), upper-left
  above the filters — server-rendered on load, then kept live via WebSocket
- **`update_status` helper collection** in MongoDB records each pipeline's last
  run (timestamp, ok/partial status, details); updated by the feed poller
  (every 5 min) and the calendar cron (daily 07:00 UTC)
- **`/ws` WebSocket endpoint** pushes status changes to all connected clients
  the moment a run completes — no polling; auto-reconnects on drop

---

## Quick Start

### Prerequisites

- Docker and Docker Compose
- API keys for **Groq** ([console.groq.com](https://console.groq.com)) and/or **LLM7** and/or the LLM API provider of your choice

### 1. Clone

```bash
git clone https://github.com/paluigi/congiuntura-live.git
cd congiuntura-live
```

### 2. Set your API keys

The app reads secrets from **environment variables**. For local Docker Compose
runs, put them in a `.env` file in the project root — Compose reads it
automatically and substitutes the values into the container:

```env
LLM7_API_KEY=your-llm7-key-here
GROQ_API_KEY=your-groq-key-here
```

`MONGODB_URL` and `MONGODB_DATABASE` already default to the compose `mongo`
service, so you only need to set the LLM keys to enable processing. See
[Configuration](#configuration) for the full variable list.

### 3. Run with Docker Compose

Docker Compose pulls the prebuilt multi-arch image
(`paluigi/congiuntura-live:0.4.0`, `linux/amd64` + `linux/arm64`) from Docker Hub —
no local build required:

```bash
docker compose up -d
```

- **Web interface:** http://localhost:8000
- **MongoDB:** reachable as `mongo:27017` inside the compose network (not published to the host; data persisted to named volume)

### 4. Verify

```bash
curl http://localhost:8000/health
# → {"status":"ok"}
```

---

## Configuration

### File separation principle

| File | Purpose | Committed? | Secrets? |
|------|---------|------------|----------|
| `config/app.toml` | App settings (polling, processing, server) | ✅ | ❌ |
| `config/feeds.toml` | RSS feed URLs per agency | ✅ | ❌ |
| `config/extraction_model.py` | Pydantic model for LLM extraction | ✅ | ❌ |
| `config/llm.toml` | outlines-cascade providers + cascades | ✅ | ❌ |
| `.env` (local only) | Secrets for Docker Compose `${VAR}` substitution | ❌ | ✅ |

**Secrets are read from environment variables** — the app reads `os.environ`
directly, never a `.env` file. For local Docker Compose runs, create a `.env`
(gitignored) and Compose substitutes its values into the container's
`environment:` block. **API keys are NEVER in TOML files** — `config/llm.toml`
stores only the variable name (`api_key_env`); the actual key lives in the
environment.

### `config/app.toml`

```toml
[polling]
interval_minutes = 5

[server]
host = "0.0.0.0"
port = 8000

[logging]
level = "INFO"

[processing]
enabled = true
llm_config = "config/llm.toml"
cascade_name = "congiuntura"
interval_minutes = 2
max_content_chars = 4000
model_path = "config/extraction_model.py"
```

### `config/feeds.toml`

Add feeds per agency — the file is re-read on every poll cycle:

```toml
[istat]
name = "Istat"
language = "it"

[[istat.feeds]]
label = "National accounts"
url = "https://www.istat.it/tema/conti-nazionali/feed"
```

### `config/extraction_model.py`

Define what the LLM extracts. The web UI auto-generates filters from this model:

```python
class LLMExtraction(BaseModel):
    topic: Literal["Consumer prices", "Producer prices", ...] = Field(...)
    country: Literal["Euro area", "Italy", ...] = Field(...)
    sentiment: Literal["positive", "negative", "neutral"] = Field(...)
    summary_en: str = Field(description="Concise English summary")
    key_figures: str = Field(description="Key numerical figures")
```

### `config/llm.toml`

```toml
[providers.llm7]
type = "openai"
base_url = "https://api.llm7.io/v1"
api_key_env = "LLM7_API_KEY"   # ← variable name only; key lives in the environment

[providers.groq]
type = "openai"
base_url = "https://api.groq.com/openai/v1"
api_key_env = "GROQ_API_KEY"

# Tried in order: if the first fails, the next is attempted.
[cascades.congiuntura]
entries = [
    { provider = "groq", model = "openai/gpt-oss-120b" },
    { provider = "llm7", model = "gpt-5.4-mini" },
    # …more fallback entries — see config/llm.toml
]
```

### Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `MONGODB_URL` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGODB_DATABASE` | Database name | `congiuntura` |
| `LLM7_API_KEY` | LLM7 API key (outlines-cascade) | — |
| `GROQ_API_KEY` | Groq API key (outlines-cascade) | — |

For local Docker Compose, put them in a gitignored `.env`:

```env
LLM7_API_KEY=your-llm7-key-here
GROQ_API_KEY=your-groq-key-here
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        congiuntura-live app                          │
│                                                                     │
│  RSS Poll (5 min)              Processing Poll (2 min)              │
│       │                              │                              │
│       ▼                              ▼                              │
│  ┌──────────┐   ┌──────┐   ┌──────────────┐   ┌───────────────┐    │
│  │FeedReader│──▶│Dedup │   │   Scraper    │──▶│   Processor   │    │
│  │(RSS/Atom)│   │(hash)│   │(trafilatura) │   │(outlines-     │    │
│  └──────────┘   └──┬───┘   └──────────────┘   │ cascade)      │    │
│                     │                          └───────┬───────┘    │
│                     ▼                                  ▼            │
│              ┌──────────────┐                  ┌──────────────┐     │
│              │press_releases│                  │processed_    │     │
│              │  (raw)       │                  │releases      │     │
│              └──────────────┘                  └──────────────┘     │
│                     │                                  │            │
│                     ▼                                  ▼            │
│              /raw page                           / (main page)     │
│              (secondary)                         (auto-generated    │
│                                                 filters + cards)   │
└─────────────────────────────────────────────────────────────────────┘
```

### Anti-hallucination design

The LLM generates **only** the fields it can reason about. Link, date, and publisher
metadata are copied verbatim from the raw feed **after** generation.

```
LLMExtraction (what the LLM sees)      ProcessedRelease (stored in MongoDB)
┌──────────────────────────┐           ┌──────────────────────────────────┐
│ topic: Literal[...]      │           │ url_hash, url, title  ← from raw │
│ country: Literal[...]    │    +      │ publisher, published  ← from raw │
│ sentiment: Literal[...]  │           │ processing_model      ← from LLM │
│ summary_en: str          │           │ processed_at          ← timestamp│
│ key_figures: str         │           │ topic, country, ...   ← from LLM │
└──────────────────────────┘           └──────────────────────────────────┘
```

### Auto-generated filter UI

The main page introspects the `LLMExtraction` model fields to build filters:

| Pydantic type | Filter control |
|---------------|----------------|
| `Literal[...]` | `<select>` dropdown |
| `str` | Text search |
| `datetime` | Date range |

Change the model → restart → filters update automatically.

---

## Web Interface

- **`/`** (main) — Processed releases with auto-generated filters and enriched cards
- **`/raw`** (secondary) — Raw feeds with publisher + date range search

The ♻ **Reprocess** button on the main page processes all raw items not yet present
in the processed collection (incremental, never re-processes existing items).

---

## Processing Pipeline

1. **RSS poll** (every 5 min) — fetches feeds, deduplicates, stores in `press_releases`
2. **Scrape** — trafilatura extracts main text from the press release page (fallback: feed summary)
3. **LLM extraction** — outlines-cascade generates structured JSON matching `LLMExtraction`
4. **Assembly** — raw fields (url, date, publisher) are copied to the processed document
5. **Storage** — result stored in `processed_releases` with `processing_model` metadata

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12+ |
| Web framework | FastAPI |
| Frontend | HTMX + Pico CSS |
| RSS parsing | feedparser |
| Content scraping | trafilatura |
| LLM extraction | outlines-cascade (Pydantic structured generation) |
| LLM backend | Groq + LLM7 (cloud, OpenAI-compatible) |
| Database | MongoDB (motor async driver) |
| Scheduler | APScheduler |
| Models | Pydantic v2 |
| Dependency management | uv |

---

## Development

### Setup

```bash
uv sync --all-extras
# Export secrets into your shell (or a local .env) — the app reads os.environ
export MONGODB_URL="mongodb://localhost:27017"
export LLM7_API_KEY="your-llm7-key-here"
export GROQ_API_KEY="your-groq-key-here"
```

### Run locally

```bash
uv run uvicorn congiuntura_live.app:app --reload
```

### Run tests

```bash
uv run pytest
```

### Lint and format

```bash
uv run ruff check src/ tests/
uv run black --check src/ tests/
```

---

## Project Structure

```
congiuntura-live/
├── config/
│   ├── app.toml                 # Application settings (polling, processing)
│   ├── feeds.toml               # RSS feed URLs (user-editable)
│   ├── extraction_model.py      # LLMExtraction Pydantic model (user-editable)
│   └── llm.toml                 # outlines-cascade providers + cascades
├── src/congiuntura_live/
│   ├── __init__.py
│   ├── settings.py              # Config loading (TOML + env vars + model loader)
│   ├── models.py                # PressRelease model
│   ├── feed_reader.py           # Async RSS/Atom reader
│   ├── scraper.py               # trafilatura content extraction
│   ├── processor.py             # outlines-cascade pipeline orchestrator
│   ├── repository.py            # Async MongoDB repository (raw + processed)
│   ├── scheduler.py             # FeedPoller + ProcessingPoller (APScheduler)
│   └── app.py                   # FastAPI app + HTMX routes
├── templates/
│   ├── base.html                # Layout with nav (Processed / Raw feeds)
│   ├── index.html               # Main: processed cards + auto-generated filters
│   └── raw.html                 # Secondary: raw feed search
├── tests/
│   ├── conftest.py              # Test fixtures (RSS/Atom samples)
│   ├── test_feed_reader.py      # Feed parsing tests
│   ├── test_dedup.py            # Deduplication tests
│   ├── test_config.py           # Config loading tests
│   ├── test_extraction_model.py # Model loading + introspection tests
│   └── test_scraper.py          # Scraper fallback + live extraction tests
├── .dockerignore                # Excludes .env/.git/.venv from the build context
├── Dockerfile                   # Multi-stage build
├── docker-compose.yml           # Pulls paluigi/congiuntura-live + MongoDB
├── pyproject.toml
├── PLAN.md
└── README.md
```

---

## License

MIT © Luigi Palumbo

---

## Change Log

- **0.6.3**: Fixed duplicate Country filter when a stale extraction-model
  config (still defining the country field) is mounted over the image —
  model-derived country fields are now always ignored in filter generation.
- **0.6.2**: Country is now fully deterministic — derived from the issuing NSO
  (Istat→Italy, INE→Spain, INSEE→France, Destatis→Germany, CSO→Ireland,
  Eurostat→Euro area) instead of LLM-extracted; the "Other" option is gone and
  the country field was removed from the LLM output schema (fewer tokens, no
  misassignments possible). The home-page country filter is generated from the
  mapping.
- **0.6.1**: New LLM cascade — Meta `muse-spark-1.2-contributor` leads, previous
  groq/llm7 fallbacks retained behind it. Pressing Enter in the home/raw search
  forms now runs the htmx search instead of reloading the page and clearing
  all filters.
- **0.6.0**: Topics are now tags, not mutually exclusive categories — the LLM
  extraction selects all applicable topics (typically 1–3) per release, stored
  as the `topics` array. Home-page topic filter keeps OR semantics (a release
  matches if it has any of the selected tags); cards show one chip per topic.
  Breaking data-model change: reprocess existing releases after upgrading
  (clear `processed_releases` and re-run extraction).
- **0.5.2**: Free-text search on the home page — a search box in the filter bar
  matches case-insensitive text fragments against the English title, English
  summary and key figures of processed releases, combined with the existing
  publisher/topic/country/sentiment/date filters. Calendar UI: right-aligned
  ForexFactory values with full Actual/Forecast/Previous labels, responsive
  wrapping on narrow screens, dropped the impact badge and the machine-readable
  CSO links; raw-feed rows stack as cards on mobile.
- **0.5.1**: Live "last update" indicators on all three pages (home, raw feeds,
  calendar) — `update_status` helper MongoDB collection tracks each pipeline's
  last run (feed poller ~5 min, calendar cron daily 07:00 UTC); `/ws` WebSocket
  endpoint broadcasts status changes to connected clients instantly, with a
  vanilla-JS auto-reconnecting client in the base template. Fixed WebSocket
  handshake 403 by handling `WebSocketDisconnect` explicitly.
- **0.5.0**: English title translations (`title_en`) on processed press releases —
  extracted alongside the other LLM fields for new items, backfilled at startup for
  existing ones (title-only calls, no re-scraping); card UI shows the original title
  with the translation below. New `/calendar` page integrating the nso-calendar
  project: 7 collectors (Eurostat, Istat, INE, Destatis, INSEE, CSO, ForexFactory
  EUR-only), MongoDB collections `nso_releases`/`ff_releases`, daily APScheduler cron
  at 07:00 UTC (was weekly in the standalone project), first-run backfill, optional
  `FF_PROXY_URL`. Migrated from Motor to native PyMongo async (`AsyncMongoClient`).
- **0.4.0**: Added CSO (Ireland) as 6th monitored agency; added `Ireland` to the
  extraction-model `country` choices; added `.badge.cso` (teal `#008080`); fixed footer,
  README clone URL, and user-agent strings (`paluigi-moltis` → `paluigi`).
- **0.3.0**: Secrets now read from environment variables (was `.env` via Pydantic
  `env_file`); Docker Compose injects them via an explicit `environment:` block. Removed
  `.env.example`. Frontend migrated from Datastar/SSE to HTMX. LLM backends switched from
  OpenRouter to Groq + LLM7 (outlines-cascade cascade `congiuntura`). Multi-arch Docker
  image (`paluigi/congiuntura-live`, `linux/amd64` + `linux/arm64`) published to Docker Hub;
  Compose now pulls the image instead of building. Added `.dockerignore` to keep secrets
  and dev artifacts out of the build context.
- **0.2.0** (2025-07-21): LLM processing via outlines-cascade. Structured extraction
  (topic, country, sentiment, EN summary, key figures). Anti-hallucination two-model
  architecture. trafilatura scraping. Auto-generated filter UI. OpenRouter backend.
- **0.1.0** (2025-07-21): Initial release. RSS aggregation from 5 agencies, dedup,
  MongoDB storage, Datastar search UI, Docker Compose deployment.
