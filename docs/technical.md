# Technical guide

## Setup

Requires Python 3.11–3.14 and Bash. Python 3.15 is unsupported because Camoufox's
fingerprint generator fails with `binascii.Error: Incomplete Base85 group`. If `python3`
is 3.15, use `python3.14` or `uv venv`, which reads `.python-version`.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m camoufox fetch
```

The final command downloads the stealth Firefox binary; installing Python dependencies
alone is insufficient.

## Configuration

When no local config exists, copy `search_config.example.yaml` to `search_config.yaml`
and customize it. The build reads the gitignored local file; the example and local
config do not update each other automatically.

The [`rightmove_search.py` module docstring](../rightmove_search.py) documents filter keys.
`property_types` accepts `detached`, `semi-detached`, `terraced` and `flat`.

Location IDs come from Rightmove's autocomplete endpoint:

```bash
curl -s -A "Mozilla/5.0" "https://los.rightmove.co.uk/typeahead?query=clapham%20junction&limit=10&exclude=STREET"
```

Join a match's `type` and `id` with `^`, for example `STATION^2156`. Verify that the match
is the intended location rather than relying on its display name alone.

## Build and report files

```bash
./build_site.sh
```

The build runs `rightmove_search.py`, enriches the resulting CSV with
`generate_report.py`, and copies the generated HTML into `site/index.html`. Listing
details are cached beside the CSV in `<name>.enriched.json`; the report is `<name>.html`.
Photos remain external URLs rather than downloaded assets.

`build_site.sh` reads `search_config.yaml` and discovers new CSVs only in `results/`.
Keep `output.directory: results` for this command. The search script supports other
configs and output directories, but those require explicitly passing the resulting CSV
to the build script.

```bash
./build_site.sh --csv results/rightmove_YYYYMMDD_HHMMSS.csv
```

Replace the example filename with an existing CSV. This rebuilds without searching.
`--max-images N` sets the photo limit per property (default: 6). `--refresh` refetches
listing details instead of using the enrichment cache; it does not reset the CSV baseline.

Exit `0` means the site was built, `2` means there are no new or repriced listings and
the existing site stays unchanged, and `1` means an error. Check the report's source CSV
and generation time before publishing an existing output.

## Search history and comparison

The newest timestamped `rightmove_*.csv` in the configured output directory is the
comparison baseline, even if it came from a different config. A listing is exported if
its ID is absent from that CSV or its price differs. Without a baseline, every matching
listing is exported. Cross-station deduplication is controlled by `output.deduplicate`;
it does not disable comparison against the previous CSV.

Each exported CSV contains only that run's new or repriced listings, rather than a full
snapshot. A listing absent from the newest CSV can therefore appear again in a later
report. Retaining older CSVs does not provide cumulative history, and the scraper does
not record a separate last-update timestamp. The configured date window still applies
before comparison, so it must cover the time between updates to avoid excluding older
changes.

To reset comparison history in the default directory and remove its generated reports
and enrichment caches:

```bash
find results -maxdepth 1 -type f \
  \( -name 'rightmove_*.csv' -o -name 'rightmove_*.html' -o -name 'rightmove_*.enriched.json' \) \
  -delete
```

This deletes the matching files, including the CSV baselines. It preserves unrelated
files and subdirectories. Use the configured output directory instead of `results` if
different. To preserve history while making a full report, use an isolated directory.

## Full date-window report

Use a new empty output directory for each full report so existing CSVs cannot suppress
unchanged matches. Keep a copy of the user's customized config under gitignored
`results/`; for example:

```bash
mkdir -p results
mkdir results/full-report
cp search_config.yaml results/full-report/search_config.yaml
```

Choose another unused directory name if `results/full-report` already exists. In the
copied YAML, retain the user's other settings and set `filters.max_days_since_added`
to the requested window, such as `30`, and `output.directory` to `results/full-report`.
Then run:

```bash
.venv/bin/python rightmove_search.py --config results/full-report/search_config.yaml
./build_site.sh --csv results/full-report/rightmove_YYYYMMDD_HHMMSS.csv
```

Use the actual CSV path emitted by the search. This leaves the normal incremental
baseline untouched and produces the full report in `site/index.html` for publication.
For a later update relative to this full report, reuse its copied config and output
directory with the same explicit search and `--csv` pipeline. The default `results/`
baseline remains separate.

Rightmove's URL age filter supports at most 14 days. For a larger configured window,
the scraper omits that URL filter, crawls broader results and filters locally using
the displayed Added or Reduced date. Unknown date formats are excluded and counted in
the logs. Broad windows can therefore require substantially more pages and time.

## Deployment and external content

Publish `site/` with any static host; it contains one `index.html` and needs no server-side
build. The single file still depends on external services for images and maps. Prefer
private or unlisted access because the content reveals the user's search criteria.

If the host enforces a Content Security Policy (CSP), it must permit the report's inline
scripts/styles and external sources. Relevant directives are:

```text
script-src 'self' 'unsafe-inline' https://unpkg.com;
style-src 'self' 'unsafe-inline' https://unpkg.com;
img-src 'self' data: https://media.rightmove.co.uk https://unpkg.com https://tile.openstreetmap.org;
frame-src 'self' https://www.openstreetmap.org;
```

Rightmove serves photos and floorplans. Leaflet's scripts, styles and marker images come
from `unpkg.com`; overview tiles come from `tile.openstreetmap.org`. Per-property maps
embed `www.openstreetmap.org` in an iframe. Inspect the deployed response headers: adding
a permissive CSP cannot relax another stricter policy enforced by the host.

In a browser, scroll through cards to trigger lazy images, click **Load overview map**
and verify tiles and property markers, then click **Load interactive map** on a card to
verify the embedded map. Check the console and network panel for blocked or failed
resources. Private hosting may require sign-in. An authenticated HTTP fetch can verify
the deployed HTML and headers, but does not establish that the signed-in browser flow
works; report those checks separately.

## Troubleshooting

- **Browser fails to start:** check the Python version and Camoufox binary installation.
  A sandbox may prevent the browser process from launching even when installation is valid.
- **Zero results for a station:** verify the location ID and criteria. Rightmove block or
  CAPTCHA pages are not reliably detected and can look like valid empty results.
- **Missing images or extraction errors:** the listing may have been removed or Rightmove
  may have changed its embedded `window.PAGE_MODEL` / `__PAGE_MODEL` data.
- **Old report after a build:** exit `2` preserves the existing site when comparison finds
  no new or repriced listings. Use a full report when the desired result is every match.
- **Missing maps or photos after publication:** check the deployed content policy and
  external requests as described above; both map views load only when requested.

For more verbose search logs:

```bash
.venv/bin/python rightmove_search.py --log-level DEBUG
```

Keep scraper delays intact; faster requests can cause Rightmove to block searches.
