-- Mission 1 archive marts. Views remain safe when base tables are empty.

CREATE VIEW IF NOT EXISTS mission1_auction_winners AS
SELECT
  s.chain_id,
  s.dog_id,
  s.winner,
  s.amount_raw,
  s.amount_display_weth,
  s.display_decimals_confidence,
  s.block_number,
  s.block_time_utc,
  s.tx_hash,
  s.source_confidence
FROM mission1_auction_settled s;

CREATE VIEW IF NOT EXISTS mission1_recent_bids AS
SELECT
  b.chain_id,
  b.dog_id,
  b.bidder,
  b.value_raw,
  b.value_display_weth,
  b.display_decimals_confidence,
  b.extended,
  b.block_number,
  b.block_time_utc,
  b.tx_hash,
  b.log_index,
  b.source_confidence
FROM mission1_auction_bids b
ORDER BY b.block_number DESC, b.log_index DESC;

CREATE VIEW IF NOT EXISTS mission1_bidder_leaderboard AS
SELECT
  chain_id,
  bidder,
  COUNT(*) AS bid_count,
  COUNT(DISTINCT dog_id) AS dogs_bid_on,
  MIN(block_number) AS first_bid_block,
  MAX(block_number) AS last_bid_block,
  'raw amount totals intentionally omitted until all WETH/Idle routes are reconciled' AS amount_note
FROM mission1_auction_bids
GROUP BY chain_id, bidder
ORDER BY bid_count DESC, last_bid_block DESC;

CREATE VIEW IF NOT EXISTS mission1_auction_timeline AS
SELECT
  c.chain_id,
  c.dog_id,
  c.start_time_utc,
  c.end_time_utc AS created_end_time_utc,
  s.block_time_utc AS settled_time_utc,
  s.winner,
  s.amount_raw,
  s.amount_display_weth,
  COALESCE(b.bid_count, 0) AS bid_count,
  COALESCE(b.unique_bidder_count, 0) AS unique_bidder_count,
  c.tx_hash AS created_tx_hash,
  s.tx_hash AS settled_tx_hash,
  CASE WHEN s.dog_id IS NULL THEN 'missing_settlement_or_unsettled' ELSE 'settled' END AS settlement_status
FROM mission1_auction_created c
LEFT JOIN mission1_auction_settled s ON s.chain_id = c.chain_id AND s.dog_id = c.dog_id
LEFT JOIN (
  SELECT chain_id, dog_id, COUNT(*) AS bid_count, COUNT(DISTINCT bidder) AS unique_bidder_count
  FROM mission1_auction_bids
  GROUP BY chain_id, dog_id
) b ON b.chain_id = c.chain_id AND b.dog_id = c.dog_id;

CREATE VIEW IF NOT EXISTS mission1_daily_activity AS
SELECT
  chain_id,
  substr(COALESCE(block_time_utc, ''), 1, 10) AS activity_date_utc,
  COUNT(*) AS bid_count,
  COUNT(DISTINCT dog_id) AS dogs_with_bids,
  COUNT(DISTINCT bidder) AS unique_bidders
FROM mission1_auction_bids
GROUP BY chain_id, activity_date_utc
ORDER BY activity_date_utc;

CREATE VIEW IF NOT EXISTS mission1_archive_metrics AS
SELECT 'auction_created_rows' AS metric, CAST(COUNT(*) AS TEXT) AS value FROM mission1_auction_created
UNION ALL SELECT 'auction_bid_rows', CAST(COUNT(*) AS TEXT) FROM mission1_auction_bids
UNION ALL SELECT 'auction_extended_rows', CAST(COUNT(*) AS TEXT) FROM mission1_auction_extended
UNION ALL SELECT 'auction_settled_rows', CAST(COUNT(*) AS TEXT) FROM mission1_auction_settled
UNION ALL SELECT 'nft_transfer_rows', CAST(COUNT(*) AS TEXT) FROM mission1_nft_transfers
UNION ALL SELECT 'bsct_transfer_rows', CAST(COUNT(*) AS TEXT) FROM mission1_bid_tokens_transfers
UNION ALL SELECT 'raw_log_rows', CAST(COUNT(*) AS TEXT) FROM mission1_raw_logs
UNION ALL SELECT 'known_gap_rows', CAST(COUNT(*) AS TEXT) FROM mission1_index_gaps;

CREATE VIEW IF NOT EXISTS mission1_reward_context AS
SELECT
  'reward_accounting_status' AS key,
  'partial: BSCT transfers captured from auction-house receipt paths; treasury/Idle/Superfluid stream flows need broader log recovery' AS value
UNION ALL SELECT 'bid_currency', 'WETH, verified by auction contract weth() and source docs/scripts'
UNION ALL SELECT 'idle_strategy', 'IdleWETH Best Yield address verified; full deposit/withdraw accounting TODO'
UNION ALL SELECT 'donation_contract', 'Ukraine.sol verified in docs; full Unchain Ukraine flow TODO';

CREATE VIEW IF NOT EXISTS mission1_future_dashboard_bridge AS
SELECT
  m.metric,
  m.value,
  'Mission 1 archive; not yet integrated into the live Mission 3 dashboard' AS dashboard_status
FROM mission1_archive_metrics m;
