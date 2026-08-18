"""FastAPI application with htmx-powered search interface."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args, get_origin

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.responses import Response

from .feed_reader import FeedReader
from .processor import ReleaseProcessor
from .repository import PressReleaseRepository
from .scheduler import FeedPoller, ProcessingPoller
from .settings import Settings, load_app_config, load_extraction_model, load_feeds_config
from .update_status import KEY_CALENDAR, KEY_PRESSES, UpdateStatusRepository
from .ws_hub import update_hub
from .calendar import CalendarPoller, CalendarRepository
from .calendar.config import load_calendar_config

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"

# Global objects populated at startup.
_repo: PressReleaseRepository | None = None
_reader: FeedReader | None = None
_poller: FeedPoller | None = None
_processor: ReleaseProcessor | None = None
_proc_poller: ProcessingPoller | None = None
_extraction_model_class = None
_calendar_repo: CalendarRepository | None = None
_calendar_poller: CalendarPoller | None = None
_status_repo: UpdateStatusRepository | None = None

# Module-level processing reference counter, incremented by ProcessingPoller
# and the manual reprocess button.  Polled by the htmx /processing-status endpoint.
_processing_refs: int = 0


def _is_processing() -> bool:
    return _processing_refs > 0


def _processing_inc() -> None:
    global _processing_refs
    _processing_refs += 1


def _processing_dec() -> None:
    global _processing_refs
    _processing_refs = max(0, _processing_refs - 1)


def _format_status(doc: dict[str, Any] | None) -> dict[str, Any]:
    """Serialize an update_status document for templates/WS payloads."""
    if not doc:
        return {"last_run": None, "status": "never", "details": ""}
    last_run = doc.get("last_run")
    return {
        "last_run": last_run.strftime("%Y-%m-%d %H:%M UTC") if last_run else None,
        "status": doc.get("status", ""),
        "details": doc.get("details", ""),
    }


async def _initial_calendar_backfill() -> None:
    """On first run (empty calendar collections), populate the calendar:
    NSO collectors once + ForexFactory historical backfill. The daily
    scheduler keeps everything up to date afterwards."""
    if _calendar_repo is None or _calendar_poller is None:
        return
    try:
        nso_count = await _calendar_repo._nso.count_documents({})
        ff_count = await _calendar_repo._ff.count_documents({})
        if nso_count == 0 and ff_count == 0:
            logger.info("Calendar collections empty — running initial collection")
            await _calendar_poller.collect_once()
            ff_upserted = await _calendar_poller.backfill_ff()
            logger.info("Initial calendar backfill finished (FF: %d events)", ff_upserted)
        else:
            logger.info("Calendar already populated (nso=%d, ff=%d) — skipping backfill", nso_count, ff_count)
    except Exception:
        logger.exception("Initial calendar backfill failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    global _repo, _reader, _poller, _processor, _proc_poller, _extraction_model_class
    global _calendar_repo, _calendar_poller, _status_repo

    settings = Settings()
    app_cfg = load_app_config()

    logging.basicConfig(
        level=getattr(logging, app_cfg.logging.level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    _repo = PressReleaseRepository(settings.mongodb_url, settings.mongodb_database)
    await _repo.ensure_indexes()

    # Last-update tracker (helper collection) + WebSocket broadcast on change
    _status_repo = UpdateStatusRepository(settings.mongodb_url, settings.mongodb_database)

    async def _broadcast_update_status() -> None:
        """Render and push indicator fragments to subscribed clients."""
        from .ws_hub import render_indicator_fragment

        if _status_repo is None:
            return
        try:
            statuses = await _status_repo.get_all()
            for key in (KEY_PRESSES, KEY_CALENDAR):
                fragment = render_indicator_fragment(key, _format_status(statuses.get(key)))
                await update_hub.broadcast(key, fragment)
        except Exception:
            logger.exception("Failed to broadcast update status")

    _reader = FeedReader()
    _poller = FeedPoller(
        _reader,
        _repo,
        status_repo=_status_repo,
        on_update=lambda: asyncio.create_task(_broadcast_update_status()),
    )
    _poller.start(interval_minutes=app_cfg.polling.interval_minutes)

    # Release calendar (NSO + ForexFactory) — same MongoDB database.
    calendar_cfg = load_calendar_config()
    _calendar_repo = CalendarRepository(settings.mongodb_url, settings.mongodb_database)
    await _calendar_repo.ensure_indexes()
    _calendar_poller = CalendarPoller(
        _calendar_repo,
        calendar_cfg,
        status_repo=_status_repo,
        on_update=lambda: asyncio.create_task(_broadcast_update_status()),
    )
    _calendar_poller.start()
    # First-run backfill: populate the calendar if collections are empty
    asyncio.create_task(_initial_calendar_backfill())

    # Load extraction model for UI introspection
    _extraction_model_class = load_extraction_model(app_cfg.processing.model_path)

    # Start processing pipeline if enabled
    if app_cfg.processing.enabled:
        try:
            _processor = ReleaseProcessor(app_cfg.processing)
            _proc_poller = ProcessingPoller(
                _processor,
                _repo,
                interval_minutes=app_cfg.processing.interval_minutes,
                on_processing_change=lambda active: _processing_inc() if active else _processing_dec(),
            )
            _proc_poller.start()
            logger.info("LLM processing enabled — cascade '%s'", app_cfg.processing.cascade_name)

            # Startup backfill: translate titles of already-processed releases
            # that predate the title_en field. Lightweight (title-only LLM
            # calls, no re-scraping); runs in background, never blocks startup.
            from .title_backfill import run_title_backfill_on_startup

            asyncio.create_task(run_title_backfill_on_startup(app_cfg.processing, _repo))
        except Exception:
            logger.exception("Failed to start LLM processing — continuing in raw-only mode")
            _processor = None
            _proc_poller = None

    logger.info("Application started — polling every %d min", app_cfg.polling.interval_minutes)
    yield

    if _calendar_poller:
        _calendar_poller.stop()
    if _calendar_repo:
        await _calendar_repo.close()
    if _status_repo:
        await _status_repo.close()
    if _proc_poller:
        await _proc_poller.stop()
    if _poller:
        await _poller.stop()
    if _repo:
        await _repo.close()
    logger.info("Application stopped")


app = FastAPI(title="Congiuntura Live", version="0.6.1", lifespan=lifespan)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Serve vendored static assets (htmx.min.js etc.)
app.mount("/static", StaticFiles(directory=str(TEMPLATES_DIR / "static")), name="static")


def _get_repo() -> PressReleaseRepository:
    if _repo is None:
        raise RuntimeError("Repository not initialized")
    return _repo


# ── Template filters ────────────────────────────────────────


def _format_date(pub_date: Any) -> str:
    if not pub_date:
        return ""
    try:
        dt = datetime.fromisoformat(pub_date) if isinstance(pub_date, str) else pub_date
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(pub_date)[:16]


templates.env.filters["format_date"] = _format_date


# ── Calendar template filters ───────────────────────────────


_SOURCE_NAMES = {
    "eurostat": "Eurostat", "istat": "Istat", "ine": "INE",
    "destatis": "Destatis", "insee": "INSEE", "cso": "CSO",
    "forexfactory": "ForexFactory",
}


def _cal_date(dt: Any) -> str:
    if not dt:
        return ""
    try:
        d = datetime.fromisoformat(dt) if isinstance(dt, str) else dt
        return d.strftime("%a %b %d")
    except (ValueError, TypeError):
        return str(dt)[:10]


def _cal_time(dt: Any) -> str:
    if not dt:
        return ""
    try:
        d = datetime.fromisoformat(dt) if isinstance(dt, str) else dt
        return d.strftime("%H:%M UTC")
    except (ValueError, TypeError):
        return ""


def _cal_source_name(code: Any) -> str:
    return _SOURCE_NAMES.get(str(code), str(code))


templates.env.filters["cal_date"] = _cal_date
templates.env.filters["cal_time"] = _cal_time
templates.env.filters["cal_source_name"] = _cal_source_name


# ── Model introspection for auto-generated filters ──────────


def _literal_choices(annotation: Any) -> list[str] | None:
    """Return the Literal choices of an annotation.

    Accepts both ``Literal[...]`` (single-select semantics) and
    ``list[Literal[...]]`` (multi-tag semantics); anything else → None.
    """
    origin = get_origin(annotation)
    if origin is not None and hasattr(origin, "__name__") and origin.__name__ == "Literal":
        return list(get_args(annotation))
    if origin is list:
        args = get_args(annotation)
        if args:
            inner_origin = get_origin(args[0])
            if inner_origin is not None and inner_origin.__name__ == "Literal":
                return list(get_args(args[0]))
    return None


def _build_filter_definitions() -> list[dict[str, Any]]:
    """Introspect the LLMExtraction model to build UI filter definitions.

    Literal and list[Literal[...]] types become dropdown filters
    (multi-select in the UI either way).  ``str`` fields (summary_en,
    key_figures) are excluded — they are display-only.
    """
    if _extraction_model_class is None:
        return []
    filters: list[dict[str, Any]] = []
    for name, field_info in _extraction_model_class.model_fields.items():
        choices = _literal_choices(field_info.annotation)
        if choices:
            filters.append({"name": name, "type": "select", "choices": choices})
    return filters


# ── Full page routes ────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Render the main page with processed releases and auto-generated filters."""
    feeds_cfg = load_feeds_config()
    publishers = [(slug, cfg.name) for slug, cfg in feeds_cfg.items()]
    repo = _get_repo()
    processed_count = await repo.count_total_processed()
    raw_count = await repo.count_total_raw()
    filters = _build_filter_definitions()
    ctx = await _update_status_context()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "publishers": publishers,
            "processed_count": processed_count,
            "raw_count": raw_count,
            "filters": filters,
            "processing_enabled": _processor is not None,
            **ctx,
        },
    )


