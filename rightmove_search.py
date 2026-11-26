"""RightMove property search script using Crawlee + Camoufox.

Stealth batch-export of property listings by station radius to CSV.
Uses a fingerprint-resistant browser (Camoufox) with human-like delays
and scrolling to avoid bot detection.

Setup
-----
1. Install Python dependencies:

       pip install -r requirements.txt

2. Download the Camoufox browser binary (required, pip alone is NOT enough):

       camoufox fetch

3. Copy ``search_config.example.yaml`` to ``search_config.yaml`` (same
   directory as this script) and edit it. See "Configuration" below.

4. Run the script **from this directory**:

       cd property_search
       python rightmove_search.py

   Results are written as timestamped CSVs into the configured output
   directory (default: ``results/``).

   IMPORTANT: Crawlee creates a ``storage/`` folder for its runtime cache
   (request queues, key-value stores) in the **current working directory**,
   not relative to the script. Always run from the ``property_search/``
   directory so the cache stays contained here.

Configuration (search_config.yaml)
----------------------------------
The YAML file must contain three top-level keys:

    stations:
      - name: "Raynes Park"          # Human-readable label (used in CSV "Area" column)
        id: "STATION^7607"           # RightMove location identifier (see below)

    filters:
      radius: 0.5                   # Miles from station
      min_price: 800000
      max_price: 1250000
      property_types:               # Any of: detached, semi-detached, terraced, flat
        - detached
        - semi-detached
      max_days_since_added: 3       # Only listings added in the last N days
      min_bedrooms: 2
      max_bedrooms: 5

    output:
      directory: "results"          # CSV files are saved here with timestamps
      deduplicate: true             # Remove duplicate property IDs across stations

How to find station / location IDs:
    Query Rightmove's location autocomplete endpoint:

        https://los.rightmove.co.uk/typeahead?query=raynes%20park&limit=10&exclude=STREET

    Each match has ``type`` and ``id``; the location identifier is
    ``<type>^<id>``, e.g. ``STATION^7607`` or ``REGION^70418``.

Output
------
CSV columns: Address, Price, Type, Beds, Area, Date Added, URL

Each run creates a new timestamped file in the ``results/`` directory
(e.g. ``results/rightmove_20260306_143012.csv``). Previous results are
never overwritten.

Known Pitfalls & Caveats
-------------------------
1. **Camoufox binary required** -- ``pip install camoufox`` only installs the
   Python wrapper. You MUST also run ``camoufox fetch`` to download the
   stealth Firefox binary, otherwise the script crashes at startup.

2. **Station IDs are opaque** -- RightMove uses internal location identifiers
   (e.g. ``STATION^7607``). There is no public directory; look them up via
   the autocomplete endpoint (see instructions above).

3. **Fragile selectors** -- Extraction relies on ``data-testid`` attributes
   (``propertyCard-``, ``property-address``, ``property-price``, etc.).
   If RightMove redesigns their frontend, extraction silently returns
   incomplete or empty data rather than raising errors.

4. **No CAPTCHA / block detection** -- If RightMove serves a CAPTCHA or
   block page, the script does not detect it. It will silently return zero
   properties for that station.

5. **Buy-only** -- The search channel is hardcoded to ``BUY`` (line with
   ``"channel": "BUY"``). Rental searches require a code change.

6. **Results accumulate** -- Each run creates a new timestamped CSV in the
   ``results/`` directory. Old files are never cleaned up automatically;
   remove them manually if disk space is a concern.

7. **Python 3.15 unsupported** -- Camoufox's fingerprint generator (``fpgen``)
   fails on Python 3.15's stricter ``base64.b85decode`` with "Incomplete
   Base85 group". Use Python 3.11-3.14.

8. **Silent per-card failures** -- If a single property card fails to parse,
   the error is logged as a warning and that card is skipped. A systematic
   selector change could cause all cards to be silently skipped.
"""

from __future__ import annotations

import asyncio
import argparse
import csv
import json
import logging
import math
import random
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Optional, TypeVar
from urllib.parse import quote

