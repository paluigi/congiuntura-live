"""Read-only MCP (Model Context Protocol) server for agentic websites.

Exposes Congiuntura Live data over the MCP Streamable HTTP transport
(JSON-RPC 2.0 over HTTP POST at ``/mcp``) so that browser-run agent
networks (e.g. the agentic sibling site congiuntura-agent.by.gg8.eu)
can ground their answers in live MongoDB data.

Design rules:
- READ-ONLY: the tool registry exposes only read operations, backed by a
  dedicated repository that can use a read-only Mongo user via
  ``AGENT_MONGODB_URL`` (falls back to ``MONGODB_URL`` when unset).
- Server-side caps on limits and date ranges regardless of caller args.
- Tool arguments are validated before any database call; failures return
  MCP error results (``isError: true``), never raise.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pymongo import AsyncMongoClient
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase

from .settings import Settings

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "congiuntura-live-mcp", "version": "0.8.0"}

# Server-side caps — callers may ask for less, never more.
MAX_RELEASES_LIMIT = 30
MAX_LATEST_LIMIT = 20
MAX_DAYS_AHEAD = 14

ALLOWED_ORIGINS = [
    "https://congiuntura-agent.by.gg8.eu",
]

# Localhost dev origins (any port)
LOCAL_ORIGIN_PREFIXES = ("http://localhost", "http://127.0.0.1")

PROCESSOR_FIELDS_PROJECTION = {
    "_id": 0,
    "url": 1,
    "title": 1,
    "title_en": 1,
    "summary_en": 1,
    "topics": 1,
    "country": 1,
    "sentiment": 1,
    "key_figures": 1,
    "publisher": 1,
    "publisher_full": 1,
    "published": 1,
    "tags": 1,
}


def _iso(value: Any) -> Any:
    """JSON-safe serialization of MongoDB values."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC).isoformat()
    return value


def _clean_doc(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: _iso(v) for k, v in doc.items() if v is not None}


# ─── Read-only repository ────────────────────────────────────