@app.get("/raw", response_class=HTMLResponse)
async def raw(request: Request):
    """Render the secondary page with raw (unprocessed) feeds."""
    feeds_cfg = load_feeds_config()
    publishers = [(slug, cfg.name) for slug, cfg in feeds_cfg.items()]
    repo = _get_repo()
    total = await repo.count_total_raw()
    ctx = await _update_status_context()
    return templates.TemplateResponse(
        request,
        "raw.html",
        {
            "request": request,
            "publishers": publishers,
            "total": total,
            **ctx,
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Date parsing helper ─────────────────────────────────────


def _parse_date(raw: str | None) -> str | None:
    """Parse a ``YYYY-MM-DD`` date picker value into an ISO 8601 string.

    The ``published`` field is stored as an ISO 8601 string in MongoDB,
    so date-range filters must also be strings for correct comparison.
    """
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC).isoformat()
    except ValueError:
        return None


# ── htmx fragment routes ────────────────────────────────────


@app.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    publisher: list[str] = Query(default=[]),
    topics: list[str] = Query(default=[]),
    country: list[str] = Query(default=[]),
    sentiment: list[str] = Query(default=[]),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    q: str = Query(default=""),
):
    """htmx endpoint: filter processed releases, return card fragment.

    The response includes an out-of-band swap that updates the
    ``#retrieved-count`` element with the number of filtered results.
    ``q`` is a text fragment matched (case-insensitive) in the English
    title, English summary and key figures, combined with the other filters.
    """
    repo = _get_repo()
    filters: dict[str, Any] = {}
    for key, vals in (("publisher", publisher), ("topics", topics), ("country", country), ("sentiment", sentiment)):
        if vals:
            filters[key] = vals
    filters["date_from"] = _parse_date(date_from)
    filters["date_to"] = _parse_date(date_to)
    if q.strip():
        filters["q"] = q.strip()

    results = await repo.search_processed(filters=filters, limit=200)
    total_matching = await repo.count_processed(filters=filters)
    return templates.TemplateResponse(
        request,
        "_processed_cards.html",
        {"request": request, "results": results, "retrieved_count": total_matching},
    )


