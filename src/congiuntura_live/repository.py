"""Async MongoDB repositories using motor."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from pymongo import AsyncMongoClient
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase

from .models import PressRelease

logger = logging.getLogger(__name__)

RAW_COLLECTION = "press_releases"
PROCESSED_COLLECTION = "processed_releases"


class PressReleaseRepository:
    """Asynchronous repository for raw press releases in MongoDB."""

    def __init__(self, mongo_url: str, database_name: str) -> None:
        self._client = AsyncMongoClient(mongo_url)
        self._db: AsyncDatabase = self._client[database_name]
        self._collection: AsyncCollection = self._db[RAW_COLLECTION]
        self._processed: AsyncCollection = self._db[PROCESSED_COLLECTION]

    async def ensure_indexes(self) -> None:
        """Create indexes for dedup and query performance on both collections."""
        # Raw collection
        await self._collection.create_index("url_hash", unique=True)
        await self._collection.create_index("publisher")
        await self._collection.create_index([("published", -1)])
        await self._collection.create_index([("publisher", 1), ("published", -1)])
        # Processed collection
        await self._processed.create_index("url_hash", unique=True)
        await self._processed.create_index("publisher")
        await self._processed.create_index([("published", -1)])
        await self._processed.create_index([("publisher", 1), ("published", -1)])
        await self._processed.create_index("topic")
        await self._processed.create_index("country")
        await self._processed.create_index("sentiment")
        await self._processed.create_index("processing_model")
        logger.info(
            "MongoDB indexes ensured on '%s' and '%s'",
            RAW_COLLECTION,
            PROCESSED_COLLECTION,
        )

    async def insert_many_new(self, releases: list[PressRelease]) -> tuple[int, int]:
        """Insert raw releases, skipping duplicates by url_hash."""
        inserted = 0
        skipped = 0
        for release in releases:
            doc = release.to_doc()
            try:
                await self._collection.insert_one(doc)
                inserted += 1
            except Exception:  # DuplicateKeyError from unique index
                skipped += 1
        if inserted or skipped:
            logger.info("Insert: %d new, %d duplicates skipped", inserted, skipped)
        return inserted, skipped

    # ── Raw collection queries ───────────────────────────────

    async def search_raw(
        self,
        publisher: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Search raw press releases by publisher and date range."""
        query = self._build_raw_query(publisher, date_from, date_to)
        cursor = self._collection.find(query, {"_id": 0}).sort("published", -1).limit(limit)
        return await cursor.to_list(length=limit)

    async def count_raw(
        self,
        publisher: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> int:
        query = self._build_raw_query(publisher, date_from, date_to)
        return await self._collection.count_documents(query)

    async def count_total_raw(self) -> int:
        return await self._collection.count_documents({})

    async def list_publishers(self) -> list[str]:
        return await self._collection.distinct("publisher")

    # ── Processed collection queries ─────────────────────────

    async def insert_processed(self, doc: dict[str, Any]) -> bool:
        """Insert a processed release. Returns True if inserted, False if duplicate."""
        try:
            await self._processed.insert_one(doc)
            return True
        except Exception:
            return False

    async def search_processed(
        self,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Search processed releases by any combination of fields."""
        query = self._build_processed_query(filters)
        cursor = self._processed.find(query, {"_id": 0}).sort("published", -1).limit(limit)
        return await cursor.to_list(length=limit)

    async def count_processed(self, filters: dict[str, Any] | None = None) -> int:
        query = self._build_processed_query(filters)
        return await self._processed.count_documents(query)

    async def count_total_processed(self) -> int:
        return await self._processed.count_documents({})

    async def find_processed_missing_field(
        self, field: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Processed documents where `field` is absent (used for backfills)."""
        query = {field: {"$exists": False}}
        cursor = self._processed.find(query, {"_id": 0}).sort("published", -1).limit(limit)
        return await cursor.to_list(length=limit)

    async def set_processed_field(self, url_hash: str, field: str, value: Any) -> bool:
        """Set a single field on a processed document by url_hash."""
        result = await self._processed.update_one(
            {"url_hash": url_hash},
            {"$set": {field: value, "updated_at": datetime.now(UTC)}},
        )
        return result.modified_count > 0

    # ── Unprocessed query (join raw minus processed) ─────────

    async def find_unprocessed(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return raw releases not yet present in the processed collection."""
        all_raw = await self._collection.find({}, {"_id": 0}).to_list(length=10000)
        processed_hashes = set(await self._processed.distinct("url_hash"))
        unprocessed = [r for r in all_raw if r.get("url_hash") not in processed_hashes]
        logger.info(
            "Unprocessed: %d raw, %d processed, %d pending",
            len(all_raw),
            len(processed_hashes),
            len(unprocessed),
        )
        return unprocessed[:limit]

    # ── Query builders ───────────────────────────────────────

    @staticmethod
    def _build_raw_query(
        publisher: str | None,
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {}
        if publisher and publisher != "all":
            query["publisher"] = publisher
        date_filter: dict[str, Any] = {}
        if date_from:
            date_filter["$gte"] = date_from
        if date_to:
            date_filter["$lte"] = date_to
        if date_filter:
            query["published"] = date_filter
        return query

    @staticmethod
    def _build_processed_query(filters: dict[str, Any] | None) -> dict[str, Any]:
        """Build a MongoDB query from frontend filter signals.

        ``topic``, ``country``, ``sentiment``, ``publisher`` values are
        lists — mapped to ``$in`` for multi-select OR semantics.
        ``q`` is a free-text fragment matched case-insensitively against
        the English title/summary and key figures.
        """
        if not filters:
            return {}
        query: dict[str, Any] = {}
        for field, value in filters.items():
            if field in ("publisher", "topic", "country", "sentiment"):
                if isinstance(value, list):
                    if value:
                        query[field] = {"$in": value}
                elif value and value != "all":
                    query[field] = value
            elif field in ("date_from", "date_to"):
                continue  # handled below
        # Free-text fragment (escaped so user input matches literally)
        q = filters.get("q")
        if q:
            pattern = {"$regex": re.escape(q), "$options": "i"}
            query["$or"] = [
                {"title_en": pattern},
                {"summary_en": pattern},
                {"key_figures": pattern},
            ]
        # Date range
        date_filter: dict[str, Any] = {}
        if filters.get("date_from"):
            date_filter["$gte"] = filters["date_from"]
        if filters.get("date_to"):
            date_filter["$lte"] = filters["date_to"]
        if date_filter:
            query["published"] = date_filter
        return query

    async def close(self) -> None:
        await self._client.close()
