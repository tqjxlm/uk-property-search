# property_search

Find Rightmove properties for sale around your chosen stations and publish a browsable
report with photos, floorplans, descriptions and interactive maps.

## Usage

Prefer using this project with an AI coding agent, preferably ChatGPT with access to the
repository and a publishing tool. Give the agent this repository and describe the report
you want. Other coding agents can use the same [agent instructions](AGENTS.md).

Two typical requests are:

> Generate a report for properties in the last 30 days and publish it.

This produces a full report of matching listings added or repriced during that window.

> Generate a report for properties since the last update and publish it.

This produces an incremental report using the previous CSV. Keep your search history
between updates. Comparison is against the most recent CSV within the configured search
window, rather than a complete record of all past listings.

On first use, the agent asks about locations, budget, property types, bedrooms and search
window, then creates a customized local configuration from the template. You can also
provide these requirements in your initial request.

For manual setup and commands, see the [technical guide](docs/technical.md).

## Expected results

- A CSV of matching properties and a report in `site/index.html`, ready for static hosting.
- Listing photos, floorplans, key features and maps where Rightmove provides them.
- A published URL when you ask the agent to publish and it has access to a suitable host.
  Prefer private or unlisted hosting because the report reveals your search criteria.
- If an incremental search finds nothing new or repriced, the existing report stays unchanged.

Searching needs no credentials, API keys or accounts. Images and maps require internet
access, and private hosting may require sign-in. Searches take several minutes; wider
date ranges can take longer. Only for-sale searches are supported.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| No properties returned | Check your criteria and locations. Rightmove blocking or page changes can also cause empty results. |
| The report still shows an earlier run | An incremental search may have found no changes. Check the source CSV and generation time shown in the report. |
| Photos or maps do not appear | Check internet access and the host's external-content policy. Click the map's load button to open it. |
| Setup or browser launch fails | Use a supported Python version and install the scraper's browser. |

See the [technical guide](docs/technical.md) for setup, history handling and deployment
checks. Scraping depends on Rightmove's current site structure, and block or CAPTCHA
pages are not reliably detected.

## Disclaimer

This is an unofficial tool for personal property searches and has no connection to
Rightmove. Automated access may conflict with Rightmove's terms of use, and listing content
belongs to Rightmove and the agents who list the properties. Use it responsibly, keep the
request rate low, and prefer private deployments of generated reports.

## License

[MIT](LICENSE)