import yaml
from camoufox import AsyncNewBrowser
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import Locator
from typing_extensions import override

from crawlee.browsers import (
    BrowserPool,
    PlaywrightBrowserController,
    PlaywrightBrowserPlugin,
)
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("rightmove")

CONFIG_PATH = Path(__file__).parent / "search_config.yaml"
PAGE_ACTION_RETRIES = 4
RIGHTMOVE_MAX_DAYS_SINCE_ADDED_URL_LIMIT = 14
INVALID_LOCATION_MARKERS = (
    "we couldn’t find the place you were looking for",
    "we couldn't find the place you were looking for",
    "this isn't the place you were looking for",
)
ZERO_RESULTS_MARKERS = (
    "there are currently no properties that meet your search criteria",
    "no properties match your search criteria",
)
TRANSIENT_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "net::err",
    "connection reset",
    "connection aborted",
    "connection closed",
    "socket hang up",
    "temporarily unavailable",
    "target page, context or browser has been closed",
    "target closed",
    "browser has been closed",
    "econnreset",
    "econnrefused",
    "enotfound",
    "503",
    "504",
    "502",
)
T = TypeVar("T")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict:
    """Load and validate search config from YAML."""
    with open(path) as f:
        config = yaml.safe_load(f)

    for key in ("stations", "filters", "output"):
        if key not in config:
            raise ValueError(f"Missing required config key: {key}")

    if not config["stations"]:
        raise ValueError("At least one station is required")

    filters = config["filters"]
    max_days = filters.get("max_days_since_added")
    if max_days not in (None, ""):
        try:
            filters["max_days_since_added"] = int(max_days)
        except (TypeError, ValueError) as exc:
            raise ValueError("filters.max_days_since_added must be an integer") from exc

    return config


def resolve_url_max_days_since_added(filters: dict) -> int | str:
    """Return the Rightmove-supported URL value for maxDaysSinceAdded.

    Rightmove rejects values above 14 with a page-not-found style response.
    For broader windows we omit the URL parameter and apply the filter locally.
    """
    max_days = filters.get("max_days_since_added", "")
    if max_days in (None, ""):
        return ""
    if max_days <= RIGHTMOVE_MAX_DAYS_SINCE_ADDED_URL_LIMIT:
        return max_days
    return ""


def build_search_url(station: dict, filters: dict) -> str:
    """Build a RightMove search URL for a station with filters."""
    location_id = quote(station["id"], safe="")
    property_types = "%2C".join(filters.get("property_types", []))

    params = {
        "locationIdentifier": location_id,
        "radius": filters.get("radius", 0.5),
        "minPrice": filters.get("min_price", ""),
        "maxPrice": filters.get("max_price", ""),
        "minBedrooms": filters.get("min_bedrooms", ""),
        "maxBedrooms": filters.get("max_bedrooms", ""),
        "propertyTypes": property_types,
        "maxDaysSinceAdded": resolve_url_max_days_since_added(filters),
        "channel": "BUY",
    }

    query = "&".join(f"{k}={v}" for k, v in params.items() if v != "")
    return f"https://www.rightmove.co.uk/property-for-sale/find.html?{query}"


def parse_listing_date(date_added: str, *, now: datetime | None = None) -> datetime | None:
    """Parse Rightmove's listing age text into a datetime.

    Supported formats include "Added today", "Reduced yesterday", and
    "Added on 31/03/2026".
    """
    if not date_added:
        return None

    value = date_added.strip().lower()
    current = now or datetime.now()

    if "today" in value:
        return current
    if "yesterday" in value:
        return current - timedelta(days=1)

    match = re.search(r"on\s+(\d{2}/\d{2}/\d{4})", value)
    if not match:
        return None

    try:
        return datetime.strptime(match.group(1), "%d/%m/%Y")
    except ValueError:
        return None


