# Agent instructions

This project searches Rightmove for-sale listings and builds a static report in `site/`.
Take a requested search through configuration, build, publication and verification using
the hosting tools available to you. Searching requires no credentials or accounts.

## First use and configuration

When a user uses this project for the first time, ask questions to establish their
requirements before creating a customized `search_config.yaml` from
`search_config.example.yaml`. The example's values are a starting template, not evidence
of the user's preferences.

Use requirements already provided in the request or conversation and ask only for missing
information:

- Locations or stations and search radius.
- Minimum and maximum price.
- Property types and bedroom range.
- Date window and whether they want a full report or an update since the previous report.
- Hosting preference and audience, if needed to complete publication.

`search_config.yaml` is gitignored. Preserve an existing local config and reuse it unless
the user requests changes. Do not overwrite it with the example. Update the example only
when requested; neither file automatically synchronizes with the other.

Resolve and verify location IDs with Rightmove's autocomplete endpoint. Configuration
keys are documented in the [`rightmove_search.py` module docstring](rightmove_search.py);
see [configuration details](docs/technical.md#configuration) for the lookup command.

## Choose the report mode

- **Full date-window report**, such as “properties in the last 30 days”: include all current
  matches added or repriced within that window. Use a new empty output directory and the
  [full-report workflow](docs/technical.md#full-date-window-report) so previous CSVs do not
  suppress matches. Preserve existing history unless the user authorizes its removal.
- **Update since the last report**: identify the source CSV of the report the user wants
  to update and reuse its output directory and baseline. A full report in an isolated
  directory has a separate baseline from the standard `results/` workflow. Ensure the
  configured date window covers the time since the previous update. The code
  compares IDs and prices against only the newest CSV, not cumulative history or an
  automatically tracked update timestamp. Explain this limitation when it affects the
  requested result; see [comparison behavior](docs/technical.md#search-history-and-comparison).

For windows above 14 days, the scraper crawls broader results and filters listing dates
locally. Allow for longer searches and note any skipped unknown dates in the logs.

## Build

Follow the [setup instructions](docs/technical.md#setup), then run:

```bash
./build_site.sh
```

The standard build reads `search_config.yaml` and expects CSV output in `results/`.
For another config or output directory, run the search explicitly and pass its CSV to
the build script as shown in the technical guide. Do not invent unsupported CLI flags.

The scraper uses human-like pacing. Do not lower its delays to speed up a search.
Monitor station failures and enrichment errors, and distinguish partial results from a
complete search. If a sandbox prevents Camoufox from launching, use the environment's
permission mechanism rather than changing scraper behavior.

| Exit code | Action |
| --- | --- |
| 0 | Verify the new report and proceed with requested publication. |
| 2 | No new or repriced listings; the existing site is unchanged. Do not describe it as a freshly generated report. |
| 1 | Diagnose the error, including the case where every station search fails. |

`./build_site.sh --refresh` refetches listing details; it does not reset search history.

## Publish and verify

When publication is requested, publish the completed `site/` with an available static
hosting tool. Reuse an identified existing deployment and preserve its audience. For a
new deployment, prefer private or unlisted access unless the user requests otherwise.
Complete authorized publication without adding a separate conversational approval gate.

Before reporting success:

1. Check the report's source CSV, generation time, property count and any search or
   enrichment failures against the requested configuration.
2. Confirm deployment succeeded and return its actual URL.
3. Check the deployed content policy and verify external photos, overview-map tiles and
   markers, and a per-property embedded map. Scroll to trigger lazy images and click both
   map load controls. Follow the [deployment checks](docs/technical.md#deployment-and-external-content).

Private sites may require sign-in for direct browser checks. Do not make a site public
to simplify testing. State what was verified and any remaining verification limits.

Use the [technical troubleshooting guidance](docs/technical.md#troubleshooting) for empty
results, browser failures, extraction errors and diagnostic logging.