class AgentReadOnlyRepository:
    """Read-only MongoDB access for MCP tools.

    Uses ``agent_mongodb_url`` when configured (a read-only Mongo user in
    production) so that a bug here can never write to production data.
    """

    def __init__(self, mongo_url: str, database_name: str) -> None:
        self._client = AsyncMongoClient(mongo_url)
        self._db: AsyncDatabase = self._client[database_name]
        self._processed: AsyncCollection = self._db["processed_releases"]
        self._nso: AsyncCollection = self._db["nso_releases"]
        self._ff: AsyncCollection = self._db["ff_releases"]
        self._status: AsyncCollection = self._db["update_status"]

    async def search_processed(
        self,
        query: dict[str, Any],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        cursor = (
            self._processed.find(query, PROCESSOR_FIELDS_PROJECTION)
            .sort("published", -1)
            .limit(limit)
        )
        return [_clean_doc(d) async for d in cursor]

    async def count_processed(self, query: dict[str, Any]) -> int:
        return await self._processed.count_documents(query)

    async def sentiment_breakdown(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        """Aggregate counts grouped by sentiment/topic/country/publisher.

        ``topics`` is an array of non-exclusive tags — the pipeline unwinds
        it so each topic dimension value accumulates the full release count.
        """
        pipeline: list[dict[str, Any]] = [{"$match": query}, {"$unwind": "$topics"}]
        pipeline.append(
            {
                "$group": {
                    "_id": {
                        "sentiment": "$sentiment",
                        "topic": "$topics",
                        "country": "$country",
                        "publisher": "$publisher",
                    },
                    "count": {"$sum": 1},
                }
            }
        )
        return await (await self._processed.aggregate(pipeline)).to_list(length=None)

    async def upcoming_nso(
        self, now: datetime, horizon: datetime, limit: int
    ) -> list[dict[str, Any]]:
        query = {"release_dt": {"$gte": now, "$lte": horizon}}
        cursor = (
            self._nso.find(
                query,
                {
                    "_id": 0,
                    "source": 1,
                    "title": 1,
                    "release_dt": 1,
                    "reference_period": 1,
                    "url": 1,
                },
            )
            .sort("release_dt", 1)
            .limit(limit)
        )
        return [_clean_doc(d) async for d in cursor]

    async def upcoming_ff(
        self, now: datetime, horizon: datetime, limit: int
    ) -> list[dict[str, Any]]:
        query = {"release_dt": {"$gte": now, "$lte": horizon}}
        cursor = (
            self._ff.find(
                query,
                {
                    "_id": 0,
                    "title": 1,
                    "release_dt": 1,
                    "impact": 1,
                    "currency": 1,
                    "actual": 1,
                    "forecast": 1,
                    "previous": 1,
                },
            )
            .sort("release_dt", 1)
            .limit(limit)
        )
        return [_clean_doc(d) async for d in cursor]

    async def pipeline_status(self) -> list[dict[str, Any]]:
        cursor = self._status.find({}, {"_id": 0})
        return [_clean_doc(d) async for d in cursor]

    async def close(self) -> None:
        await self._client.close()


# ─── Query helpers ───────────────────────────────────────────


def _published_range(date_from: str | None, date_to: str | None) -> dict[str, Any]:
    """Build a ``published`` filter over ISO-string dates.

    ``published`` is stored as an ISO 8601 string (model_dump(mode="json")),
    so string bounds compare correctly; ``date_to`` is made exclusive by
    advancing to the next day (avoids time-of-day suffix edge cases).
    """
    bounds: dict[str, Any] = {}
    if date_from:
        bounds["$gte"] = f"{date_from}T00:00:00+00:00"
    if date_to:
        to_dt = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        bounds["$lt"] = to_dt.strftime("%Y-%m-%d")
    return bounds


def _validate_date(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a 'YYYY-MM-DD' string")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid 'YYYY-MM-DD' date, got {value!r}") from exc
    return value


def _validate_enum(value: Any, name: str, allowed: tuple[str, ...] | list[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{name} must be one of: {', '.join(allowed)}")
    return value


def _validate_opt_enum(value: Any, name: str, allowed: tuple[str, ...] | list[str]) -> str | None:
    if value is None:
        return None
    return _validate_enum(value, name, allowed)


TOPICS = (
    "Consumer prices",
    "Producer prices",
    "Import prices",
    "Export prices",
    "GDP",
    "Industrial production",
    "Services production",
    "International trade",
    "Labour market",
    "Public finance",
    "Retail trade",
    "Construction",
    "Business surveys",
    "Other economic topics",
    "Non-economic topics",
)
COUNTRIES = ("Euro area", "Italy", "Spain", "France", "Germany", "Ireland", "Other")
SENTIMENTS = ("positive", "negative", "neutral")
PUBLISHERS = ("eurostat", "istat", "ine", "insee", "destatis", "cso", "dgecofin")
CAL_SOURCES = ("eurostat", "istat", "ine", "destatis", "insee", "cso", "forexfactory")


# ─── Tool implementations (pure logic, repo injected) ────────


class McpToolRegistry:
    """Registry of read-only MCP tools backed by AgentReadOnlyRepository.

    Separated from the transport so the tool layer is testable without Mongo.
    """

    def __init__(self, repo: Any) -> None:
        self._repo = repo
        self._tools: dict[str, dict[str, Any]] = {}
        self._handlers: dict[str, Callable[..., Awaitable[Any]]] = {}
        for name, schema, handler in (
            ("search_releases", SEARCH_RELEASES_SCHEMA, self._search_releases),
            ("get_latest_releases", LATEST_RELEASES_SCHEMA, self._latest_releases),
            ("sentiment_summary", SENTIMENT_SUMMARY_SCHEMA, self._sentiment_summary),
            ("get_upcoming_releases", UPCOMING_SCHEMA, self._upcoming),
            ("get_ff_events", FF_EVENTS_SCHEMA, self._ff_events),
            ("get_pipeline_status", PIPELINE_STATUS_SCHEMA, self._pipeline_status),
        ):
            self._tools[name] = schema
            self._handlers[name] = handler

    # -- introspection ------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {"name": n, "description": t["description"], "inputSchema": t["inputSchema"]}
            for n, t in self._tools.items()
        ]

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    # -- dispatch -----------------------------------------------------

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool, returning an MCP tool-result envelope.

        Never raises: validation and handler errors become isError results.
        """
        handler = self._handlers.get(name)
        if handler is None:
            return _error_result(f"Unknown tool: {name}")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _error_result("Tool arguments must be an object")
        try:
            data = await handler(**arguments)
        except TypeError as exc:
            return _error_result(f"Invalid arguments for {name}: {exc}")
        except ValueError as exc:
            return _error_result(str(exc))
        except Exception:
            logger.exception("MCP tool '%s' failed", name)
            return _error_result(f"Tool {name} failed; see server logs")
        return {
            "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
            "structuredContent": data,
            "isError": False,
        }

    # -- tools ----------------------------------------------------------

    async def _search_releases(
        self,
        topic: str | None = None,
        country: str | None = None,
        sentiment: str | None = None,
        publisher: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search processed press releases by topic/country/sentiment/publisher/date."""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, MAX_RELEASES_LIMIT)
        topic = _validate_opt_enum(topic, "topic", TOPICS)
        country = _validate_opt_enum(country, "country", COUNTRIES)
        sentiment = _validate_opt_enum(sentiment, "sentiment", SENTIMENTS)
        publisher = _validate_opt_enum(publisher, "publisher", PUBLISHERS)
        if date_from is not None:
            date_from = _validate_date(date_from, "date_from")
        if date_to is not None:
            date_to = _validate_date(date_to, "date_to")

        query: dict[str, Any] = {}
        for field, value in (
            ("topics", topic),
            ("country", country),
            ("sentiment", sentiment),
            ("publisher", publisher),
        ):
            if value:
                # topics is an array field (non-exclusive tags) → $in OR-match
                query[field] = {"$in": [value]} if field == "topics" else value
        published = _published_range(date_from, date_to)
        if published:
            query["published"] = published

        results = await self._repo.search_processed(query, limit=limit)
        total = await self._repo.count_processed(query)
        return {
            "query": query,
            "total_matching": total,
            "returned": len(results),
            "results": results,
        }

    async def _latest_releases(self, limit: int = 10) -> dict[str, Any]:
        """Most recent processed press releases across all agencies."""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, MAX_LATEST_LIMIT)
        results = await self._repo.search_processed({}, limit=limit)
        return {"returned": len(results), "results": results}

    async def _sentiment_summary(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        publisher: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate sentiment/topic/country/publisher counts for a period."""
        publisher = _validate_opt_enum(publisher, "publisher", PUBLISHERS)
        if date_from is not None:
            date_from = _validate_date(date_from, "date_from")
        if date_to is not None:
            date_to = _validate_date(date_to, "date_to")

        query: dict[str, Any] = {}
        if publisher:
            query["publisher"] = publisher
        published = _published_range(date_from, date_to)
        if published:
            query["published"] = published

        rows = await self._repo.sentiment_breakdown(query)
        summary: dict[str, dict[str, int]] = {
            "by_sentiment": {},
            "by_topic": {},
            "by_country": {},
            "by_publisher": {},
        }
        # Distinct-release total: after the topics $unwind a multi-topic
        # release appears once per topic, so count releases (not rows) by
        # deduplicating on (sentiment, country, publisher) — a release has
        # exactly one value of each of those.
        seen: set[tuple] = set()
        total = 0
        for row in rows:
            count = int(row.get("count", 0))
            key = row.get("_id", {})
            release_key = (key.get("sentiment"), key.get("country"), key.get("publisher"))
            if release_key not in seen:
                seen.add(release_key)
                total += 1
            for dim, field in (
                ("sentiment", "by_sentiment"),
                ("topic", "by_topic"),
                ("country", "by_country"),
                ("publisher", "by_publisher"),
            ):
                value = key.get(dim)
                if value:
                    summary[field][str(value)] = summary[field].get(str(value), 0) + count
        return {
            "query": query,
            "total_releases": total,
            "note": (
                "Counts are press-release counts (a release covering several topics is "
                "counted once per topic in by_topic); sentiment reflects the tone the "
                "agency conveyed, not a market judgment."
            ),
            **summary,
        }

    async def _upcoming(
        self,
        days_ahead: int = 7,
        source: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """NSO statistical releases scheduled in the next days_ahead days."""
        if not isinstance(days_ahead, int) or isinstance(days_ahead, bool) or days_ahead < 1:
            raise ValueError("days_ahead must be a positive integer")
        days_ahead = min(days_ahead, MAX_DAYS_AHEAD)
        source = _validate_opt_enum(source, "source", CAL_SOURCES)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, MAX_RELEASES_LIMIT)

        if source == "forexfactory":
            return {"window_days": days_ahead, "results": []}
        now = datetime.now(UTC).replace(tzinfo=None)
        horizon = now + timedelta(days=days_ahead)
        results = await self._repo.upcoming_nso(now, horizon, limit)
        if source:
            results = [r for r in results if r.get("source") == source]
        return {"window_days": days_ahead, "returned": len(results), "results": results}

    async def _ff_events(
        self,
        days_ahead: int = 7,
        limit: int = 20,
    ) -> dict[str, Any]:
        """ForexFactory euro-area economic events in the next days_ahead days."""
        if not isinstance(days_ahead, int) or isinstance(days_ahead, bool) or days_ahead < 1:
            raise ValueError("days_ahead must be a positive integer")
        days_ahead = min(days_ahead, MAX_DAYS_AHEAD)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, MAX_RELEASES_LIMIT)

        now = datetime.now(UTC).replace(tzinfo=None)
        horizon = now + timedelta(days=days_ahead)
        results = await self._repo.upcoming_ff(now, horizon, limit)
        return {"window_days": days_ahead, "returned": len(results), "results": results}

    async def _pipeline_status(self) -> dict[str, Any]:
        """Last run timestamp/status of the data pipelines (freshness info)."""
        rows = await self._repo.pipeline_status()
        return {"pipelines": rows}


# ─── Tool schemas ────────────────────────────────────────────

_ENUM_TOPIC = {"type": "string", "enum": list(TOPICS)}
_ENUM_COUNTRY = {"type": "string", "enum": list(COUNTRIES)}
_ENUM_SENTIMENT = {"type": "string", "enum": list(SENTIMENTS)}
_ENUM_PUBLISHER = {"type": "string", "enum": list(PUBLISHERS)}
_DATE = {"type": "string", "description": "Date in YYYY-MM-DD format"}

SEARCH_RELEASES_SCHEMA = {
    "description": (
        "Search processed press releases from European statistical agencies "
        "(Eurostat, Istat, INE, INSEE, Destatis, CSO, DG EcoFin) by topic, country, "
        "sentiment, publisher and/or date range. Returns titles, English summaries, "
        "key figures, sentiment and source URLs. Topics are non-exclusive tags: a "
        "release can cover several (e.g. a CPI release may be tagged both 'Consumer "
        "prices' and 'International trade'). Prefer narrow filters and cite the url "
        "of each release you use."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "topic": {
                **_ENUM_TOPIC,
                "description": "Topic tag the release must include (topics are non-exclusive tags)",
            },
            "country": {**_ENUM_COUNTRY, "description": "Geographic scope of the data"},
            "sentiment": {**_ENUM_SENTIMENT, "description": "Tone conveyed by the data"},
            "publisher": {**_ENUM_PUBLISHER, "description": "Publishing agency slug"},
            "date_from": _DATE,
            "date_to": _DATE,
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_RELEASES_LIMIT,
                "description": f"Max releases returned (default 10, max {MAX_RELEASES_LIMIT})",
            },
        },
    },
}

LATEST_RELEASES_SCHEMA = {
    "description": (
        "Most recent processed press releases across all monitored agencies. "
        "Use for 'what's new / latest signal' questions."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_LATEST_LIMIT,
                "description": f"Max releases returned (default 10, max {MAX_LATEST_LIMIT})",
            },
        },
    },
}

SENTIMENT_SUMMARY_SCHEMA = {
    "description": (
        "Aggregate counts of processed press releases by sentiment, topic, country "
        "and publisher over an optional date range. Use for 'data mood' or "
        "cross-agency comparison questions. Counts are release counts, not "
        "forecasts."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "date_from": _DATE,
            "date_to": _DATE,
            "publisher": {**_ENUM_PUBLISHER, "description": "Restrict to one agency"},
        },
    },
}

UPCOMING_SCHEMA = {
    "description": (
        "Official statistical releases (NSO calendars: Eurostat, Istat, INE, Destatis, "
        "INSEE, CSO) scheduled within the next days. Use for 'which releases are due' "
        "questions. ForexFactory events are served by get_ff_events."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "days_ahead": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_DAYS_AHEAD,
                "description": f"Look-ahead window in days (default 7, max {MAX_DAYS_AHEAD})",
            },
            "source": {
                "type": "string",
                "enum": [s for s in CAL_SOURCES if s != "forexfactory"],
                "description": "Restrict to one agency",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RELEASES_LIMIT},
        },
    },
}

FF_EVENTS_SCHEMA = {
    "description": (
        "ForexFactory euro-area (EUR) economic events within the next days, with "
        "impact rating and actual/forecast/previous values when published. Use for "
        "'what were the previous/forecast values' questions."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "days_ahead": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_DAYS_AHEAD,
                "description": f"Look-ahead window in days (default 7, max {MAX_DAYS_AHEAD})",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RELEASES_LIMIT},
        },
    },
}

PIPELINE_STATUS_SCHEMA = {
    "description": (
        "Freshness of the site's data: when the press-release and calendar pipelines "
        "last ran and whether they succeeded. Call this when data currency matters "
        "for the answer."
    ),
    "inputSchema": {"type": "object", "properties": {}},
}


def _error_result(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


# ─── JSON-RPC / MCP transport ────────────────────────────────


class McpRpcHandler:
    """JSON-RPC 2.0 method dispatch for the MCP Streamable HTTP transport."""

    def __init__(self, registry: McpToolRegistry) -> None:
        self._registry = registry

    async def handle(self, message: Any) -> tuple[Any | None, int]:
        """Handle one JSON-RPC message (dict or batch list).

        Returns (response_body_or_None, http_status). Notifications (no id)
        return (None, 202) per the Streamable HTTP transport rules.
        """
        if isinstance(message, list):
            responses = []
            for item in message:
                body, status = await self._handle_single(item)
                if body is not None:
                    responses.append(body)
            if not responses:
                return None, 202
            return responses, 200
        return await self._handle_single(message)

    async def _handle_single(self, message: Any) -> tuple[Any | None, int]:
        if not isinstance(message, dict):
            return self._rpc_error(None, -32600, "Invalid Request: expected a JSON-RPC object"), 400
        rpc_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            return self._rpc_error(rpc_id, -32600, "Invalid Request: missing method"), 400

        # Notifications: no response required
        if "id" not in message:
            if method == "notifications/initialized":
                return None, 202
            return None, 202

        params = message.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        try:
            if method == "initialize":
                result = {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self._registry.list_tools()}
            elif method == "tools/call":
                name = params.get("name")
                if not isinstance(name, str):
                    return self._rpc_error(rpc_id, -32602, "params.name must be a string"), 200
                result = await self._registry.call_tool(name, params.get("arguments") or {})
            elif method in ("resources/list", "prompts/list"):
                result = {}
            else:
                return self._rpc_error(rpc_id, -32601, f"Method not found: {method}"), 200
        except Exception:
            logger.exception("MCP RPC error on %s", method)
            return self._rpc_error(rpc_id, -32603, "Internal error"), 200

        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}, 200

    @staticmethod
    def _rpc_error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": code, "message": message},
        }


def register_mcp(app: FastAPI, cors_extra_origins: list[str] | None = None) -> None:
    """Install CORS + the /mcp JSON-RPC endpoint on the FastAPI app.

    The registry is read from ``app.state.mcp_registry`` at request time so
    it can be created during lifespan (when settings are available).
    """
    origins = list(ALLOWED_ORIGINS) + list(cors_extra_origins or [])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id"],
    )

    @app.post("/mcp")
    async def mcp_endpoint(request: Request) -> JSONResponse:
        registry = getattr(app.state, "mcp_registry", None)
        if registry is None:
            return JSONResponse({"error": "MCP not initialized"}, status_code=503)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                status_code=400,
            )
        handler = McpRpcHandler(registry)
        response, status = await handler.handle(body)
        if response is None:
            return JSONResponse(None, status_code=status)
        return JSONResponse(response, status_code=status)

    @app.get("/mcp")
    async def mcp_get(request: Request) -> JSONResponse:
        # Streamable HTTP: server does not offer an SSE stream here.
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32000, "message": "GET not supported: POST JSON-RPC to /mcp"},
            },
            status_code=405,
        )


async def create_mcp_repo(settings: Settings) -> AgentReadOnlyRepository:
    """Create the read-only repository used by MCP tools.

    Uses AGENT_MONGODB_URL (read-only user) when set, else MONGODB_URL.
    """
    url = settings.agent_mongodb_url or settings.mongodb_url
    if settings.agent_mongodb_url:
        logger.info("MCP: using dedicated AGENT_MONGODB_URL (read-only user)")
    else:
        logger.warning("MCP: AGENT_MONGODB_URL not set — falling back to MONGODB_URL")
    return AgentReadOnlyRepository(url, settings.mongodb_database)