def apply_local_max_days_filter(properties: list[dict], filters: dict) -> tuple[list[dict], int]:
    """Apply a local max-days filter when Rightmove cannot enforce it in the URL."""
    max_days = filters.get("max_days_since_added")
    if max_days in (None, "") or max_days <= RIGHTMOVE_MAX_DAYS_SINCE_ADDED_URL_LIMIT:
        return properties, 0

    cutoff = datetime.now() - timedelta(days=max_days)
    filtered: list[dict] = []
    skipped_unknown = 0

    for prop in properties:
        parsed = parse_listing_date(prop.get("date_added", ""))
        if parsed is None:
            skipped_unknown += 1
            continue
        if parsed >= cutoff:
            filtered.append(prop)

    log.info(
        "Applied local %s-day filter for %s properties, kept %s, skipped %s with unknown dates",
        max_days,
        len(properties),
        len(filtered),
        skipped_unknown,
    )
    return filtered, skipped_unknown


# ---------------------------------------------------------------------------
# Browser plugin
# ---------------------------------------------------------------------------

class CamoufoxPlugin(PlaywrightBrowserPlugin):
    """Stealth Firefox browser plugin using Camoufox."""

    @override
    async def new_browser(self) -> PlaywrightBrowserController:
        if not self._playwright:
            raise RuntimeError("Playwright not initialized.")

        # Remove keys that conflict with Camoufox's own parameters
        launch_opts = dict(self._browser_launch_options)
        launch_opts.pop("headless", None)

        return PlaywrightBrowserController(
            browser=await AsyncNewBrowser(
                self._playwright,
                headless=True,
                humanize=True,
                **launch_opts,
            ),
            max_open_pages_per_browser=1,
            header_generator=None,  # Camoufox handles its own headers
        )


# ---------------------------------------------------------------------------
# Human-like behaviour helpers
# ---------------------------------------------------------------------------

async def human_delay(min_s: float = 3.0, max_s: float = 8.0) -> None:
    """Sleep for a random human-like duration (gaussian distribution)."""
    mean = (min_s + max_s) / 2
    std = (max_s - min_s) / 4
    delay = max(min_s, min(max_s, random.gauss(mean, std)))
    log.debug(f"Sleeping {delay:.1f}s")
    await asyncio.sleep(delay)


async def human_scroll(page) -> None:
    """Simulate human-like scrolling down the page."""
    viewport_height = (
        page.viewport_size.get("height", 800) if page.viewport_size else 800
    )
    scroll_distance = random.randint(200, viewport_height)
    steps = random.randint(3, 6)
    for _ in range(steps):
        await page.mouse.wheel(0, scroll_distance)
        await asyncio.sleep(random.uniform(0.3, 1.0))


async def accept_cookies(page) -> None:
    """Dismiss the cookie consent dialog if present."""
    try:
        accept_btn = await page.query_selector('button:has-text("Accept all")')
        if accept_btn:
            log.info("Accepting cookies...")
            await accept_btn.click()
            await asyncio.sleep(2)
    except Exception:
        pass


