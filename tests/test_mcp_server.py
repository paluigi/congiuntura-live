"""Tests for the read-only MCP server (tool layer + JSON-RPC transport)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from congiuntura_live.mcp_server import (
    MAX_DAYS_AHEAD,
    MAX_LATEST_LIMIT,
    MCP_PROTOCOL_VERSION,
    AgentReadOnlyRepository,
    McpRpcHandler,
    McpToolRegistry,
    register_mcp,
)

# ─── Fakes ───────────────────────────────────────────────────


class FakeRepo:
    """In-memory stand-in for AgentReadOnlyRepository."""

    def __init__(self) -> None:
        self.releases: list[dict] = []
        self.nso: list[dict] = []
        self.ff: list[dict] = []
        self.status: list[dict] = []
        self.write_attempted = False

    async def search_processed(self, query, limit=10):
        def _match(doc):
            for field, cond in query.items():
                if field == "published":
                    pub = doc.get("published", "")
                    if "$gte" in cond and pub < cond["$gte"]:
                        return False
                    if "$lt" in cond and pub >= cond["$lt"]:
                        return False
                elif isinstance(cond, dict) and "$in" in cond:
                    value = doc.get(field)
                    value_list = value if isinstance(value, list) else [value]
                    if not any(v in value_list for v in cond["$in"]):
                        return False
                elif doc.get(field) != cond:
                    return False
            return True

        found = [d for d in self.releases if _match(d)]
        found.sort(key=lambda d: d.get("published", ""), reverse=True)
        return found[:limit]

    async def count_processed(self, query):
        return len(await self.search_processed(query, limit=10**9))

    async def sentiment_breakdown(self, query):
        def _match(doc):
            for field, cond in query.items():
                if field == "published":
                    pub = doc.get("published", "")
                    if "$gte" in cond and pub < cond["$gte"]:
                        return False
                    if "$lt" in cond and pub >= cond["$lt"]:
                        return False
                elif isinstance(cond, dict) and "$in" in cond:
                    value = doc.get(field)
                    value_list = value if isinstance(value, list) else [value]
                    if not any(v in value_list for v in cond["$in"]):
                        return False
                elif doc.get(field) != cond:
                    return False
            return True

        rows: list[dict] = []
        for doc in self.releases:
            if not _match(doc):
                continue
            # Mirror the $unwind: one breakdown row per (release, topic)
            topics = doc.get("topics") or [doc.get("topic")]
            for t in topics:
                key = (doc.get("sentiment"), t, doc.get("country"), doc.get("publisher"))
                existing = next(
                    (
                        r
                        for r in rows
                        if r["_id"]
                        == {
                            "sentiment": key[0],
                            "topic": key[1],
                            "country": key[2],
                            "publisher": key[3],
                        }
                    ),
                    None,
                )
                if existing:
                    existing["count"] += 1
                else:
                    rows.append(
                        {
                            "_id": {
                                "sentiment": key[0],
                                "topic": key[1],
                                "country": key[2],
                                "publisher": key[3],
                            },
                            "count": 1,
                        }
                    )
        return rows

    async def upcoming_nso(self, now, horizon, limit):
        out = [d for d in self.nso if now <= d["release_dt"] <= horizon]
        out.sort(key=lambda d: d["release_dt"])
        return out[:limit]

    async def upcoming_ff(self, now, horizon, limit):
        out = [d for d in self.ff if now <= d["release_dt"] <= horizon]
        out.sort(key=lambda d: d["release_dt"])
        return out[:limit]

    async def pipeline_status(self):
        return self.status


@pytest.fixture
def registry():
    repo = FakeRepo()
    repo.releases = [
        {
            "url": "https://www.istat.it/example",
            "title": "Inflazione a novembre",
            "title_en": "November inflation",
            "summary_en": "HICP fell to 1.5%.",
            "topics": ["Consumer prices"],
            "country": "Italy",
            "sentiment": "negative",
            "key_figures": "HICP 1.5%; -0.2pp",
            "publisher": "istat",
            "publisher_full": "Istat",
            "published": "2026-11-20T10:00:00+00:00",
        },
        {
            "url": "https://www.destatis.de/example",
            "title": "BIP Wachstum",
            "title_en": "GDP growth",
            "summary_en": "GDP grew 0.3%.",
            "topics": ["GDP", "International trade"],
            "country": "Germany",
            "sentiment": "positive",
            "key_figures": "GDP +0.3%",
            "publisher": "destatis",
            "publisher_full": "Destatis",
            "published": "2026-11-14T08:00:00+00:00",
        },
    ]
    return McpToolRegistry(repo)


def _rpc_request(method: str, params: dict | None = None, rpc_id: int = 1) -> dict:
    msg = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


# ─── Tool registry tests ─────────────────────────────────────


class TestToolRegistry:
    async def test_lists_six_read_only_tools(self, registry):
        names = registry.tool_names
        assert names == [
            "get_ff_events",
            "get_latest_releases",
            "get_pipeline_status",
            "get_upcoming_releases",
            "search_releases",
            "sentiment_summary",
        ]
        for tool in registry.list_tools():
            assert tool["description"]
            assert tool["inputSchema"]["type"] == "object"

    async def test_search_filters_and_citation_fields(self, registry):
        result = await registry.call_tool("search_releases", {"topic": "Consumer prices"})
        assert result["isError"] is False
        data = result["structuredContent"]
        assert data["total_matching"] == 1
        release = data["results"][0]
        for field in (
            "url",
            "title",
            "title_en",
            "summary_en",
            "sentiment",
            "key_figures",
            "publisher",
        ):
            assert field in release

    async def test_search_invalid_enum_is_error(self, registry):
        result = await registry.call_tool("search_releases", {"topic": "Crypto"})
        assert result["isError"] is True
        assert "topic" in result["content"][0]["text"]

    async def test_search_bad_date_is_error(self, registry):
        result = await registry.call_tool("search_releases", {"date_from": "20-11-2026"})
        assert result["isError"] is True

    async def test_limit_is_capped_server_side(self, registry):
        result = await registry.call_tool("search_releases", {"limit": 10_000})
        assert result["isError"] is False  # capped, not rejected
        result = await registry.call_tool("search_releases", {"limit": 0})
        assert result["isError"] is True

    async def test_unknown_tool_and_bad_args(self, registry):
        assert (await registry.call_tool("nope", {}))["isError"] is True
        assert (await registry.call_tool("search_releases", {"limit": "ten"}))["isError"] is True
        assert (await registry.call_tool("search_releases", None))["isError"] is False

    async def test_sentiment_summary_aggregates(self, registry):
        result = await registry.call_tool("sentiment_summary", {})
        data = result["structuredContent"]
        assert data["total_releases"] == 2  # releases, not topic rows
        # by_sentiment counts per unwind row: the 2-topic destatis release
        # contributes its 'positive' sentiment once per topic tag
        assert data["by_sentiment"] == {"negative": 1, "positive": 2}
        assert data["by_publisher"]["istat"] == 1

    async def test_topics_are_nonexclusive_tags(self, registry):
        """A multi-topic release matches both topics; by_topic counts per tag."""
        r1 = await registry.call_tool("search_releases", {"topic": "GDP"})
        assert r1["structuredContent"]["total_matching"] == 1
        r2 = await registry.call_tool("search_releases", {"topic": "International trade"})
        assert r2["structuredContent"]["total_matching"] == 1
        assert r2["structuredContent"]["results"][0]["publisher"] == "destatis"

        summary = (await registry.call_tool("sentiment_summary", {}))["structuredContent"]
        # The destatis release carries two topics → counted once under each
        assert summary["by_topic"]["GDP"] == 1
        assert summary["by_topic"]["International trade"] == 1
        assert summary["total_releases"] == 2  # release count, not topic count

    async def test_sentiment_summary_date_filter(self, registry):
        result = await registry.call_tool(
            "sentiment_summary", {"date_from": "2026-11-15", "date_to": "2026-11-21"}
        )
        data = result["structuredContent"]
        assert data["total_releases"] == 1
        assert data["by_publisher"] == {"istat": 1}

    async def test_upcoming_capped_window(self, registry):
        result = await registry.call_tool("get_upcoming_releases", {"days_ahead": 999})
        assert result["isError"] is False
        assert result["structuredContent"]["window_days"] == MAX_DAYS_AHEAD

    async def test_latest_capped_limit(self, registry):
        result = await registry.call_tool("get_latest_releases", {"limit": 10_000})
        assert result["isError"] is False
        assert result["structuredContent"]["returned"] <= MAX_LATEST_LIMIT


# ─── JSON-RPC transport tests ────────────────────────────────


class TestRpcHandler:
    async def test_initialize(self, registry):
        handler = McpRpcHandler(registry)
        body, status = await handler.handle(
            _rpc_request("initialize", {"protocolVersion": MCP_PROTOCOL_VERSION})
        )
        assert status == 200
        assert body["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION
        assert body["result"]["serverInfo"]["name"] == "congiuntura-live-mcp"

    async def test_tools_list_and_call(self, registry):
        handler = McpRpcHandler(registry)
        body, _ = await handler.handle(_rpc_request("tools/list"))
        names = [t["name"] for t in body["result"]["tools"]]
        assert "search_releases" in names

        body, status = await handler.handle(
            _rpc_request(
                "tools/call", {"name": "search_releases", "arguments": {"country": "Italy"}}
            )
        )
        assert status == 200
        assert body["result"]["isError"] is False
        assert body["result"]["structuredContent"]["results"][0]["publisher"] == "istat"

    async def test_tool_error_is_result_not_rpc_error(self, registry):
        handler = McpRpcHandler(registry)
        body, status = await handler.handle(
            _rpc_request("tools/call", {"name": "search_releases", "arguments": {"topic": "nope"}})
        )
        assert status == 200
        assert body["result"]["isError"] is True  # MCP convention

    async def test_unknown_method(self, registry):
        handler = McpRpcHandler(registry)
        body, status = await handler.handle(_rpc_request("resources/read", {"uri": "x"}))
        assert body["error"]["code"] == -32601

    async def test_notification_returns_202(self, registry):
        handler = McpRpcHandler(registry)
        body, status = await handler.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        assert body is None and status == 202

    async def test_batch(self, registry):
        handler = McpRpcHandler(registry)
        body, status = await handler.handle(
            [_rpc_request("tools/list", rpc_id=1), _rpc_request("ping", rpc_id=2)]
        )
        assert status == 200 and isinstance(body, list) and len(body) == 2


# ─── HTTP endpoint tests (CORS + lifecycle) ──────────────────


class TestHttpEndpoint:
    def _app(self) -> FastAPI:
        app = FastAPI()
        register_mcp(app)
        return app

    def test_cors_allows_agent_subdomain(self):
        client = TestClient(self._app())
        response = client.options(
            "/mcp",
            headers={
                "Origin": "https://congiuntura-agent.by.gg8.eu",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.status_code == 200
        assert (
            response.headers["access-control-allow-origin"] == "https://congiuntura-agent.by.gg8.eu"
        )

    def test_cors_allows_localhost_dev(self):
        client = TestClient(self._app())
        response = client.options(
            "/mcp",
            headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
        )
        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"

    def test_cors_rejects_unknown_origin(self):
        client = TestClient(self._app())
        response = client.options(
            "/mcp",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
        )
        assert "access-control-allow-origin" not in response.headers

    def test_post_before_init_returns_503(self):
        client = TestClient(self._app())
        response = client.post("/mcp", json=_rpc_request("tools/list"))
        assert response.status_code == 503

    def test_post_with_registry_end_to_end(self):
        app = self._app()
        app.state.mcp_registry = McpToolRegistry(FakeRepo())
        client = TestClient(app)
        response = client.post(
            "/mcp",
            json=_rpc_request("tools/call", {"name": "get_pipeline_status", "arguments": {}}),
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["result"]["isError"] is False
        assert payload["result"]["structuredContent"] == {"pipelines": []}

    def test_malformed_json_400(self):
        app = self._app()
        app.state.mcp_registry = McpToolRegistry(FakeRepo())
        client = TestClient(app)
        response = client.post(
            "/mcp", content=b"not-json", headers={"content-type": "application/json"}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == -32700


# ─── Read-only repository guard test ─────────────────────────


class TestRepositoryReadOnly:
    async def test_repo_has_no_write_methods(self):
        """The MCP repository must expose no mutation methods."""
        write_like = [
            name
            for name in dir(AgentReadOnlyRepository)
            if any(
                token in name
                for token in ("insert", "update", "delete", "upsert", "replace", "mark", "set_")
            )
        ]
        assert write_like == [], f"write-like methods leaked: {write_like}"
