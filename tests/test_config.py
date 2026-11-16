"""Tests for TOML configuration loading."""

from __future__ import annotations

from congiuntura_live.settings import (
    load_app_config,
    load_feeds_config,
)


class TestAppConfig:
    def test_load_app_config(self):
        cfg = load_app_config("config/app.toml")
        assert cfg.polling.interval_minutes == 5
        assert cfg.server.host == "0.0.0.0"
        assert cfg.server.port == 8000

    def test_default_when_file_missing(self):
        cfg = load_app_config("nonexistent.toml")
        assert cfg.polling.interval_minutes == 5  # default
        assert cfg.server.port == 8000


class TestFeedsConfig:
    def test_load_all_agencies(self):
        feeds = load_feeds_config("config/feeds.toml")
        assert len(feeds) == 7
        assert set(feeds.keys()) == {
            "eurostat",
            "istat",
            "ine",
            "insee",
            "destatis",
            "cso",
            "dgecofin",
        }

    def test_agency_fields(self):
        feeds = load_feeds_config("config/feeds.toml")
        istat = feeds["istat"]
        assert istat.name == "Istat"
        assert istat.language == "it"
        assert istat.country == "Italy"
        assert len(istat.feeds) >= 4

    def test_feed_urls_are_valid(self):
        feeds = load_feeds_config("config/feeds.toml")
        for slug, agency in feeds.items():
            for feed in agency.feeds:
                assert feed.url.startswith("https://"), f"{slug} feed URL not HTTPS: {feed.url}"
                assert feed.label, f"{slug} feed missing label"

    def test_istat_has_economic_topics(self):
        feeds = load_feeds_config("config/feeds.toml")
        istat_labels = [f.label.lower() for f in feeds["istat"].feeds]
        assert any("national" in label or "conti" in label for label in istat_labels)
        assert any("price" in label or "prezzi" in label for label in istat_labels)

    def test_insee_uses_english_flux(self):
        feeds = load_feeds_config("config/feeds.toml")
        for feed in feeds["insee"].feeds:
            assert "/en/flux/" in feed.url, "INSEE should use English flux URLs"

    def test_cso_config(self):
        feeds = load_feeds_config("config/feeds.toml")
        cso = feeds["cso"]
        assert cso.name == "CSO"
        assert cso.language == "en"
        assert cso.country == "Ireland"
        assert len(cso.feeds) >= 1
        assert feeds["cso"].feeds[0].url.startswith("https://")

    def test_dgecofin_config(self):
        feeds = load_feeds_config("config/feeds.toml")
        dgecofin = feeds["dgecofin"]
        assert dgecofin.name == "DG EcoFin"
        assert dgecofin.language == "en"
        assert dgecofin.country == "Euro area"
        assert len(dgecofin.feeds) >= 1
        for feed in dgecofin.feeds:
            assert feed.url.startswith("https://")
