# AGENTS.md

Instructions for AI coding agents. This project scrapes Rightmove for-sale listings around
chosen stations and builds a single static HTML report in `site/`. It needs no credentials,
API keys or accounts. Deploying `site/` is left to whatever static hosting you have access to.

## Setup

Requires Python 3.11–3.14 and a POSIX shell. Python 3.15 is not supported: Camoufox's
fingerprint generator fails with `binascii.Error: Incomplete Base85 group`. If `python3`
is 3.15, use `python3.14` or `uv venv` (which reads `.python-version`).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m camoufox fetch   # downloads the stealth Firefox binary; pip alone is not enough
```

## Configure

`search_config.yaml` is the user's local config and is gitignored. If it does not exist,
copy `search_config.example.yaml` and edit it with the user's requirements. Ask them for
locations, price range, property types, bedrooms and how many days back to look.

Location IDs (`STATION^2156`, `REGION^85434`, ...) come from Rightmove's autocomplete
endpoint. Each match's identifier is `<type>^<id>`:

```bash
curl -s -A "Mozilla/5.0" "https://los.rightmove.co.uk/typeahead?query=clapham%20junction&limit=10&exclude=STREET"
```

Filter keys are documented in the `rightmove_search.py` module docstring. `property_types`
accepts `detached`, `semi-detached`, `terraced`, `flat`.

## Build

```bash
./build_site.sh
```

This runs the search, generates the report and writes `site/index.html`. It takes several
minutes because the scraper uses human-like delays. Don't lower them, or Rightmove starts
blocking requests.

| Exit code | Meaning |
| --- | --- |
| 0 | `site/index.html` built |
| 2 | Nothing new since the last run; `site/` left as it was |
| 1 | Error, including every station search failing |

Each run reports only listings that are **new or re-priced** since the previous CSV in
`results/`. The first run reports everything that matches. Keep `results/` between runs,
or every run will look like a first run.

`./build_site.sh --csv results/<file>.csv` rebuilds the site from an existing CSV without
searching again.

## Deploy

`site/` is a self-contained static site with one `index.html` and no build step. Publish
it with any static host you can use, for example Netlify (`netlify deploy --dir site --prod`),
Vercel (`vercel deploy site --prod`), Cloudflare Pages (`wrangler pages deploy site`),
GitHub Pages, S3, or your own platform's publishing tool.

At view time the page loads listing images from Rightmove's CDN, Leaflet from `unpkg.com`,
and map tiles from `tile.openstreetmap.org`. A host that enforces a strict CSP must allow
those origins.

The report contains Rightmove listing content and reveals what the user is searching for.
Prefer a private or unlisted deployment unless the user asks otherwise.

## Troubleshooting

- **Zero results for a station**: the location ID may be wrong, or Rightmove served a
  block or CAPTCHA page, which the scraper can't detect. Re-check the ID with the
  autocomplete endpoint and retry later.
- **Cards with no images or an extraction error**: Rightmove changed its listing-page
  data (`window.PAGE_MODEL` / `__PAGE_MODEL`), or the listing was removed.
- More verbose logs: `.venv/bin/python rightmove_search.py --log-level DEBUG`.