@app.get("/search-raw", response_class=HTMLResponse)
async def search_raw(
    request: Request,
    publisher: str = Query(default="all"),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
):
    """htmx endpoint: filter raw releases, return table fragment."""
    repo = _get_repo()
    parsed_from = _parse_date(date_from)
    parsed_to = _parse_date(date_to)
    results = await repo.search_raw(
        publisher=publisher,
        date_from=parsed_from,
        date_to=parsed_to,
        limit=200,
    )
    total_matching = await repo.count_raw(
        publisher=publisher, date_from=parsed_from, date_to=parsed_to
    )
    return templates.TemplateResponse(
        request,
        "_raw_rows.html",
        {"request": request, "results": results, "retrieved_count": total_matching},
    )


# ── Reprocess + processing status ───────────────────────────


@app.post("/reprocess")
async def reprocess(request: Request):
    """Kick off manual reprocessing as a background task.

    Sets the processing flag immediately (so the spinner shows), then
    runs the full pending queue.  The flag is cleared when done.
    """
    if _proc_poller is None:
        return Response(
            content='<span id="reprocess-status" class="flash">Processing is not enabled.</span>',
            media_type="text/html",
            status_code=200,
        )

    if _is_processing():
        return Response(
            content='<span id="reprocess-status" class="flash">Already processing — please wait.</span>',
            media_type="text/html",
            status_code=200,
        )

    async def _run():
        _processing_inc()
        try:
            await _proc_poller.process_all_pending()
        except Exception:
            logger.exception("Background reprocess failed")
        finally:
            _processing_dec()

    background = BackgroundTask(_run)

    return templates.TemplateResponse(
        request,
        "_stats.html",
        {
            "request": request,
            "processing": True,
            "processed_count": await _get_repo().count_total_processed(),
            "raw_count": await _get_repo().count_total_raw(),
            "reprocess_msg": "Processing started…",
        },
        background=background,
    )


