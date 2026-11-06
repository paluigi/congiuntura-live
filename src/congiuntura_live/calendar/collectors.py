"""Calendar collectors — NSO ICS/HTML/API + ForexFactory, async httpx port.

Ported from nso-calendar. All collectors return lists of release dicts
ready for MongoDB upsert (keys match CalendarRepository collections).
"""

from __future__ import annotations

import hashlib
import logging
import re
from abc import ABC, abstractmethod
from datetime import UTC, date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
import icalendar
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

NSO_COLLECTION = "nso_releases"
FF_COLLECTION = "ff_releases"


def make_uid(*parts: str) -> str:
    """Stable hash for deduplication."""
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:64]


# ─── NSO collectors ────────────────────────────────────────────


class BaseNSOCollector(ABC):
    """Base class for NSO calendar collectors. Returns release dicts."""

    @abstractmethod
    def source_code(self) -> str: ...

    @abstractmethod
    async def collect(self) -> list[dict]: ...

    def _record(self, title: str, release_dt: datetime, reference_period: str | None = None, url: str | None = None) -> dict:
        return {
            "source": self.source_code(),
            "title": title,
            "release_dt": release_dt,
            "reference_period": reference_period,
            "url": url,
            "source_uid": make_uid(self.source_code(), title, release_dt.isoformat()),
        }


class ICSCollector(BaseNSOCollector):
    """Fetches and parses iCalendar (.ics) feeds."""

    def __init__(self, code: str, feed_urls: list[str], default_tz: str = "UTC", default_hour: int = 9):
        self._code = code
        self._urls = feed_urls
        self._tz = ZoneInfo(default_tz)
        self._hour = default_hour

    def source_code(self) -> str:
        return self._code

    def _keep(self, event: icalendar.Event) -> bool:
        """Hook for subclasses: whether to keep a parsed VEVENT."""
        return True

    def _parse_events(self, text: str) -> list[dict]:
        """Parse one iCalendar payload into release records (no dedup)."""
        records: list[dict] = []
        for event in icalendar.Calendar.from_ical(text).walk("VEVENT"):
            dtstart = event.get("DTSTART")
            if dtstart is None:
                continue
            dt = dtstart.dt
            if isinstance(dt, datetime):
                dt_utc = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=self._tz).astimezone(UTC)
            else:
                dt_utc = datetime.combine(dt, time(self._hour, 0), tzinfo=self._tz).astimezone(UTC)

            summary = str(event.get("SUMMARY", "")).strip()
            if not summary:
                continue
            if not self._keep(event):
                continue
            ref_period = self._extract_ref_period(str(event.get("DESCRIPTION", "")))
            url_field = event.get("URL")
            records.append(self._record(
                summary, dt_utc, ref_period,
                str(url_field) if url_field else None,
            ))
        return records

    async def collect(self) -> list[dict]:
        records: list[dict] = []
        seen_uids: set[str] = set()
        async with httpx.AsyncClient(timeout=30, headers=HEADERS, follow_redirects=True) as client:
            for url in self._urls:
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    parsed = self._parse_events(resp.text)
                    if not parsed:
                        logger.warning(
                            "ICSCollector [%s] URL %s returned 0 events", self._code, url
                        )
                    for record in parsed:
                        if record["source_uid"] in seen_uids:  # same event via multiple URLs
                            continue
                        seen_uids.add(record["source_uid"])
                        records.append(record)
                except Exception:
                    logger.exception("ICSCollector [%s] URL %s failed", self._code, url)
        return records

    @staticmethod
    def _extract_ref_period(desc: str) -> str | None:
        for pattern in (r"[Rr]eference period:?\s*(.+?)(?:\\n|,|$)", r"[Pp]eriodo di riferimento:?\s*(.+?)(?:\\n|,|$)"):
            m = re.search(pattern, desc)
            if m:
                return m.group(1).strip()
        return None


class IstatCollector(ICSCollector):
    def __init__(self):
        super().__init__(
            code="istat",
            feed_urls=["https://www.google.com/calendar/ical/4s57ih6d08n330qrm9ee575nog%40group.calendar.google.com/public/basic.ics"],
            default_tz="Europe/Rome", default_hour=10,
        )


class INECollector(ICSCollector):
    def __init__(self):
        super().__init__(
            code="ine",
            feed_urls=["https://www.ine.es/dynt3/Calendario/en/calendario.ics"],
            default_tz="Europe/Madrid", default_hour=9,
        )