def is_transient_error(error: BaseException) -> bool:
    """Return True when *error* or one of its causes looks transient."""
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, (asyncio.TimeoutError, PlaywrightTimeoutError)):
            return True
        message = str(current).lower()
        if any(marker in message for marker in TRANSIENT_ERROR_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


async def retry_transient_action(
    action: Callable[[], Awaitable[T]],
    *,
    attempts: int = PAGE_ACTION_RETRIES,
    label: str,
    station_name: str,
    on_retry: Callable[[int, BaseException], Awaitable[None]] | None = None,
) -> T:
    """Retry a page action when it fails with a transient/load-related error."""
    last_error: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await action()
        except Exception as exc:
            if not is_transient_error(exc) or attempt >= attempts:
                raise
            last_error = exc
            log.warning(
                "%s: transient failure during %s, retrying (%s/%s): %s",
                station_name,
                label,
                attempt,
                attempts,
                exc,
            )
            if on_retry is not None:
                await on_retry(attempt, exc)
            await human_delay(2.0 + attempt, 4.0 + attempt)

    raise RuntimeError(f"{station_name}: failed during {label}") from last_error


async def _safe_inner_text(locator: Locator) -> str:
    try:
        if await locator.count():
            return (await locator.first.inner_text()).strip()
    except Exception:
        pass
    return ""


async def detect_page_state(page) -> str:
    """Classify the current Rightmove page state.

    Returns one of: ``results``, ``zero_results``, ``invalid_location``.
    """
    title = (await page.title()).strip().lower()
    body_text = (await _safe_inner_text(page.locator("body"))).lower()

    if any(marker in title or marker in body_text for marker in INVALID_LOCATION_MARKERS):
        return "invalid_location"

    if any(marker in body_text for marker in ZERO_RESULTS_MARKERS):
        return "zero_results"

    if await page.locator('[data-testid^="propertyCard-"]:not([data-testid*="vrt"])').count():
        return "results"

    search_description = await _safe_inner_text(page.locator('[data-testid="search-description"]'))
    if search_description:
        if re.search(r"\b0\s+results?\b", search_description, re.IGNORECASE):
            return "zero_results"
        if re.search(r"\b[\d,]+\s+results?\b", search_description, re.IGNORECASE):
            return "results"

    return "results"


async def wait_for_results(page) -> str:
    """Wait for a search page to settle and classify the outcome."""
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1500)

    selectors = [
        '[data-testid="results-list"]',
        '[data-testid^="propertyCard-"]',
        '[data-testid="search-description"]',
        'text="There are currently no properties that meet your search criteria"',
        'text="This isn\'t the place you were looking for!"',
        'text="We couldn\'t find the place you were looking for"',
    ]

    for selector in selectors:
        try:
            await page.wait_for_selector(selector, timeout=5000)
            break
        except PlaywrightTimeoutError:
            continue

    return await detect_page_state(page)


# ---------------------------------------------------------------------------
# Property extraction
# ---------------------------------------------------------------------------

def extract_property_id(url: str) -> Optional[str]:
    """Extract numeric property ID from a RightMove URL."""
    match = re.search(r"/properties/(\d+)", url)
    return match.group(1) if match else None


async def extract_properties_from_page(page) -> list[dict]:
    """Extract property listings from the current search results page."""
    properties = []

    # Property cards use data-testid="propertyCard-0", "propertyCard-1", etc.
    # Exclude the "vrt" (virtual) duplicates
    cards = await page.query_selector_all(
        '[data-testid^="propertyCard-"]:not([data-testid*="vrt"])'
    )

    for card in cards:
        try:
            # Address
            addr_el = await card.query_selector('[data-testid="property-address"]')
            address = (await addr_el.inner_text()).strip() if addr_el else "Unknown"

            # Price
            price_el = await card.query_selector('[data-testid="property-price"]')
            price = (await price_el.inner_text()).strip() if price_el else "Unknown"

            # Property type and beds from property-information
            info_el = await card.query_selector('[data-testid="property-information"]')
            prop_type = ""
            beds = ""
            if info_el:
                info_text = await info_el.inner_text()
                # Format is typically: "End of Terrace\n3\n2\n0.22 miles..."
                # or: "Semi-Detached\n4\n2\n0.46 miles..."
                lines = [l.strip() for l in info_text.split("\n") if l.strip()]
                if lines:
                    prop_type = lines[0]
                if len(lines) > 1:
                    beds_match = re.match(r"(\d+)", lines[1])
                    beds = beds_match.group(1) if beds_match else ""

            # Date added / reduced — in the marketed-by text
            card_text = await card.inner_text()
            date_match = re.search(
                r"((?:Added|Reduced)\s+(?:on\s+\d{2}/\d{2}/\d{4}|today|yesterday))",
                card_text,
                re.IGNORECASE,
            )
            date_added = date_match.group(1) if date_match else ""

            # Property URL — first link containing /properties/
            link_el = await card.query_selector("a[href*='/properties/']")
            href = (await link_el.get_attribute("href")) if link_el else ""
            if href and not href.startswith("http"):
                href = f"https://www.rightmove.co.uk{href}"
            # Clean up anchor fragments
            href = href.split("#")[0] if href else ""

            prop_id = extract_property_id(href) if href else None

            properties.append({
                "address": address,
                "price": price,
                "type": prop_type,
                "beds": beds,
                "date_added": date_added,
                "url": href,
                "property_id": prop_id,
            })
        except Exception as e:
            log.warning(f"Failed to extract property card: {e}")
            continue

    return properties


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

