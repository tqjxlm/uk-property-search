# property_search

Scrape Rightmove for-sale listings around the stations you choose, then publish a browsable
static report with photos, floorplans, key features and an overview map.

It needs no credentials, API keys or accounts. The output is one self-contained
`site/index.html` that any static host can serve.

## Use it with an AI agent

Point your coding agent (Claude Code, Codex, Cursor, Gemini CLI, ...) at this repository and
ask something like:

> Set up this project, search for 2–3 bed houses under £700k near Clapham Junction and
> Balham, and deploy the report.

[`AGENTS.md`](AGENTS.md) tells the agent how to install the project, look up location IDs,
build the site, and deploy `site/` with whatever hosting platform it can use.

## Use it by hand

Requires Python 3.11–3.14; Camoufox doesn't work on 3.15 yet.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m camoufox fetch
cp search_config.example.yaml search_config.yaml   # then edit
./build_site.sh
```

Open `site/index.html` in a browser, or upload the `site/` folder to any static host.

### How it works

1. `rightmove_search.py` runs each station search in Camoufox, a fingerprint-resistant
   Firefox, with human-like pacing. It writes `results/rightmove_<timestamp>.csv` containing
   only the listings that are new or re-priced since the previous CSV.
2. `generate_report.py` fetches each listing page for images, floorplans, description,
   features and coordinates. It caches them in `results/<name>.enriched.json` and renders
   `results/<name>.html`.
3. `build_site.sh` chains the two scripts and copies the report to `site/index.html`.
   Run `./build_site.sh --help` for options.

### Finding location IDs

```bash
curl -s -A "Mozilla/5.0" "https://los.rightmove.co.uk/typeahead?query=balham&limit=10&exclude=STREET"
```

Join each match's `type` and `id` with `^`, for example `STATION^506`, and use that as the
station `id` in `search_config.yaml`.

## Limitations

- Scraping depends on Rightmove's current page structure (`data-testid` selectors and the
  embedded `PAGE_MODEL` JSON). If Rightmove redesigns its site, results can come back empty
  without any error.
- Block or CAPTCHA pages aren't detected; a station that is blocked just returns zero
  results.
- Only for-sale searches are supported, not rentals.
- The report hotlinks images from Rightmove instead of downloading them.

## Disclaimer

This is an unofficial tool for personal property searches and has no connection to
Rightmove. Automated access may conflict with Rightmove's terms of use, and listing content
belongs to Rightmove and the agents who list the properties. Use it responsibly, keep the
request rate low, and prefer private deployments of generated reports.

## License

[MIT](LICENSE)
