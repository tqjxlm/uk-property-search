#!/usr/bin/env python3
"""Generate an HTML report for a Rightmove CSV result set.

This script reuses the existing CSV format produced by ``rightmove_search.py`` and
fetches extra details (especially images) directly from each Rightmove listing
page as needed. A JSON cache is stored next to the CSV so repeated runs do not
need to re-fetch unchanged listings.

Example:
    python generate_report.py results/rightmove_20260320_073851.csv
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
from html import unescape
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an HTML page for one Rightmove CSV result set."
    )
    parser.add_argument("csv_path", type=Path, help="CSV file from results/")
    parser.add_argument(
        "--output",
        type=Path,
        help="Output HTML path (default: alongside CSV, same basename + .html)",
    )
    parser.add_argument(
        "--max-images",
        type=int,
        default=6,
        help="Maximum number of images to show per property (default: 6)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore the JSON cache and refetch all listing pages.",
    )
    return parser.parse_args()


def load_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def property_id_from_url(url: str) -> str:
    match = re.search(r"/properties/(\d+)", url or "")
    return match.group(1) if match else url


def cache_path_for(csv_path: Path) -> Path:
    return csv_path.with_suffix(".enriched.json")


def output_path_for(csv_path: Path, explicit: Path | None) -> Path:
    if explicit is None:
        return csv_path.with_suffix(".html")
    return explicit


def load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_cache(path: Path, cache: dict[str, Any]) -> None:
    path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def extract_balanced_json(text: str, start_index: int) -> str:
    decoder = json.JSONDecoder()
    obj, end = decoder.raw_decode(text[start_index:])
    return json.dumps(obj)


def decode_indexed_page_model(encoded: list[Any], root_index: int = 0) -> Any:
    memo: dict[int, Any] = {}

    def resolve_index(index: int) -> Any:
        if index in memo:
            return memo[index]

        value = encoded[index]
        if isinstance(value, dict):
            resolved: dict[str, Any] = {}
            memo[index] = resolved
            for key, inner in value.items():
                resolved[key] = resolve_value(inner)
            return resolved

        if isinstance(value, list):
            resolved_list: list[Any] = []
            memo[index] = resolved_list
            resolved_list.extend(resolve_value(inner) for inner in value)
            return resolved_list

        memo[index] = value
        return value

    def resolve_value(value: Any) -> Any:
        if isinstance(value, int) and 0 <= value < len(encoded):
            return resolve_index(value)
        return value

    return resolve_index(root_index)


def extract_page_model(html_text: str) -> dict[str, Any]:
    markers = ["window.PAGE_MODEL = ", "window.__PAGE_MODEL = "]

    for marker in markers:
        start = html_text.find(marker)
        if start == -1:
            continue

        start += len(marker)
        json_text = extract_balanced_json(html_text, start)
        raw_model = json.loads(json_text)

        if marker == "window.__PAGE_MODEL = ":
            encoding = raw_model.get("encoding")
            data = raw_model.get("data")
            if encoding != "on" or not isinstance(data, str):
                raise ValueError("window.__PAGE_MODEL had unexpected structure")
            encoded = json.loads(data)
            if not isinstance(encoded, list):
                raise ValueError("window.__PAGE_MODEL data was not a list")
            decoded = decode_indexed_page_model(encoded)
            if not isinstance(decoded, dict):
                raise ValueError("window.__PAGE_MODEL did not decode to an object")
            return decoded

        if not isinstance(raw_model, dict):
            raise ValueError("window.PAGE_MODEL did not decode to an object")
        return raw_model

    raise ValueError("No supported page model found")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"(?i)<p[^>]*>", "", text)
    text = re.sub(r"(?i)</?(strong|b|em|i|span)[^>]*>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"\r", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def pick_first(*values: Any) -> str:
    for value in values:
        cleaned = clean_text(value)
        if cleaned:
            return cleaned
    return ""


def clean_visible_price(price_text: Any) -> str:
    text = clean_text(price_text)
    if not text:
        return ""
    text = re.sub(r"(?im)^\s*guide\s+price\s*$", "", text)
    text = re.sub(r"(?i)\bguide\s+price\b\s*(?=[:£$€])", "", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def clean_visible_summary(summary_text: Any) -> str:
    text = clean_text(summary_text)
    if not text:
        return ""
    text = re.sub(r"(?im)^\s*guide\s+price\s*$", "", text)
    text = re.sub(r"(?i)\bguide\s+price\b\s*(?=[:£$€])", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def make_property_anchor(row: dict[str, Any], index: int) -> str:
    raw = property_id_from_url(str(row.get("URL", "")))
    if raw and raw.isdigit():
        return f"property-{raw}"
    slug_source = clean_text(row.get("Address") or f"property-{index}").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug_source).strip("-") or f"property-{index}"
    return f"property-{slug}-{index}"


def extract_enrichment(listing_url: str, max_images: int) -> dict[str, Any]:
    page_html = fetch_text(listing_url)
    model = extract_page_model(page_html)
    data = model.get("propertyData", {})

    images: list[str] = []
    for item in data.get("images", [])[:max_images]:
        if not isinstance(item, dict):
            continue
        resized = item.get("resizedImageUrls") or {}
        image_url = (
            resized.get("size656x437")
            or resized.get("size476x317")
            or item.get("url")
            or ""
        )
        if image_url:
            images.append(image_url)

    floorplans: list[dict[str, str]] = []
    for index, item in enumerate(data.get("floorplans", []), start=1):
        if not isinstance(item, dict):
            continue
        resized = item.get("resizedFloorplanUrls") or {}
        floorplan_image = (
            resized.get("size296x197")
            or item.get("url")
            or ""
        )
        floorplan_link = item.get("url") or floorplan_image
        if floorplan_image or floorplan_link:
            floorplans.append(
                {
                    "image_url": floorplan_image,
                    "url": floorplan_link,
                    "caption": pick_first(item.get("caption"), f"Floorplan {index}"),
                }
            )

    map_images = data.get("staticMapImgUrls", {}) if isinstance(data.get("staticMapImgUrls"), dict) else {}
    location = data.get("location", {}) if isinstance(data.get("location"), dict) else {}
    latitude = location.get("latitude")
    longitude = location.get("longitude")
    map_link = ""
    if latitude is not None and longitude is not None:
        map_link = f"https://www.google.com/maps?q={latitude},{longitude}"

    key_features = [
        clean_text(feature)
        for feature in data.get("keyFeatures", [])
        if clean_text(feature)
    ]

    customer = data.get("customer", {}) if isinstance(data.get("customer"), dict) else {}
    text_block = data.get("text", {}) if isinstance(data.get("text"), dict) else {}
    prices = data.get("prices", {}) if isinstance(data.get("prices"), dict) else {}
    address = data.get("address", {}) if isinstance(address := data.get("address"), dict) else {}
    bedrooms = data.get("bedrooms")
    bathrooms = data.get("bathrooms")

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "listing_title": clean_text(text_block.get("pageTitle")),
        "summary": clean_text(text_block.get("description")),
        "key_features": key_features,
        "images": images,
        "floorplans": floorplans,
        "map": {
            "image_url": pick_first(
                map_images.get("staticMapImgUrlDesktopLarge"),
                map_images.get("staticMapImgUrlDesktopSmall"),
                map_images.get("staticMapImgUrlTablet"),
                map_images.get("staticMapImgUrlMobile"),
            ),
            "url": map_link,
            "latitude": clean_text(latitude),
            "longitude": clean_text(longitude),
        },
        "display_address": clean_text(address.get("displayAddress")),
        "bedrooms": clean_text(bedrooms),
        "bathrooms": clean_text(bathrooms),
        "property_subtype": clean_text(data.get("propertySubType")),
        "added_or_reduced": clean_text(prices.get("displayPrices")[0].get("subtitle"))
        if isinstance(prices.get("displayPrices"), list) and prices.get("displayPrices")
        and isinstance(prices.get("displayPrices")[0], dict)
        else "",
        "agent_name": clean_text(customer.get("branchDisplayName") or customer.get("companyName")),
    }


def enrich_rows(
    rows: list[dict[str, str]], cache: dict[str, Any], max_images: int, refresh: bool
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    total = len(rows)
    for index, row in enumerate(rows, start=1):
        url = row.get("URL", "").strip()
        cache_key = property_id_from_url(url)
        if not url:
            details = {"error": "Missing URL", "images": []}
        elif not refresh and cache_key in cache:
            details = cache[cache_key]
        else:
            print(f"[{index}/{total}] Fetching {url}", file=sys.stderr)
            try:
                details = extract_enrichment(url, max_images=max_images)
            except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                details = {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "error": f"{type(exc).__name__}: {exc}",
                    "images": [],
                }
            cache[cache_key] = details
        merged = dict(row)
        merged["_details"] = details
        merged["_anchor"] = make_property_anchor(merged, index)
        enriched.append(merged)
    return enriched


def money_sort_key(price_text: str) -> int:
    digits = "".join(ch for ch in price_text if ch.isdigit())
    return int(digits) if digits else 0


def nl2br(text: str) -> str:
    return "<br>".join(html.escape(part) for part in text.splitlines())


def render_map_card(
    latitude: str,
    longitude: str,
    fallback_url: str,
    address: str,
) -> str:
    label = html.escape(address or "property")
    lat = clean_text(latitude)
    lon = clean_text(longitude)
    fallback = clean_text(fallback_url)

    external_links: list[str] = []
    if lat and lon:
        osm_link = (
            f"https://www.openstreetmap.org/?mlat={html.escape(lat, quote=True)}"
            f"&mlon={html.escape(lon, quote=True)}#map=16/"
            f"{html.escape(lat, quote=True)}/{html.escape(lon, quote=True)}"
        )
        external_links.append(
            f'<a class="map-link" href="{osm_link}" target="_blank" rel="noreferrer">Open in OpenStreetMap</a>'
        )
    if fallback:
        external_links.append(
            f'<a class="map-link" href="{html.escape(fallback, quote=True)}" target="_blank" rel="noreferrer">Open external map</a>'
        )

    links_html = "".join(external_links)
    fallback_html = links_html or '<div class="asset-placeholder">No map available</div>'

    if not (lat and lon):
        return (
            '<div class="map-card-shell">'
            f'<div class="map-card map-card--link-only">'
            f'<p class="map-label">Map coordinates unavailable for {label}</p>'
            '<p class="map-hint">Use the external map link if you want to inspect the location.</p>'
            '</div>'
            f'{fallback_html}'
            '</div>'
        )

    return (
        f'<div class="map-card-shell">'
        f'<button class="map-card map-card--deferred" type="button" '
        f'data-map-lat="{html.escape(lat, quote=True)}" '
        f'data-map-lon="{html.escape(lon, quote=True)}" '
        f'data-map-label="Map for {label}">'
        '<span class="map-badge">OpenStreetMap</span>'
        '<strong>Load interactive map</strong>'
        '<span class="map-hint">Deferred to keep the report fast. Click to load an OpenStreetMap view with a real marker.</span>'
        '</button>'
        f'{links_html}'
        '</div>'
    )


def render_property_card(row: dict[str, Any]) -> str:
    details = row.get("_details", {})
    images = details.get("images", []) or []
    floorplans = details.get("floorplans", []) or []
    map_details = details.get("map", {}) if isinstance(details.get("map"), dict) else {}
    features = details.get("key_features", []) or []
    summary = clean_visible_summary(details.get("summary", "") or "")
    error = details.get("error", "") or ""

    gallery_html = "".join(
        f'<a href="{html.escape(url)}" target="_blank" rel="noreferrer">'
        f'<img src="{html.escape(url)}" loading="lazy" decoding="async" '
        f'alt="{html.escape(row.get("Address", "Property image"))}"></a>'
        for url in images
    )
    if not gallery_html:
        gallery_html = '<div class="image-placeholder">No images found</div>'

    floorplan_html = "".join(
        (
            f'<a class="asset-card" href="{html.escape(item.get("url") or item.get("image_url") or "")}" '
            f'target="_blank" rel="noreferrer">'
            f'<img src="{html.escape(item.get("image_url") or item.get("url") or "")}" '
            f'loading="lazy" decoding="async" '
            f'alt="{html.escape(item.get("caption") or "Floorplan")}">'
            f'<span>{html.escape(item.get("caption") or "Floorplan")}</span>'
            '</a>'
        )
        for item in floorplans
        if item.get("image_url") or item.get("url")
    )
    if not floorplan_html:
        floorplan_html = '<div class="asset-placeholder">No floorplan available</div>'

    map_url = map_details.get("url") or ""
    map_html = render_map_card(
        str(map_details.get("latitude") or ""),
        str(map_details.get("longitude") or ""),
        map_url,
        str(row.get("Address", "property")),
    )

    features_html = "".join(f"<li>{html.escape(item)}</li>" for item in features)
    if not features_html:
        features_html = "<li>No key features extracted</li>"

    metadata_items = [
        ("Price", clean_visible_price(row.get("Price", ""))),
        ("Type", row.get("Type", "")),
        ("Beds", row.get("Beds", "")),
        ("Area", row.get("Area", "")),
        ("Date Added", row.get("Date Added", "")),
        ("Bathrooms", details.get("bathrooms", "")),
        ("Agent", details.get("agent_name", "")),
    ]
    metadata_html = "".join(
        f"<div><span>{html.escape(label)}</span><strong>{html.escape(str(value or '—'))}</strong></div>"
        for label, value in metadata_items
    )

    note_html = ""
    if error:
        note_html = f'<p class="note warning">Listing enrichment failed: {html.escape(error)}</p>'
    elif details.get("added_or_reduced"):
        note_html = (
            f'<p class="note">Listing page says: '
            f'{html.escape(str(details.get("added_or_reduced")))}</p>'
        )

    title = details.get("display_address") or row.get("Address", "Unknown property")
    anchor = row.get("_anchor") or ""

    return f"""
    <article class=\"card\" id=\"{html.escape(str(anchor), quote=True)}\">
      <div class=\"card-header\">
        <div>
          <h2>{html.escape(str(title))}</h2>
          <p class=\"subtitle\">{html.escape(str(details.get('property_subtype') or row.get('Type') or 'Property'))}</p>
        </div>
        <a class=\"listing-link\" href=\"{html.escape(row.get('URL', ''))}\" target=\"_blank\" rel=\"noreferrer\">Open Rightmove listing</a>
      </div>
      <div class=\"metadata\">{metadata_html}</div>
      {note_html}
      <div class=\"gallery\">{gallery_html}</div>
      <div class=\"asset-sections\">
        <section>
          <h3>Floorplans</h3>
          <div class=\"asset-grid\">{floorplan_html}</div>
        </section>
        <section>
          <h3>Map</h3>
          <div class=\"asset-grid map-grid\">{map_html}</div>
        </section>
      </div>
      <div class=\"content\">
        <div>
          <h3>Summary</h3>
          <p>{nl2br(summary or 'No description extracted.')}</p>
        </div>
        <div>
          <h3>Key features</h3>
          <ul>{features_html}</ul>
        </div>
      </div>
    </article>
    """.strip()


def render_overview_section(rows: list[dict[str, Any]]) -> str:
    markers: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        details = row.get("_details", {})
        map_details = details.get("map", {}) if isinstance(details.get("map"), dict) else {}
        lat = clean_text(map_details.get("latitude"))
        lon = clean_text(map_details.get("longitude"))
        if not (lat and lon):
            continue
        markers.append(
            {
                "lat": lat,
                "lon": lon,
                "anchor": row.get("_anchor") or make_property_anchor(row, index),
                "title": details.get("display_address") or row.get("Address") or f"Property {index}",
                "price": clean_visible_price(row.get("Price", "")),
                "type": details.get("property_subtype") or row.get("Type") or "Property",
            }
        )

    if not markers:
        return """
        <section class=\"hero overview-card\">
          <h2>Overview map</h2>
          <p class=\"overview-copy\">Location coordinates were not available from the listing pages, so the overview map could not be generated for this run. Individual property cards may still include useful listing details and direct map links.</p>
        </section>
        """.strip()

    markers_json = html.escape(json.dumps(markers, ensure_ascii=False), quote=True)
    return f"""
    <section class=\"hero overview-card\" id=\"top\">
      <div class=\"overview-header\">
        <div>
          <h2>Overview map</h2>
          <p class=\"overview-copy\">{len(markers)} mapped properties. Load one interactive map for the full report, then click a marker popup to jump to that property section.</p>
        </div>
        <button class=\"overview-load-button\" type=\"button\" data-overview-map-trigger>Load overview map</button>
      </div>
      <div class=\"overview-map-shell\">
        <div class=\"overview-map-placeholder\" data-overview-map-placeholder>
          <strong>Interactive overview map is deferred</strong>
          <span>Keeps initial page load light. The map library is only fetched if you actually open it.</span>
        </div>
        <div class=\"overview-map\" data-overview-map hidden data-markers=\"{markers_json}\"></div>
        <noscript>
          <p class=\"note\">JavaScript is required for the overview map.</p>
        </noscript>
      </div>
    </section>
    """.strip()


def render_html(csv_path: Path, rows: list[dict[str, Any]]) -> str:
    sorted_rows = sorted(rows, key=lambda row: money_sort_key(row.get("Price", "")), reverse=True)
    property_cards = "\n".join(render_property_card(row) for row in sorted_rows)
    overview_section = render_overview_section(sorted_rows)
    areas = sorted({row.get("Area", "") for row in rows if row.get("Area")})
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Property search report - {html.escape(csv_path.name)}</title>
  <style>
    :root {{ color-scheme: light dark; }}
    html {{ scroll-behavior: smooth; }}
    body {{ font-family: Inter, Arial, sans-serif; margin: 0; background: #f5f7fb; color: #18202a; }}
    .page {{ max-width: 1200px; margin: 0 auto; padding: 32px 20px 60px; }}
    .hero {{ background: white; border-radius: 18px; padding: 24px; box-shadow: 0 12px 30px rgba(0,0,0,.08); margin-bottom: 24px; }}
    .hero h1, .hero h2 {{ margin: 0 0 10px; }}
    .hero h1 {{ font-size: 2rem; }}
    .hero p {{ margin: 8px 0; color: #475467; }}
    .overview-card {{ display: grid; gap: 18px; }}
    .overview-header {{ display: flex; justify-content: space-between; gap: 16px; align-items: start; flex-wrap: wrap; }}
    .overview-copy {{ max-width: 72ch; }}
    .overview-load-button, .back-to-top {{ border: 0; background: #0f62fe; color: #fff; border-radius: 999px; font-weight: 700; cursor: pointer; box-shadow: 0 12px 30px rgba(15,98,254,.28); }}
    .overview-load-button {{ padding: 12px 16px; }}
    .overview-map-shell {{ display: grid; gap: 12px; }}
    .overview-map-placeholder {{ min-height: 180px; border-radius: 16px; background: linear-gradient(135deg, #e8eefc, #f8fafc); display: grid; align-content: center; gap: 8px; padding: 20px; color: #334155; }}
    .overview-map {{ height: 420px; border-radius: 16px; overflow: hidden; border: 1px solid #d0d5dd; }}
    .overview-popup h3 {{ margin: 0 0 6px; font-size: 1rem; }}
    .overview-popup p {{ margin: 0 0 10px; color: #475467; font-size: .92rem; }}
    .overview-popup a {{ color: #0f62fe; font-weight: 700; text-decoration: none; }}
    .cards {{ display: grid; gap: 22px; }}
    .card {{ background: white; border-radius: 18px; box-shadow: 0 10px 24px rgba(0,0,0,.08); overflow: hidden; padding: 20px; scroll-margin-top: 20px; }}
    .card-header {{ display: flex; gap: 12px; justify-content: space-between; align-items: start; flex-wrap: wrap; }}
    h2 {{ margin: 0; font-size: 1.45rem; }}
    h3 {{ margin-bottom: 8px; }}
    .subtitle {{ margin: 6px 0 0; color: #667085; }}
    .listing-link {{ text-decoration: none; background: #0f62fe; color: white; padding: 10px 14px; border-radius: 999px; font-weight: 600; }}
    .metadata {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin: 18px 0; }}
    .metadata div {{ background: #f8fafc; padding: 12px; border-radius: 12px; }}
    .metadata span {{ display: block; font-size: .78rem; color: #667085; margin-bottom: 6px; text-transform: uppercase; letter-spacing: .04em; }}
    .gallery {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin: 18px 0 10px; }}
    .gallery img, .image-placeholder {{ width: 100%; height: 180px; object-fit: cover; border-radius: 14px; background: #e5e7eb; display: block; }}
    .image-placeholder, .asset-placeholder {{ display: flex; align-items: center; justify-content: center; color: #667085; font-weight: 600; }}
    .asset-sections {{ display: grid; grid-template-columns: 1.3fr 1fr; gap: 22px; margin: 10px 0 18px; }}
    .asset-sections section {{ min-width: 0; }}
    .asset-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }}
    .asset-card, .asset-placeholder {{ background: #f8fafc; border-radius: 14px; overflow: hidden; min-height: 180px; text-decoration: none; color: inherit; }}
    .asset-card img {{ width: 100%; height: 180px; object-fit: cover; display: block; background: #e5e7eb; }}
    .asset-card span {{ display: block; padding: 10px 12px; font-weight: 600; }}
    .map-card-shell {{ display: grid; gap: 10px; }}
    .map-card {{ width: 100%; border: 0; text-align: left; cursor: pointer; position: relative; overflow: hidden; isolation: isolate; border-radius: 14px; }}
    .map-card--deferred {{ display: grid; align-content: center; gap: 8px; min-height: 180px; padding: 18px; background: linear-gradient(135deg, #e8eefc, #f8fafc); color: #18202a; }}
    .map-card--deferred strong {{ font-size: 1rem; }}
    .map-card--deferred[aria-busy=\"true\"] {{ opacity: 0.72; cursor: progress; }}
    .map-badge {{ display: inline-flex; width: fit-content; padding: 5px 10px; border-radius: 999px; background: rgba(15, 98, 254, 0.12); color: #0f62fe; font-size: .78rem; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; }}
    .map-hint {{ margin: 0; color: #475467; line-height: 1.45; font-size: .94rem; }}
    .map-link {{ color: #0f62fe; font-weight: 600; text-decoration: none; }}
    .map-card--loaded {{ padding: 0; background: #f8fafc; display: grid; overflow: hidden; border: 1px solid #d0d5dd; }}
    .map-card__frame {{ width: 100%; height: 260px; border: 0; display: block; background: #e5e7eb; }}
    .map-card--link-only {{ display: grid; align-content: center; gap: 10px; min-height: 180px; padding: 18px; background: #f8fafc; }}
    .map-label {{ margin: 0; color: #475467; }}
    .content {{ display: grid; grid-template-columns: 2fr 1fr; gap: 22px; }}
    .content p, .content li {{ line-height: 1.5; }}
    .note {{ color: #475467; margin: 6px 0 0; }}
    .warning {{ color: #b42318; }}
    ul {{ margin-top: 0; padding-left: 20px; }}
    .back-to-top {{ position: fixed; right: 22px; bottom: 22px; padding: 14px 16px; text-decoration: none; z-index: 20; }}
    @media (max-width: 800px) {{ .asset-sections, .content {{ grid-template-columns: 1fr; }} .page {{ padding: 18px 12px 40px; }} .overview-map {{ height: 340px; }} .back-to-top {{ right: 14px; bottom: 14px; }} }}
  </style>
</head>
<body>
  <main class=\"page\">
    <section class=\"hero\">
      <h1>Property search report</h1>
      <p><strong>Source CSV:</strong> {html.escape(str(csv_path.name))}</p>
      <p><strong>Properties:</strong> {len(rows)} · <strong>Areas:</strong> {html.escape(', '.join(areas) or '—')}</p>
      <p><strong>Generated:</strong> {html.escape(generated_at)}</p>
      <p>This page is generated from the existing Rightmove CSV output, with listing pages fetched on demand to add images and richer detail.</p>
    </section>
    {overview_section}
    <section class=\"cards\">
      {property_cards}
    </section>
  </main>
  <a class=\"back-to-top\" href=\"#top\" aria-label=\"Return to top\">↑ Top</a>
  <script>
    (function() {{
      function buildEmbedUrl(lat, lon) {{
        const latitude = Number(lat);
        const longitude = Number(lon);
        if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return '';

        const latPad = 0.003;
        const lonPad = 0.006;
        const bbox = [longitude - lonPad, latitude - latPad, longitude + lonPad, latitude + latPad]
          .map((value) => value.toFixed(6))
          .join(',');

        return 'https://www.openstreetmap.org/export/embed.html?bbox=' + bbox + '&layer=mapnik&marker=' + latitude.toFixed(6) + '%2C' + longitude.toFixed(6);
      }}

      function buildLoadedMapCard(lat, lon, label) {{
        const embedUrl = buildEmbedUrl(lat, lon);
        if (!embedUrl) return null;

        const loadedCard = document.createElement('div');
        loadedCard.className = 'map-card map-card--loaded';

        const frame = document.createElement('iframe');
        frame.className = 'map-card__frame';
        frame.loading = 'lazy';
        frame.referrerPolicy = 'no-referrer-when-downgrade';
        frame.title = label;
        frame.src = embedUrl;

        loadedCard.appendChild(frame);
        return loadedCard;
      }}

      function loadScript(src) {{
        return new Promise(function(resolve, reject) {{
          const existing = document.querySelector('script[data-src="' + src + '"]');
          if (existing) {{
            existing.addEventListener('load', function() {{ resolve(); }}, {{ once: true }});
            existing.addEventListener('error', function() {{ reject(new Error('Failed to load ' + src)); }}, {{ once: true }});
            if (existing.dataset.loaded === 'true') resolve();
            return;
          }}
          const script = document.createElement('script');
          script.src = src;
          script.async = true;
          script.dataset.src = src;
          script.onload = function() {{ script.dataset.loaded = 'true'; resolve(); }};
          script.onerror = function() {{ reject(new Error('Failed to load ' + src)); }};
          document.head.appendChild(script);
        }});
      }}

      function loadStyle(href) {{
        return new Promise(function(resolve, reject) {{
          const existing = document.querySelector('link[data-href="' + href + '"]');
          if (existing) {{ resolve(); return; }}
          const link = document.createElement('link');
          link.rel = 'stylesheet';
          link.href = href;
          link.dataset.href = href;
          link.onload = function() {{ resolve(); }};
          link.onerror = function() {{ reject(new Error('Failed to load ' + href)); }};
          document.head.appendChild(link);
        }});
      }}

      async function loadOverviewMap() {{
        const trigger = document.querySelector('[data-overview-map-trigger]');
        const mapNode = document.querySelector('[data-overview-map]');
        const placeholder = document.querySelector('[data-overview-map-placeholder]');
        if (!trigger || !mapNode) return;
        if (mapNode.dataset.loaded === 'true') return;

        trigger.disabled = true;
        trigger.textContent = 'Loading overview map…';

        try {{
          await loadStyle('https://unpkg.com/leaflet@1.9.4/dist/leaflet.css');
          await loadScript('https://unpkg.com/leaflet@1.9.4/dist/leaflet.js');

          const markers = JSON.parse(mapNode.dataset.markers || '[]');
          if (!Array.isArray(markers) || !markers.length || !window.L) {{
            throw new Error('No markers available');
          }}

          mapNode.hidden = false;
          if (placeholder) placeholder.hidden = true;

          const map = window.L.map(mapNode, {{ scrollWheelZoom: false }});
          window.L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            maxZoom: 19,
            attribution: '&copy; OpenStreetMap contributors'
          }}).addTo(map);

          const bounds = [];
          markers.forEach(function(marker) {{
            const lat = Number(marker.lat);
            const lon = Number(marker.lon);
            if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
            bounds.push([lat, lon]);
            const popupHtml = [
              '<div class="overview-popup">',
              '<h3>' + String(marker.title || 'Property') + '</h3>',
              '<p>' + String(marker.price || '') + (marker.type ? ' · ' + String(marker.type) : '') + '</p>',
              '<a href="#' + String(marker.anchor || '') + '" data-jump-anchor="' + String(marker.anchor || '') + '">Jump to property</a>',
              '</div>'
            ].join('');
            window.L.marker([lat, lon]).addTo(map).bindPopup(popupHtml);
          }});

          if (bounds.length === 1) {{
            map.setView(bounds[0], 13);
          }} else {{
            map.fitBounds(bounds, {{ padding: [30, 30] }});
          }}

          mapNode.dataset.loaded = 'true';
          trigger.hidden = true;
        }} catch (error) {{
          trigger.disabled = false;
          trigger.textContent = 'Retry overview map';
          if (placeholder) {{
            placeholder.hidden = false;
            placeholder.innerHTML = '<strong>Overview map could not be loaded</strong><span>The interactive map library was unavailable. You can still use the property cards below.</span>';
          }}
        }}
      }}

      document.addEventListener('click', function(event) {{
        const target = event.target;
        if (!(target instanceof Element)) return;

        const overviewTrigger = target.closest('[data-overview-map-trigger]');
        if (overviewTrigger) {{
          event.preventDefault();
          loadOverviewMap();
          return;
        }}

        const jumpLink = target.closest('[data-jump-anchor]');
        if (jumpLink) {{
          const anchor = jumpLink.getAttribute('data-jump-anchor');
          if (!anchor) return;
          const card = document.getElementById(anchor);
          if (card) {{
            setTimeout(function() {{ card.scrollIntoView({{ behavior: 'smooth', block: 'start' }}); }}, 20);
          }}
          return;
        }}

        const trigger = target.closest('.map-card--deferred');
        if (!trigger || trigger.dataset.loading === 'true') return;

        const lat = trigger.dataset.mapLat;
        const lon = trigger.dataset.mapLon;
        const label = trigger.dataset.mapLabel || 'Map preview';
        if (!lat || !lon) return;

        trigger.dataset.loading = 'true';
        trigger.disabled = true;
        trigger.setAttribute('aria-busy', 'true');

        const loadedCard = buildLoadedMapCard(lat, lon, label);
        if (!loadedCard) {{
          trigger.disabled = false;
          trigger.dataset.loading = 'false';
          trigger.removeAttribute('aria-busy');
          const hint = trigger.querySelector('.map-hint');
          if (hint) {{
            hint.textContent = 'Map unavailable right now. Use the external map link below instead.';
          }}
          return;
        }}

        trigger.replaceWith(loadedCard);
      }});
    }})();
  </script>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    csv_path = args.csv_path.resolve()
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        return 1

    rows = load_rows(csv_path)
    if not rows:
        print(f"No rows found in {csv_path}", file=sys.stderr)
        return 1

    cache_file = cache_path_for(csv_path)
    cache = {} if args.refresh else load_cache(cache_file)
    enriched_rows = enrich_rows(rows, cache, max_images=args.max_images, refresh=args.refresh)
    save_cache(cache_file, cache)

    output_path = output_path_for(csv_path, args.output).resolve()
    output_path.write_text(render_html(csv_path, enriched_rows), encoding="utf-8")

    print(f"Wrote HTML report: {output_path}")
    print(f"Wrote enrichment cache: {cache_file.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
