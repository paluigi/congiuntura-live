"""Pytest fixtures shared across test modules."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def rss_sample_istat() -> str:
    """A representative RSS 2.0 feed from Istat (3 items)."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel>
    <title>Conti nazionali – Istat</title>
    <link>https://www.istat.it</link>
    <language>it-IT</language>
    <item>
        <title>Pil e indebitamento delle AP: dati preliminari</title>
        <link>https://www.istat.it/communicato/pil-2025q1</link>
        <description>
            Nel primo trimestre del 2025 il PIL ha registrato
            una crescita dello 0,3%.
        </description>
        <pubDate>Mon, 30 Jun 2025 10:00:00 +0000</pubDate>
    </item>
    <item>
        <title>Conti nazionali: revisione annuale</title>
        <link>https://www.istat.it/communicato/revisione-2025</link>
        <description>Revisione dei conti nazionali con base 2020.</description>
        <pubDate>Wed, 18 Jun 2025 10:00:00 +0000</pubDate>
    </item>
    <item>
        <title>Pil e indebitamento delle AP: dati definitivi</title>
        <link>https://www.istat.it/communicato/pil-2024q4</link>
        <description>Nel quarto trimestre del 2024 il PIL è rimasto stabile.</description>
        <pubDate>Mon, 02 Jun 2025 10:00:00 +0000</pubDate>
    </item>
</channel>
</rss>"""


@pytest.fixture
def atom_sample_eurostat() -> str:
    """A representative Atom feed from Eurostat (2 entries)."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
    <title>Eurostat - Custom RSS Feed</title>
    <link rel="alternate" href="https://ec.europa.eu/eurostat"/>
    <entry>
        <title>Flash estimate: HICP +2.1% in June 2025</title>
        <link href="https://ec.europa.eu/eurostat/web/products-news-releases/-/dd-20250630-1"/>
        <id>urn:uuid:eurostat-hicp-flash</id>
        <updated>2025-06-30T10:00:00Z</updated>
        <summary>The euro area HICP flash estimate was +2.1% in June 2025.</summary>
    </entry>
    <entry>
        <title>Industrial production +0.8% in May 2025</title>
        <link href="https://ec.europa.eu/eurostat/web/products-news-releases/-/dd-20250614-2"/>
        <id>urn:uuid:eurostat-indprod</id>
        <updated>2025-06-14T10:00:00Z</updated>
        <summary>The euro area industrial production rose by 0.8% in May 2025.</summary>
    </entry>
</feed>"""


@pytest.fixture
def rss_sample_destatis() -> str:
    """A representative RSS 2.0 feed from Destatis (2 items, German)."""
    return """<?xml version="1.0"?>
<rss version="2.0">
<channel>
    <title>Statistisches Bundesamt</title>
    <link>https://www.destatis.de</link>
    <language>de-de</language>
    <item>
        <title>Inflationsrate bei +2,3 % im Juni 2025</title>
        <link>https://www.destatis.de/DE/Press/2025/07/PE25_243.html</link>
        <description>Die Inflationsrate in Deutschland lag im Juni 2025 bei +2,3 %.</description>
        <pubDate>Thu, 10 Jul 2025 09:30:00 +0200</pubDate>
    </item>
    <item>
        <title>Auftragseingang in der Industrie +1,9 % im Mai 2025</title>
        <link>https://www.destatis.de/DE/Press/2025/07/PE25_235.html</link>
        <description>Der Auftragseingang in der Industrie stieg im Mai 2025.</description>
        <pubDate>Sun, 06 Jul 2025 09:30:00 +0200</pubDate>
    </item>
</channel>
</rss>"""


@pytest.fixture
def ics_sample_eurostat() -> str:
    """A representative eventsIcal export of the Eurostat release calendar.

    Mirrors the real feed (https://ec.europa.eu/eurostat/o/calendars/eventsIcal):
    all-day VEVENTs with X-THEME/X-CATEGORY and ICS-escaped commas, no
    DESCRIPTION/URL properties.
    """
    return "\r\n".join([
        "BEGIN:VCALENDAR",
        "PRODID:-//Eurostat//Release calendar//EN",
        "X-WR-CALNAME:Eurostat release calendar",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "BEGIN:VTIMEZONE",
        "TZID:Europe/Luxembourg",
        "END:VTIMEZONE",
        "BEGIN:VEVENT",
        "DTSTAMP:20261106T183919Z",
        "DTSTART;VALUE=DATE:20260814",
        "SUMMARY:Retail trade",
        "CATEGORIES:Purple Category",
        "TRANSP:TRANSPARENT",
        "X-THEME:economy",
        "X-CATEGORY:Data release\\,Euro indicators release",
        "UID:20270418T020424Z-1@paloontrar.cc.cec.eu.int",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "DTSTAMP:20261106T183919Z",
        "DTSTART;VALUE=DATE:20270106",
        "SUMMARY:House price index\\, Q2/2026",
        "CATEGORIES:Purple Category",
        "TRANSP:TRANSPARENT",
        "X-THEME:economy",
        "X-CATEGORY:Data release",
        "UID:20270418T020425Z-1@paloontrar.cc.cec.eu.int",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "DTSTAMP:20261106T183919Z",
        "DTSTART;VALUE=DATE:20260820",
        "SUMMARY:Housing statistics in Europe",
        "CATEGORIES:Purple Category",
        "TRANSP:TRANSPARENT",
        "X-THEME:population",
        "X-CATEGORY:Publication\\,Statistics Explained",
        "UID:20270418T020426Z-1@paloontrar.cc.cec.eu.int",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "DTSTAMP:20261106T183919Z",
        "DTSTART;VALUE=DATE:20260821",
        "CATEGORIES:Purple Category",
        "TRANSP:TRANSPARENT",
        "X-THEME:economy",
        "X-CATEGORY:Data release",
        "UID:20270418T020427Z-1@paloontrar.cc.cec.eu.int",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "DTSTAMP:20261106T183919Z",
        "SUMMARY:No date here",
        "CATEGORIES:Purple Category",
        "TRANSP:TRANSPARENT",
        "X-THEME:economy",
        "X-CATEGORY:Data release",
        "UID:20270418T020428Z-1@paloontrar.cc.cec.eu.int",
        "END:VEVENT",
        "END:VCALENDAR",
    ])
