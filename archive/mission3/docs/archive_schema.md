# Mission 3 Archive Schema

The canonical schema lives in `archive/mission3/sql/schema.sql`.

Core tables:

- `mission3_raw_logs`: raw Base logs, idempotent by `(chain_id, transaction_hash, log_index)`.
- `mission3_auction_created`: decoded created events.
- `mission3_auction_bids`: decoded bid events with raw wei strings and derived ETH strings.
- `mission3_auction_extended`: decoded extension events.
- `mission3_auction_settled`: decoded settlement events.
- `mission3_index_state`: latest indexing state and last run status.
- `mission3_index_gaps`: failed or deferred log chunks.
- `mission3_current_auction_snapshots`: point-in-time current auction reads.

Derived marts live in `archive/mission3/sql/marts.sql` and can be regenerated from raw/decoded tables.
