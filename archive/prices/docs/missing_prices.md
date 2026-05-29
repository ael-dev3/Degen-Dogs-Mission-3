# Missing prices

This file documents expected missing-price behavior. The generated estimate file is authoritative after running the applier.

Rows are marked missing when:

- the asset has no configured/verified price identifier,
- the source API has no same-day or near-prior price,
- the event date is outside available coverage, or
- the native amount itself is missing/non-auction.

Missing prices are represented as nulls in JSON/CSV, never zero.
