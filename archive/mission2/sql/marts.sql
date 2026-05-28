-- Future Mission 2 archive marts. These views are safe when base tables are empty.

CREATE VIEW IF NOT EXISTS mission2_auction_winners AS
SELECT
  s.chain_id,
  s.dog_id,
  s.winner,
  s.amount_raw,
  s.amount_display_native,
  s.display_decimals_confidence,
  s.block_number,
  s.block_time_utc,
  s.tx_hash,
  s.source_confidence
FROM mission2_auction_settled s;

CREATE VIEW IF NOT EXISTS mission2_recent_bids AS
SELECT
  b.chain_id,
  b.dog_id,
  b.bidder,
  b.value_raw,
  b.value_display_native,
  b.display_decimals_confidence,
  b.extended,
  b.block_number,
  b.block_time_utc,
  b.tx_hash,
  b.log_index,
  b.source_confidence
FROM mission2_auction_bids b
ORDER BY b.block_number DESC, b.log_index DESC;

CREATE VIEW IF NOT EXISTS mission2_bidder_leaderboard AS
SELECT
  chain_id,
  bidder,
  COUNT(*) AS bid_count,
  MIN(block_number) AS first_bid_block,
  MAX(block_number) AS last_bid_block,
  'raw amount totals intentionally omitted until decimals/currency path are verified' AS amount_note
FROM mission2_auction_bids
GROUP BY chain_id, bidder
ORDER BY bid_count DESC, last_bid_block DESC;

CREATE VIEW IF NOT EXISTS mission2_auction_daily_activity AS
SELECT
  chain_id,
  substr(COALESCE(block_time_utc, ''), 1, 10) AS activity_date_utc,
  COUNT(*) AS bid_count,
  COUNT(DISTINCT dog_id) AS dogs_with_bids,
  COUNT(DISTINCT bidder) AS unique_bidders
FROM mission2_auction_bids
GROUP BY chain_id, activity_date_utc
ORDER BY activity_date_utc DESC;

CREATE VIEW IF NOT EXISTS mission2_archive_metrics AS
SELECT 'auction_created_rows' AS metric, CAST(COUNT(*) AS TEXT) AS value FROM mission2_auction_created
UNION ALL SELECT 'auction_bid_rows', CAST(COUNT(*) AS TEXT) FROM mission2_auction_bids
UNION ALL SELECT 'auction_extended_rows', CAST(COUNT(*) AS TEXT) FROM mission2_auction_extended
UNION ALL SELECT 'auction_settled_rows', CAST(COUNT(*) AS TEXT) FROM mission2_auction_settled
UNION ALL SELECT 'raw_log_rows', CAST(COUNT(*) AS TEXT) FROM mission2_raw_logs
UNION ALL SELECT 'woof_vault_allocation_rows', CAST(COUNT(*) AS TEXT) FROM mission2_woof_vault_allocations
UNION ALL SELECT 'known_gap_rows', CAST(COUNT(*) AS TEXT) FROM mission2_index_gaps;

CREATE VIEW IF NOT EXISTS mission2_top_dogs_by_bid AS
SELECT
  chain_id,
  dog_id,
  COUNT(*) AS bid_count,
  MAX(block_number) AS last_bid_block,
  'sort by raw amount only after currency path and decimals are verified' AS amount_note
FROM mission2_auction_bids
GROUP BY chain_id, dog_id
ORDER BY bid_count DESC, last_bid_block DESC;

CREATE VIEW IF NOT EXISTS mission2_settlement_gaps AS
SELECT
  c.chain_id,
  c.dog_id,
  c.start_time_utc,
  c.end_time_utc,
  c.tx_hash AS created_tx_hash,
  CASE WHEN s.dog_id IS NULL THEN 'missing_settlement' ELSE 'settled' END AS settlement_status
FROM mission2_auction_created c
LEFT JOIN mission2_auction_settled s
  ON s.chain_id = c.chain_id AND s.dog_id = c.dog_id;

CREATE VIEW IF NOT EXISTS mission2_future_dashboard_bridge AS
SELECT
  m.metric,
  m.value,
  'Mission 2 archive; not yet integrated into the live dashboard' AS dashboard_status
FROM mission2_archive_metrics m;