class EurostatCollector(ICSCollector):
    """Eurostat release calendar via the eventsIcal export of the online
    release calendar (category 1 = data release, 2 = euro indicators release;
    theme 0 = all themes)."""

    _KEEP_CATEGORIES = ("data release", "euro indicators")

    def __init__(self):
        super().__init__(
            code="eurostat",
            feed_urls=[
                "https://ec.europa.eu/eurostat/o/calendars/eventsIcal?theme=0&category=1",
                "https://ec.europa.eu/eurostat/o/calendars/eventsIcal?theme=0&category=2",
            ],
            default_tz="Europe/Brussels", default_hour=11,
        )

    def _keep(self, event: icalendar.Event) -> bool:
        # Belt-and-braces alongside the server-side category filter: skip
        # anything without a release category (publications, podcasts, ...).
        categories = str(event.get("X-CATEGORY", "")).lower()
        return any(c in categories for c in self._KEEP_CATEGORIES)


class DestatisCollector(BaseNSOCollector):
    """Annual release calendar via 13 topic facets."""

    SEARCH_URL = "https://www.destatis.de/SiteGlobals/Forms/Suche/Termine/EN/Terminsuche_Formular.html"
    PUBLISH_TZ = ZoneInfo("Europe/Berlin")
    TOPICS = (
        "preise", "industrie_verarbeitendes_gewerbe", "bauen", "unternehmen",
        "arbeitsmarkt", "aussenhandel", "gross_einzelhandel", "verdienste",
        "dienstleistungen", "volkswirtschaftliche_gesamtrechnungen_inlandsprodukt",
        "arbeits_lohnnebenkosten", "bevoelkerung", "verkehrsunfaelle",
    )

    def source_code(self) -> str:
        return "destatis"

    @staticmethod
    def _parse_cet_date(date_str: str) -> datetime:
        """Parse '2026.08.14' or '2026.08.14 (deadline)' → datetime at 08:00 CET."""
        m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", date_str)
        if not m:
            raise ValueError(f"Cannot parse Destatis date: {date_str}")
        tz = ZoneInfo("Europe/Berlin")
        return datetime(int(m[1]), int(m[2]), int(m[3]), 8, 0, tzinfo=tz).astimezone(UTC)

    async def collect(self) -> list[dict]:
        all_records: list[dict] = []
        seen: set[tuple[str, datetime]] = set()
        async with httpx.AsyncClient(timeout=30, headers=HEADERS, follow_redirects=True) as client:
            for topic in self.TOPICS:
                try:
                    resp = await client.get(self.SEARCH_URL, params={"submit": "x", "cl2Taxonomies_Themen_0": topic})
                    soup = BeautifulSoup(resp.text, "html.parser")
                    for result in soup.select("div.c-result--event-preview"):
                        heading = result.select_one(".c-result__heading")
                        title = heading.get_text(strip=True).rstrip("0123456789") if heading else ""
                        if not title:
                            continue
                        ref, date_str = "", ""
                        for m in result.select(".c-result-meta__item"):
                            text = m.get_text(strip=True).replace("ICS/iCal", "").strip()
                            if "Reporting period" in text:
                                ref = text.replace("Reporting period:", "").strip()
                            elif "Date of issue" in text:
                                date_str = text.replace("Date of issue:", "").strip()
                        dm = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", date_str)
                        if not dm:
                            continue
                        dt = self._parse_cet_date(date_str)
                        if (title, dt) in seen:
                            continue
                        seen.add((title, dt))
                        all_records.append(self._record(title, dt, ref or None))
                except Exception:
                    logger.exception("Destatis topic '%s' failed", topic)
        return all_records