async def get_total_results(page) -> int:
    """Get total result count from the search results page."""
    try:
        # Try the search description which contains "X properties"
        desc_el = await page.query_selector('[data-testid="search-description"]')
        if desc_el:
            text = await desc_el.inner_text()
            match = re.search(r"([\d,]+)\s*propert", text, re.IGNORECASE)
            if match:
                return int(match.group(1).replace(",", ""))
        # Fallback: count cards on page
        cards = await page.query_selector_all(
            '[data-testid^="propertyCard-"]:not([data-testid*="vrt"])'
        )
        return len(cards)
    except Exception:
        pass
    return 0


async def has_next_page(page) -> bool:
    """Check if there is a next page of results."""
    next_btn = await page.query_selector('[data-testid="nextPage"]')
    if not next_btn:
        return False
    is_disabled = await next_btn.get_attribute("disabled")
    return is_disabled is None


async def click_next_page(page) -> bool:
    """Click the next page button. Returns True if successful."""
    next_btn = await page.query_selector('[data-testid="nextPage"]')
    if not next_btn:
        return False

    is_disabled = await next_btn.get_attribute("disabled")
    if is_disabled is not None:
        return False

    await next_btn.scroll_into_view_if_needed()
    await human_delay(1.5, 3.0)
    await next_btn.click(timeout=20000)
    await wait_for_results(page)
    await human_delay(2.0, 4.0)
    return True


async def reload_results_page(page, station_name: str, reason: str) -> None:
    """Reload the current results page after a transient failure."""
    log.info("%s: reloading page after %s", station_name, reason)
    await page.reload(wait_until="domcontentloaded", timeout=30000)
    await wait_for_results(page)
    await accept_cookies(page)
    await human_delay(2.0, 4.0)


# ---------------------------------------------------------------------------
# Deduplication & CSV export
# ---------------------------------------------------------------------------

def deduplicate_properties(properties: list[dict]) -> list[dict]:
    """Remove duplicate properties by property ID."""
    seen: set[str] = set()
    unique = []
    for prop in properties:
        pid = prop.get("property_id")
        if pid and pid in seen:
            continue
        if pid:
            seen.add(pid)
        unique.append(prop)
    return unique


