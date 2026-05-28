# Future Dashboard Integration

Do not force a new archive UI until requested.

Prepared files for later dashboard work:

- `archive/mission3/data/generated/mission3_dog_search_index.json`
- `archive/mission3/data/generated/mission3_archive_metrics.json`
- optional public copies under `public/generated/mission3/`

Future Dog search should show only verified fields:

- mission era
- chain
- auction created tx/block/time
- settlement tx/block/time when present
- winner and amount when settled
- bid counts
- OpenSea link for Base Mission 3 Dogs
- confidence and sources

Current/live Dogs must not be marked settled unless an `AuctionSettled` event exists.