class INSEECollector(BaseNSOCollector):
    """Rolling 2-week embargo calendar."""

    CALENDAR_URL = "https://www.insee.fr/fr/information/5235017"
    TZ = ZoneInfo("Europe/Paris")
    MONTHS_FR = {
        "janvier": 1, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
        "juillet": 7, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
    }

    def source_code(self) -> str:
        return "insee"

    async def collect(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=30, headers=HEADERS, follow_redirects=True) as client:
            resp = await client.get(self.CALENDAR_URL)
            soup = BeautifulSoup(resp.text, "html.parser")

        records: list[dict] = []
        current_year = datetime.now(UTC).year
        for row in soup.find_all("tr"):
            cells = row.find_all(["td", "th"])
            texts = [c.get_text(strip=True) for c in cells]
            combined = " ".join(texts)
            dm = re.search(r"(\d{1,2})\s+([a-zéûôà]+)\s+à\s+(\d+)h(\d{0,2})", combined, re.IGNORECASE)
            if not dm:
                continue
            day, month_name, hour, minute = int(dm[1]), dm[2].lower(), int(dm[3]), int(dm[4] or 0)
            month = self.MONTHS_FR.get(month_name)
            if not month:
                continue
            year = current_year + (1 if month < datetime.now(UTC).month else 0)
            dt = datetime(year, month, day, hour, minute, tzinfo=self.TZ).astimezone(UTC)
            title = re.sub(r"\s*\d{1,2}\s+[a-zéûôà]+\s+à\s+.*$", "", texts[0], flags=re.IGNORECASE).strip()
            title = re.sub(r"^INDICATEURS CONJONCTURELS\s*", "", title, flags=re.IGNORECASE).strip()
            if len(title) < 5:
                continue
            records.append(self._record(title, dt))

        logger.info("INSEE: collected %d records", len(records))
        return records