def load_previous_results(output_dir: str) -> dict[str, dict]:
    """Load properties from the most recent CSV in *output_dir*."""
    results_dir = Path(output_dir)
    if not results_dir.exists():
        return {}

    csv_files = sorted(results_dir.glob("rightmove_*.csv"))
    if not csv_files:
        return {}

    latest = csv_files[-1]
    log.info(f"Loading previous results from {latest}")

    prev: dict[str, dict] = {}
    with open(latest, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pid = extract_property_id(row.get("URL", ""))
            if pid:
                prev[pid] = dict(row)
    log.info(f"Loaded {len(prev)} previous properties")
    return prev


def remove_unchanged(
    properties: list[dict], previous: dict[str, dict]
) -> list[dict]:
    """Filter out properties that are unchanged since the last run.

    A property is considered "unchanged" when its property_id was seen
    previously AND its price is identical. Any price change means the
    property is kept.
    """
    if not previous:
        return properties

    new_or_updated: list[dict] = []
    skipped = 0

    for prop in properties:
        pid = prop.get("property_id")
        if not pid or pid not in previous:
            new_or_updated.append(prop)
            continue

        old = previous[pid]
        price_changed = prop.get("price", "") != old.get("Price", "")

        if price_changed:
            new_or_updated.append(prop)
        else:
            skipped += 1

    log.info(
        f"Cross-run dedup: {skipped} unchanged properties removed, "
        f"{len(new_or_updated)} new/updated kept"
    )
    return new_or_updated


def export_to_csv(properties: list[dict], output_dir: str) -> Path:
    """Export properties to a timestamped CSV file inside *output_dir*.

    The file is named ``rightmove_YYYYMMDD_HHMMSS.csv`` so that each run
    is preserved and previous results are never overwritten.
    """
    results_dir = Path(output_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = results_dir / f"rightmove_{timestamp}.csv"

    fieldnames = ["Address", "Price", "Type", "Beds", "Area", "Date Added", "URL"]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for prop in properties:
            writer.writerow({
                "Address": prop["address"],
                "Price": prop["price"],
                "Type": prop["type"],
                "Beds": prop["beds"],
                "Area": prop.get("area", ""),
                "Date Added": prop["date_added"],
                "URL": prop["url"],
            })

    log.info(f"Exported {len(properties)} properties to {output_path}")
    return output_path


def append_properties_to_spool(spool_path: Path, properties: list[dict]) -> None:
    """Append extracted properties to a JSONL spool file."""
    if not properties:
        return

    with spool_path.open("a", encoding="utf-8") as handle:
        for prop in properties:
            handle.write(json.dumps(prop, ensure_ascii=False) + "\n")


def finalize_spooled_properties(
    spool_path: Path,
    *,
    output_dir: str,
    deduplicate: bool,
) -> tuple[Path | None, int]:
    """Stream dedupe/filtering from the spool file into the final CSV."""
    if not spool_path.exists():
        return None, 0

    previous = load_previous_results(output_dir)
    results_dir = Path(output_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = results_dir / f"rightmove_{timestamp}.csv"
    fieldnames = ["Address", "Price", "Type", "Beds", "Area", "Date Added", "URL"]

    seen: set[str] = set()
    exported = 0
    skipped_duplicates = 0
    skipped_unchanged = 0

    with spool_path.open("r", encoding="utf-8") as src, output_path.open(
        "w", newline="", encoding="utf-8"
    ) as dst:
        writer = csv.DictWriter(dst, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()

        for line in src:
            line = line.strip()
            if not line:
                continue
            prop = json.loads(line)
            pid = prop.get("property_id")

            if deduplicate and pid and pid in seen:
                skipped_duplicates += 1
                continue
            if pid:
                seen.add(pid)

            if pid and pid in previous and prop.get("price", "") == previous[pid].get("Price", ""):
                skipped_unchanged += 1
                continue

            writer.writerow({
                "Address": prop["address"],
                "Price": prop["price"],
                "Type": prop["type"],
                "Beds": prop["beds"],
                "Area": prop.get("area", ""),
                "Date Added": prop["date_added"],
                "URL": prop["url"],
            })
            exported += 1

    if skipped_duplicates:
        log.info("Run dedup: %s duplicate properties removed", skipped_duplicates)
    if skipped_unchanged:
        log.info(
            "Cross-run dedup: %s unchanged properties removed, %s new/updated kept",
            skipped_unchanged,
            exported,
        )

    if exported == 0:
        output_path.unlink(missing_ok=True)
        return None, 0

    log.info("Exported %s properties to %s", exported, output_path)
    return output_path, exported


# ---------------------------------------------------------------------------
# Main crawler
# ---------------------------------------------------------------------------

async def run_search(config_path: Path = CONFIG_PATH) -> bool:
    """Main entry point: load config, crawl stations, export CSV.

    Returns False when no station could be searched.
    """
    config = load_config(config_path)
    stations = config["stations"]
    filters = config["filters"]
    output_cfg = config["output"]

    output_dir = output_cfg.get("directory", "results")
    deduplicate_output = output_cfg.get("deduplicate", True)

    log.info(f"Starting search across {len(stations)} stations")

    local_days_filter_active = filters.get("max_days_since_added") not in (None, "") and filters.get("max_days_since_added") > RIGHTMOVE_MAX_DAYS_SINCE_ADDED_URL_LIMIT
    if local_days_filter_active:
        log.info(
            "max_days_since_added=%s exceeds Rightmove URL limit (%s), so older results will be crawled and filtered locally",
            filters.get("max_days_since_added"),
            RIGHTMOVE_MAX_DAYS_SINCE_ADDED_URL_LIMIT,
        )

    search_started_at = time.perf_counter()
    stations_searched = 0
    stations_failed = 0
    spool_dir = Path(tempfile.mkdtemp(prefix="rightmove_spool_"))
    spool_path = spool_dir / "properties.jsonl"

    # Homepage first for session warming, then one URL per station
    urls_to_crawl = ["https://www.rightmove.co.uk/"]
    station_urls: dict[str, str] = {}
    for station in stations:
        url = build_search_url(station, filters)
        urls_to_crawl.append(url)
        station_urls[url] = station["name"]

    crawler = PlaywrightCrawler(
        browser_pool=BrowserPool(plugins=[CamoufoxPlugin()]),
        max_requests_per_crawl=len(urls_to_crawl) * 10,  # room for pagination
        request_handler_timeout=timedelta(seconds=180),
        max_crawl_depth=0,
        max_request_retries=3,
    )

    @crawler.router.default_handler
    async def request_handler(context: PlaywrightCrawlingContext) -> None:
        nonlocal stations_searched, stations_failed

        url = context.request.url
        page = context.page

        # --- Homepage: warm session + accept cookies ---
        if url.rstrip("/") == "https://www.rightmove.co.uk":
            log.info("Warming session on homepage...")
            await page.wait_for_load_state("networkidle")
            await human_delay(2.0, 5.0)
            await accept_cookies(page)
            await human_scroll(page)
            await human_delay(1.0, 3.0)
            return

        # --- Search results page ---
        base_url = url.split("&index=")[0]
        station_name = station_urls.get(base_url, "Unknown")
        station_index = next((idx for idx, station in enumerate(stations, start=1) if station["name"] == station_name), None)
        station_label = f"[{station_index}/{len(stations)}] {station_name}" if station_index else station_name
        station_started_at = time.perf_counter()
        station_total_kept = 0
        station_total_raw = 0
        station_total_unknown_dates = 0
        log.info("Searching %s", station_label)

        try:
            # Wait for JS-rendered results
            page_state = await retry_transient_action(
                lambda: wait_for_results(page),
                label="initial results load",
                station_name=station_name,
                on_retry=lambda attempt, exc: reload_results_page(
                    page,
                    station_name,
                    f"initial results load failure ({type(exc).__name__})",
                ),
            )
            await human_delay(2.0, 5.0)

            # Accept cookies again if the dialog reappears
            await accept_cookies(page)

            if page_state == "invalid_location":
                log.error("%s: Rightmove location page is invalid or no longer exists", station_name)
                stations_failed += 1
                return

            total = await get_total_results(page)
            if page_state == "zero_results":
                log.info("%s: valid search page with zero matching properties", station_name)
                stations_searched += 1
                return

            estimated_pages = max(1, math.ceil(total / 24)) if total else 1
            log.info("%s: %s total results, ~%s page(s) expected", station_label, total, estimated_pages)

            # Paginate through all results
            page_num = 1
            empty_pages_seen = 0
            while True:
                await human_scroll(page)
                await human_delay(1.0, 2.0)

                page_extract_started_at = time.perf_counter()
                props = await retry_transient_action(
                    lambda: extract_properties_from_page(page),
                    label=f"page {page_num} extraction",
                    station_name=station_name,
                    on_retry=lambda attempt, exc: reload_results_page(
                        page,
                        station_name,
                        f"page {page_num} extraction failure ({type(exc).__name__})",
                    ),
                )

                if total > 0 and not props:
                    empty_pages_seen += 1
                    if empty_pages_seen < 2:
                        await reload_results_page(
                            page,
                            station_name,
                            f"empty page {page_num} despite {total} reported results",
                        )
                        continue
                    log.warning(
                        "%s page %s rendered with no cards after reload, stopping station early",
                        station_name,
                        page_num,
                    )
                    break

                empty_pages_seen = 0
                raw_count = len(props)
                for p in props:
                    p["area"] = station_name
                props, unknown_dates = apply_local_max_days_filter(props, filters)
                kept_count = len(props)
                station_total_raw += raw_count
                station_total_kept += kept_count
                station_total_unknown_dates += unknown_dates
                append_properties_to_spool(spool_path, props)

                page_elapsed = time.perf_counter() - page_extract_started_at
                log.info(
                    "%s page %s/%s: kept %s of %s cards in %.1fs (station kept=%s, raw=%s, unknown_dates=%s)",
                    station_label,
                    page_num,
                    estimated_pages,
                    kept_count,
                    raw_count,
                    page_elapsed,
                    station_total_kept,
                    station_total_raw,
                    station_total_unknown_dates,
                )

                if await has_next_page(page):
                    navigation_started_at = time.perf_counter()
                    moved = await retry_transient_action(
                        lambda: click_next_page(page),
                        label=f"page {page_num + 1} navigation",
                        station_name=station_name,
                        on_retry=lambda attempt, exc: reload_results_page(
                            page,
                            station_name,
                            f"page {page_num + 1} navigation failure ({type(exc).__name__})",
                        ),
                    )
                    if moved:
                        log.info(
                            "%s: advanced to page %s in %.1fs",
                            station_label,
                            page_num + 1,
                            time.perf_counter() - navigation_started_at,
                        )
                        page_num += 1
                        await human_delay(3.0, 6.0)
                        continue
                break

            stations_searched += 1
            log.info(
                "%s done in %.1fs, kept %s properties from %s raw cards across %s page(s)",
                station_label,
                time.perf_counter() - station_started_at,
                station_total_kept,
                station_total_raw,
                page_num,
            )

        except Exception as e:
            if is_transient_error(e):
                retry_number = context.request.retry_count + 1
                retry_limit = context.request.max_retries or 3
                log.warning(
                    "%s: transient station failure, handing back to crawler retry (%s/%s)",
                    station_name,
                    retry_number,
                    retry_limit,
                )
                raise

            log.error(f"Failed to search {station_name}: {e}")
            stations_failed += 1

    @crawler.failed_request_handler
    async def failed_request_handler(context: PlaywrightCrawlingContext, error: Exception) -> None:
        nonlocal stations_failed

        url = context.request.url
        if url.rstrip("/") == "https://www.rightmove.co.uk":
            log.error(f"Homepage warm-up failed after retries: {error}")
            return

        station_name = station_urls.get(url.split("&index=")[0], "Unknown")
        stations_failed += 1
        log.error(f"Failed to search {station_name} after retries: {error}")

    # Run — URLs processed sequentially (single browser tab)
    await crawler.run(urls_to_crawl)

    output_path, exported_count = finalize_spooled_properties(
        spool_path,
        output_dir=output_dir,
        deduplicate=deduplicate_output,
    )

    if output_path is None:
        log.warning("No properties found")

    log.info(
        f"Done in {time.perf_counter() - search_started_at:.1f}s. Searched {stations_searched}/{len(stations)} stations, "
        f"{stations_failed} failed, {exported_count} unique properties."
    )

    spool_path.unlink(missing_ok=True)
    spool_dir.rmdir()
    return stations_searched > 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Search Rightmove by station and export CSV results.")
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help=f"Path to YAML config file (default: {CONFIG_PATH.name})",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level for progress visibility (default: INFO)",
    )
    args = parser.parse_args()
    if not args.config.exists():
        parser.error(f"{args.config} not found; copy search_config.example.yaml to {CONFIG_PATH.name} and edit it")
    log.setLevel(getattr(logging, args.log_level))
    sys.exit(0 if asyncio.run(run_search(args.config)) else 1)
