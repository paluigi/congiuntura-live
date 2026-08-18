"""LLM processing pipeline using outlines-cascade.

Fetches unprocessed raw press releases, scrapes their content, extracts
structured data via outlines-cascade, and assembles the final
ProcessedRelease document with auto-populated fields (url, date, publisher).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from outlines_cascade import generate, load_config
from outlines_cascade.config import AppConfig as CascadeAppConfig

from .scraper import PressReleaseScraper
from .settings import ProcessingConfig, load_extraction_model

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are an expert economic analyst specializing in euro-area macroeconomic statistics. "
    "Extract structured information from the following official statistics press release. "
    "Select all applicable topics (topics are tags, not mutually exclusive), identify the "
    "geographic scope, assess the economic sentiment, write a concise English summary, "
    "and extract key numerical figures."
)


class ReleaseProcessor:
    """Orchestrates the LLM extraction pipeline for raw press releases.

    The LLM sees only the title and scraped page text — never URLs, dates,
    or publisher metadata. Those are copied verbatim from the raw document
    after generation to prevent hallucination.
    """

    def __init__(
        self,
        processing_cfg: ProcessingConfig,
    ) -> None:
        self._cfg = processing_cfg
        self._scraper = PressReleaseScraper(max_chars=processing_cfg.max_content_chars)
        self._cascade_config: CascadeAppConfig | None = None
        self._extraction_model = load_extraction_model(processing_cfg.model_path)

    def _ensure_config(self) -> CascadeAppConfig:
        """Lazy-load the outlines-cascade config (reads env vars at call time)."""
        if self._cascade_config is None:
            self._cascade_config = load_config(self._cfg.llm_config)
        return self._cascade_config

    async def process_one(self, raw_doc: dict[str, Any]) -> dict[str, Any] | None:
        """Process a single raw press release document.

        Returns a ProcessedRelease dict ready for MongoDB insertion, or
        None if processing failed.
        """
        url = raw_doc.get("url", "")
        title = raw_doc.get("title", "")
        summary = raw_doc.get("summary", "")

        # 1. Scrape the press release page for richer context.
        page_content = await self._scraper.extract_content(url)
        if page_content:
            llm_input = f"Title: {title}\n\n{page_content}"
        else:
            # Fallback to the feed summary if scraping fails.
            logger.debug("Using feed summary fallback for %s", url)
            llm_input = f"Title: {title}\n\n{summary}"

        # 2. Run structured generation via outlines-cascade.
        try:
            result = await generate(
                prompt=llm_input,
                output_type=self._extraction_model,
                config=self._ensure_config(),
                cascade_name=self._cfg.cascade_name,
                system_prompt=_SYSTEM_PROMPT,
            )
        except Exception:
            logger.exception("LLM processing failed for %s", url)
            return None

        extraction = result.value

        # 3. Assemble ProcessedRelease: LLM fields + auto fields from raw.
        processed: dict[str, Any] = {
            # Auto fields (copied verbatim — never seen by the LLM)
            "url_hash": raw_doc["url_hash"],
            "url": url,
            "title": title,
            "publisher": raw_doc.get("publisher", ""),
            "publisher_full": raw_doc.get("publisher_full", ""),
            "feed_label": raw_doc.get("feed_label", ""),
            "language": raw_doc.get("language", ""),
            "published": raw_doc.get("published"),
            "fetched_at": raw_doc.get("fetched_at"),
            "processed_at": datetime.now(UTC),
            # LLM metadata
            "processing_model": f"{result.provider}/{result.model}",
            # LLM-generated fields
            "topics": list(dict.fromkeys(extraction.topics)),
            "country": extraction.country,
            "sentiment": extraction.sentiment,
            "title_en": extraction.title_en,
            "summary_en": extraction.summary_en,
            "key_figures": extraction.key_figures,
        }
        logger.info(
            "Processed %s → topics=%s, country=%s",
            url[:60],
            extraction.topics,
            extraction.country,
        )
        return processed

    async def close(self) -> None:
        await self._scraper.close()
