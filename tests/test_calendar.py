"""Tests for the calendar module (offline — parsing logic only)."""

from datetime import UTC, datetime

from congiuntura_live.calendar.collectors import (
    DestatisCollector,
    EurostatCollector,
    ForexFactoryCollector,
    ICSCollector,
    make_uid,
)


class TestForexFactoryParsing:
    def test_parse_datetime_regular(self):
        # 8:00am ET (EDT, UTC-4) = 12:00 UTC
        dt = ForexFactoryCollector._parse_datetime("Aug 14", "8:00am", 2026)
        assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 8, 14, 12)

    def test_parse_datetime_pm(self):
        dt = ForexFactoryCollector._parse_datetime("Aug 14", "4:00pm", 2026)
        assert dt.hour == 20

    def test_parse_datetime_tentative(self):
        dt = ForexFactoryCollector._parse_datetime("Aug 14", "Tentative", 2026)
        assert dt.day == 14
        assert dt.hour == 4  # midnight ET = 04:00 UTC

    def test_parse_datetime_12am(self):
        dt = ForexFactoryCollector._parse_datetime("Aug 14", "12:00am", 2026)
        assert dt.hour == 4

    def test_parse_datetime_12pm(self):
        dt = ForexFactoryCollector._parse_datetime("Aug 14", "12:00pm", 2026)
        assert dt.hour == 16

    def test_parse_datetime_winter_est(self):
        # January = EST (UTC-5): 8:00am ET = 13:00 UTC
        dt = ForexFactoryCollector._parse_datetime("Jan 15", "8:00am", 2027)
        assert dt.hour == 13

    def test_shift_month(self):
        assert ForexFactoryCollector._shift_month(2026, 12, 1) == (2027, 1)
        assert ForexFactoryCollector._shift_month(2026, 1, -1) == (2025, 12)

    def test_month_range_cross_year(self):
        months = ForexFactoryCollector._month_range(2026, 11, 2027, 2)
        assert months == [(2026, 11), (2026, 12), (2027, 1), (2027, 2)]

    def test_routine_window_is_5_months(self):
        c = ForexFactoryCollector()
        now = datetime.now(UTC)
        sy, sm = c._shift_month(now.year, now.month, -c.LOOKBACK_MONTHS)
        ey, em = c._shift_month(now.year, now.month, c.LOOKAHEAD_MONTHS)
        assert len(c._month_range(sy, sm, ey, em)) == 5


class TestDestatisParsing:
    def test_parse_date_summer(self):
        # 08:00 Berlin (CEST, UTC+2) = 06:00 UTC
        dt = DestatisCollector._parse_cet_date("2026.08.14")
        assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 8, 14, 6)

    def test_parse_date_winter(self):
        # 08:00 Berlin (CET, UTC+1) = 07:00 UTC
        dt = DestatisCollector._parse_cet_date("2026.12.18")
        assert dt.hour == 7


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _FakeAsyncClient:
    """Offline stand-in for httpx.AsyncClient: every URL returns `text`."""

    text = ""

    def __init__(self, *args, **kwargs): ...

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str) -> _FakeResponse:
        return _FakeResponse(self.text)


class TestICSParsing:
    def test_all_day_summer_becomes_9am_utc(self, ics_sample_eurostat):
        # DTSTART;VALUE=DATE + default hour 11:00 Europe/Brussels (CEST, UTC+2)
        records = EurostatCollector()._parse_events(ics_sample_eurostat)
        rec = next(r for r in records if r["title"] == "Retail trade")
        assert rec["release_dt"] == datetime(2026, 8, 14, 9, 0, tzinfo=UTC)
        assert rec["source"] == "eurostat"
        assert rec["reference_period"] is None  # the real feed has no DESCRIPTION
        assert rec["url"] is None

    def test_all_day_winter_becomes_10am_utc(self, ics_sample_eurostat):
        # 11:00 Europe/Brussels (CET, UTC+1) = 10:00 UTC
        records = EurostatCollector()._parse_events(ics_sample_eurostat)
        rec = next(r for r in records if r["title"].startswith("House price index"))
        assert rec["release_dt"] == datetime(2027, 1, 6, 10, 0, tzinfo=UTC)

    def test_keeps_only_release_categories(self, ics_sample_eurostat):
        records = EurostatCollector()._parse_events(ics_sample_eurostat)
        titles = {r["title"] for r in records}
        assert titles == {"Retail trade", "House price index, Q2/2026"}

    def test_plain_ics_keeps_all_categories(self, ics_sample_eurostat):
        # a generic ICSCollector (e.g. Istat/INE) has no category filter
        records = ICSCollector(code="x", feed_urls=[])._parse_events(ics_sample_eurostat)
        assert "Housing statistics in Europe" in {r["title"] for r in records}

    def test_skips_events_without_summary_or_dtstart(self, ics_sample_eurostat):
        records = EurostatCollector()._parse_events(ics_sample_eurostat)
        assert len(records) == 2

    def test_ref_period_extraction(self):
        desc = "Intro text\\nReference period: Q2/2026\\nMore text"
        assert ICSCollector._extract_ref_period(desc) == "Q2/2026"
        assert ICSCollector._extract_ref_period("no period info") is None

    async def test_collect_dedupes_same_event_across_urls(self, ics_sample_eurostat, monkeypatch):
        # Both configured URLs serve the same events → collected once
        monkeypatch.setattr(_FakeAsyncClient, "text", ics_sample_eurostat)
        monkeypatch.setattr(
            "congiuntura_live.calendar.collectors.httpx.AsyncClient", _FakeAsyncClient
        )
        records = await EurostatCollector().collect()
        assert len(records) == 2
        assert len({r["source_uid"] for r in records}) == 2


class TestUID:
    def test_make_uid_stable(self):
        assert make_uid("istat", "CPI", "2026-08-14T10:00:00+00:00") == \
               make_uid("istat", "CPI", "2026-08-14T10:00:00+00:00")

    def test_make_uid_distinct(self):
        assert make_uid("istat", "CPI", "a") != make_uid("ine", "CPI", "a")