@app.get("/processing-status", response_class=HTMLResponse)
async def processing_status(request: Request):
    """Return the stats bar fragment (polled by htmx every 2s).

    Includes a spinner when processing is active.
    """
    repo = _get_repo()
    processed_count, raw_count = await asyncio.gather(
        repo.count_total_processed(), repo.count_total_raw()
    )
    return templates.TemplateResponse(
        request,
        "_stats.html",
        {
            "request": request,
            "processing": _is_processing(),
            "processed_count": processed_count,
            "raw_count": raw_count,
        },
    )


# ── Release calendar routes ─────────────────────────────────


def _get_calendar_repo() -> CalendarRepository:
    if _calendar_repo is None:
        raise RuntimeError("Calendar repository not initialized")
    return _calendar_repo


def _parse_calendar_date(raw: str | None) -> datetime | None:
    """Parse a YYYY-MM-DD date picker value into a naive UTC datetime.

    Mongo returns naive datetimes, so filters must be naive to match.
    """
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    total = (year * 12 + (month - 1)) + delta
    return total // 12, total % 12 + 1


@app.get("/calendar", response_class=HTMLResponse)
async def calendar_page(request: Request):
    """Release calendar page — NSO schedules + ForexFactory events."""
    repo = _get_calendar_repo()
    sources = await repo.list_calendar_sources()
    today = datetime.now(UTC).date()
    # Default range: current month
    import calendar as _cal
    default_from = today.replace(day=1).isoformat()
    default_to = _cal.monthrange(today.year, today.month)[1]
    default_to = today.replace(day=default_to).isoformat()
    ctx = await _update_status_context()
    return templates.TemplateResponse(
        request,
        "calendar.html",
        {
            "request": request,
            "sources": sources,
            "default_date_from": default_from,
            "default_date_to": default_to,
            **ctx,
        },
    )


@app.get("/calendar/search", response_class=HTMLResponse)
async def calendar_search(
    request: Request,
    source: str = Query(default="all"),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    q: str = Query(default=""),
):
    """htmx endpoint: filter calendar releases, return card fragment.

    Defaults: current month when no filters; text search active →
    date_from becomes the first day of the previous month (no date_to).
    """
    repo = _get_calendar_repo()
    today = datetime.now(UTC).date()

    parsed_from = _parse_calendar_date(date_from)
    parsed_to = _parse_calendar_date(date_to)

    if not date_from and not date_to and not q:
        import calendar as _cal
        last = _cal.monthrange(today.year, today.month)[1]
        parsed_from = datetime(today.year, today.month, 1)
        parsed_to = datetime(today.year, today.month, last, 23, 59, 59)
    elif not date_from and q:
        py, pm = _shift_month(today.year, today.month, -1)
        parsed_from = datetime(py, pm, 1)

    if parsed_to:
        parsed_to = parsed_to.replace(hour=23, minute=59, second=59)

    results = await repo.search_calendar(
        source=source,
        date_from=parsed_from,
        date_to=parsed_to,
        q=q or None,
        limit=500,
    )

    # Normalize release_dt for template comparison: Mongo returns naive UTC.
    now_naive = datetime.now(UTC).replace(tzinfo=None)

    return templates.TemplateResponse(
        request,
        "_calendar_cards.html",
        {
            "request": request,
            "results": results,
            "retrieved_count": len(results),
            "now": now_naive,
        },
    )


# ── WebSocket + update-status indicator ─────────────────────


def _get_status_repo() -> UpdateStatusRepository:
    if _status_repo is None:
        raise RuntimeError("Status repository not initialized")
    return _status_repo


async def _update_status_context() -> dict[str, Any]:
    """Context for the last-update indicators rendered on each page."""
    repo = _get_status_repo()
    statuses = await repo.get_all()
    return {
        "press_status": _format_status(statuses.get(KEY_PRESSES)),
        "calendar_status": _format_status(statuses.get(KEY_CALENDAR)),
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Push channel for live update-status indicators (htmx ws extension).

    Clients subscribe to one indicator via ?kind=press|calendar and receive
    HTML fragments that the extension swaps by element id.
    """
    from .ws_hub import VALID_KINDS

    kind_param = ws.query_params.get("kind", "press")
    kind = "calendar" if kind_param == "calendar" else "press_releases"
    assert kind in VALID_KINDS
    try:
        await update_hub.connect(ws, {kind})
        while True:
            # Anything received (htmx pings, client messages) is ignored
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WebSocket endpoint error")
    finally:
        await update_hub.disconnect(ws)