class CSOCollector(BaseNSOCollector):
    """PxStat RESTful API — releases of the last 30 days."""

    API_BASE = "https://ws.cso.ie/public/api.restful"

    def source_code(self) -> str:
        return "cso"

    async def collect(self) -> list[dict]:
        since = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%d")
        url = f"{self.API_BASE}/PxStat.Data.Cube_API.ReadCollection/{since}/en"
        async with httpx.AsyncClient(timeout=60, headers={"Accept": "application/json", **HEADERS}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        items = data.get("link", {}).get("item", [])
        records: list[dict] = []
        seen: set[tuple[str, datetime]] = set()
        for item in items:
            title = (item.get("label") or "").strip()
            updated = (item.get("updated") or "").strip()
            href = item.get("href") or ""
            if not title or not updated:
                continue
            try:
                dt = datetime.fromisoformat(updated.replace("Z", "+00:00")).astimezone(UTC)
            except ValueError:
                continue
            if (title, dt) in seen:
                continue
            seen.add((title, dt))
            records.append(self._record(title, dt, None, href if href.startswith("http") else None))

        logger.info("CSO: collected %d records", len(records))
        return records


# ─── ForexFactory collector ────────────────────────────────────

FF_IMPACT_MAP = {
    "icon--ff-impact-red": "high",
    "icon--ff-impact-ora": "medium",
    "icon--ff-impact-yel": "low",
}
FF_ET = ZoneInfo("America/New_York")
FF_MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
             "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


class ForexFactoryCollector:
    """Monthly ForexFactory calendar HTML scraper, EUR events only."""

    BASE_URL = "https://www.forexfactory.com/calendar"
    BACKFILL_START = (2026, 6)
    LOOKBACK_MONTHS = 1
    LOOKAHEAD_MONTHS = 3

    def __init__(self, proxy_url: str | None = None, currency: str = "EUR"):
        from .config import calendar_settings

        self._proxy = (proxy_url if proxy_url is not None else calendar_settings.ff_proxy_url).strip() or None
        self._currency = currency
        if self._proxy:
            logger.info("ForexFactory requests routed via proxy %s", self._mask_credentials(self._proxy))

    @staticmethod
    def _mask_credentials(proxy_url: str) -> str:
        parts = urlsplit(proxy_url)
        if parts.username is None:
            return proxy_url
        return f"{parts.scheme}://{parts.netloc.split('@', 1)[1]}"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=30, headers=HEADERS, follow_redirects=True,
            proxy=self._proxy or None,
        )

    async def collect_initial_backfill(self) -> list[dict]:
        now = datetime.now(UTC)
        months = self._month_range(self.BACKFILL_START[0], self.BACKFILL_START[1], now.year, now.month)
        return await self._scrape_months(months)

    async def collect_routine(self) -> list[dict]:
        now = datetime.now(UTC)
        sy, sm = self._shift_month(now.year, now.month, -self.LOOKBACK_MONTHS)
        ey, em = self._shift_month(now.year, now.month, self.LOOKAHEAD_MONTHS)
        return await self._scrape_months(self._month_range(sy, sm, ey, em))

    async def collect_month(self, year: int, month: int) -> list[dict]:
        return await self._scrape_months([(year, month)])

    async def _scrape_months(self, months: list[tuple[int, int]]) -> list[dict]:
        all_records: list[dict] = []
        async with self._client() as client:
            for year, month in months:
                month_name = datetime(year, month, 1).strftime("%b").lower()
                url = f"{self.BASE_URL}?month={month_name}.{year}"
                try:
                    all_records.extend(await self._scrape_page(client, url, year))
                    logger.info("ForexFactory %s.%d: %d EUR events", month_name, year, len(all_records))
                except Exception:
                    logger.exception("ForexFactory scrape failed for %s.%d", month_name, year)
        return all_records

    async def _scrape_page(self, client: httpx.AsyncClient, url: str, year: int) -> list[dict]:
        resp = await client.get(url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        records: list[dict] = []
        current_date_str: str | None = None

        for row in soup.select("table.calendar__table tr"):
            date_cell = row.select_one("td.calendar__date")
            if date_cell:
                m = re.search(r"([A-Z][a-z]{2}\s+\d+)", date_cell.get_text(strip=True))
                if m:
                    current_date_str = m.group(1).strip()

            currency_cell = row.select_one("td.calendar__currency")
            event_cell = row.select_one("td.calendar__event")
            if not currency_cell or not event_cell:
                continue
            if currency_cell.get_text(strip=True) != self._currency:
                continue
            title = event_cell.get_text(strip=True)
            if not title:
                continue

            impact = "low"
            impact_cell = row.select_one("td.calendar__impact")
            if impact_cell:
                span = impact_cell.find("span", class_=re.compile(r"icon--ff-impact"))
                if span:
                    for cls in span.get("class") or []:
                        impact = FF_IMPACT_MAP.get(cls, impact)

            time_val = self._cell_text(row, "calendar__time")
            actual = self._cell_text(row, "calendar__actual")
            forecast = self._cell_text(row, "calendar__forecast")
            previous = self._cell_text(row, "calendar__previous")
            release_dt = self._parse_datetime(current_date_str, time_val, year)

            records.append({
                "title": title,
                "release_dt": release_dt,
                "release_dt_orig": time_val or None,
                "impact": impact,
                "currency": self._currency,
                "actual": actual or None,
                "forecast": forecast or None,
                "previous": previous or None,
                "source": "forexfactory",
                "source_uid": make_uid("forexfactory", title, release_dt.isoformat()),
            })
        return records

    @staticmethod
    def _cell_text(row, cls: str) -> str:
        cell = row.select_one(f"td.{cls}")
        return cell.get_text(strip=True) if cell else ""

    @staticmethod
    def _parse_datetime(date_str: str | None, time_str: str | None, year: int) -> datetime:
        m = re.search(r"([A-Z][a-z]{2})\s+(\d+)", date_str or "")
        if not m:
            return datetime.now(UTC)
        month = FF_MONTHS.get(m.group(1), 1)
        day = int(m.group(2))
        if not time_str or time_str in ("Tentative", "All Day", ""):
            return datetime(year, month, day, 0, 0, tzinfo=FF_ET).astimezone(UTC)
        tm = re.match(r"(\d+):(\d+)\s*(am|pm)", time_str, re.IGNORECASE)
        if tm:
            h = int(tm.group(1))
            if tm.group(3).lower() == "pm" and h != 12:
                h += 12
            elif tm.group(3).lower() == "am" and h == 12:
                h = 0
            return datetime(year, month, day, h, int(tm.group(2)), tzinfo=FF_ET).astimezone(UTC)
        return datetime(year, month, day, 0, 0, tzinfo=FF_ET).astimezone(UTC)

    @staticmethod
    def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
        total = (year * 12 + (month - 1)) + delta
        return total // 12, total % 12 + 1

    @staticmethod
    def _month_range(y1: int, m1: int, y2: int, m2: int) -> list[tuple[int, int]]:
        months: list[tuple[int, int]] = []
        y, m = y1, m1
        while (y, m) <= (y2, m2):
            months.append((y, m))
            y, m = ForexFactoryCollector._shift_month(y, m, 1)
        return months


NSO_COLLECTORS: list[type[BaseNSOCollector]] = [
    EurostatCollector, IstatCollector, INECollector,
    DestatisCollector, INSEECollector, CSOCollector,
]
