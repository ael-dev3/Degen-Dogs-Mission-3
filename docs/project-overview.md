# Project overview

Degen Dogs Mission 3 Analytics is an independent community-built analytics dashboard and archive layer for Degen Dogs Mission 3 on Base.

The public site is a static GitHub Pages dashboard. A local runner fetches public/onchain data, decodes contract events, loads an in-memory SQLite database, runs the approved SQL layer, and writes the CSV/JSON/static HTML files that visitors see.

## Audience

- Degen Dogs collectors who want a fast view of current auction state and recent winners.
- Community members who want downloadable CSV/JSON data.
- Developers and agents who need a reproducible query layer.
- Future maintainers who may need to fork, rebuild, or recover the dashboard.

## Archive direction

The repo is broader than the live Mission 3 page:

- Mission 1: Polygon-era historical archive/research.
- Mission 2: Degen Chain archive/recovery.
- Mission 3: Base live dashboard and rolling archive.
- Unified Dog search: cross-mission records where verified outputs exist.

The hosted auction feed still defaults to the latest 10 archive records for casual visitors. Power users can use the compact controls above the feed to search, paginate, change rows per page, filter by Mission 1/2/3, and sort by highest historical estimated USD bid where that estimate exists.

## Independence

Degen Dogs was created by Mark Carey / dogmaster. This repo is a community analytics/archive contribution and should not be presented as official unless the project explicitly approves that wording.
