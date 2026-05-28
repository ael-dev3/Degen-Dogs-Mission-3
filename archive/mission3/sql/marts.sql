DROP VIEW IF EXISTS mission3_auction_winners;
CREATE VIEW mission3_auction_winners AS
WITH bid_counts AS (
  SELECT
    token_id,
    COUNT(*) AS bid_count,
    COUNT(DISTINCT LOWER(bidder)) AS unique_bidder_count,
    MIN(block_time_utc) AS first_bid_time_utc,
    MAX(block_time_utc) AS last_bid_time_utc
  FROM mission3_auction_bids
  GROUP BY token_id
)
SELECT
  s.token_id,
  s.winner,
  s.amount_raw,
  s.amount_eth,
  s.block_number AS settled_block,
  s.transaction_hash AS settled_tx,
  s.log_index AS settled_log_index,
  s.block_time_utc AS settled_time_utc,
  COALESCE(b.bid_count, 0) AS bid_count,
  COALESCE(b.unique_bidder_count, 0) AS unique_bidder_count,
  b.first_bid_time_utc,
  b.last_bid_time_utc
FROM mission3_auction_settled s
LEFT JOIN bid_counts b USING (token_id)
ORDER BY s.token_id DESC;

DROP VIEW IF EXISTS mission3_recent_bids;
CREATE VIEW mission3_recent_bids AS
SELECT
  token_id,
  bidder,
  amount_raw,
  amount_eth,
  extended,
  block_number,
  transaction_hash,
  log_index,
  block_time_utc
FROM mission3_auction_bids
ORDER BY block_number DESC, log_index DESC;

DROP VIEW IF EXISTS mission3_bidder_leaderboard;
CREATE VIEW mission3_bidder_leaderboard AS
WITH bidder_stats AS (
  SELECT
    LOWER(bidder) AS bidder,
    COUNT(*) AS bids,
    COUNT(DISTINCT token_id) AS auctions_bid,
    SUM(CAST(amount_raw AS REAL) / 1000000000000000000.0) AS bid_eth,
    MAX(CAST(amount_raw AS REAL) / 1000000000000000000.0) AS high_bid_eth,
    MAX(block_time_utc) AS latest_bid_time_utc
  FROM mission3_auction_bids
  GROUP BY LOWER(bidder)
),
winner_stats AS (
  SELECT
    LOWER(winner) AS bidder,
    COUNT(*) AS auction_wins,
    SUM(CAST(amount_raw AS REAL) / 1000000000000000000.0) AS winning_eth
  FROM mission3_auction_settled
  GROUP BY LOWER(winner)
)
SELECT
  b.bidder,
  b.bids,
  b.auctions_bid,
  ROUND(b.bid_eth, 8) AS bid_eth,
  ROUND(b.high_bid_eth, 8) AS high_bid_eth,
  COALESCE(w.auction_wins, 0) AS auction_wins,
  ROUND(COALESCE(w.winning_eth, 0), 8) AS winning_eth,
  b.latest_bid_time_utc
FROM bidder_stats b
LEFT JOIN winner_stats w USING (bidder)
ORDER BY b.bid_eth DESC, b.bids DESC, b.bidder;

DROP VIEW IF EXISTS mission3_auction_timeline;
CREATE VIEW mission3_auction_timeline AS
WITH bid_stats AS (
  SELECT
    token_id,
    COUNT(*) AS bids,
    COUNT(DISTINCT LOWER(bidder)) AS unique_bidder_count,
    MAX(CAST(amount_raw AS REAL) / 1000000000000000000.0) AS high_bid_eth,
    MAX(block_time_utc) AS latest_bid_time_utc
  FROM mission3_auction_bids
  GROUP BY token_id
),
latest_bid AS (
  SELECT token_id, bidder AS latest_bidder, amount_raw AS latest_bid_raw, amount_eth AS latest_bid_eth
  FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY token_id ORDER BY block_number DESC, log_index DESC) AS rn
    FROM mission3_auction_bids
  )
  WHERE rn = 1
)
SELECT
  c.token_id,
  c.start_time,
  datetime(c.start_time, 'unixepoch') AS start_time_utc,
  c.end_time,
  datetime(c.end_time, 'unixepoch') AS end_time_utc,
  c.block_number AS created_block,
  c.transaction_hash AS created_tx,
  c.block_time_utc AS created_block_time_utc,
  COALESCE(b.bids, 0) AS bids,
  COALESCE(b.unique_bidder_count, 0) AS unique_bidder_count,
  ROUND(COALESCE(b.high_bid_eth, 0), 8) AS high_bid_eth,
  lb.latest_bidder,
  lb.latest_bid_raw,
  lb.latest_bid_eth,
  b.latest_bid_time_utc,
  s.winner,
  s.amount_raw AS settled_amount_raw,
  s.amount_eth AS settled_amount_eth,
  s.block_number AS settled_block,
  s.transaction_hash AS settled_tx,
  s.block_time_utc AS settled_time_utc,
  CASE WHEN s.transaction_hash IS NOT NULL THEN 'settled' ELSE 'unsettled_or_live' END AS auction_state
FROM mission3_auction_created c
LEFT JOIN bid_stats b USING (token_id)
LEFT JOIN latest_bid lb USING (token_id)
LEFT JOIN mission3_auction_settled s USING (token_id)
ORDER BY c.token_id DESC;

DROP VIEW IF EXISTS mission3_daily_activity;
CREATE VIEW mission3_daily_activity AS
WITH days AS (
  SELECT date(block_time_utc) AS activity_day FROM mission3_auction_created WHERE block_time_utc IS NOT NULL
  UNION
  SELECT date(block_time_utc) AS activity_day FROM mission3_auction_bids WHERE block_time_utc IS NOT NULL
  UNION
  SELECT date(block_time_utc) AS activity_day FROM mission3_auction_settled WHERE block_time_utc IS NOT NULL
),
created AS (
  SELECT date(block_time_utc) AS activity_day, COUNT(*) AS created_auctions FROM mission3_auction_created GROUP BY date(block_time_utc)
),
bids AS (
  SELECT date(block_time_utc) AS activity_day, COUNT(*) AS bids, COUNT(DISTINCT LOWER(bidder)) AS unique_bidders, SUM(CAST(amount_raw AS REAL) / 1000000000000000000.0) AS bid_eth FROM mission3_auction_bids GROUP BY date(block_time_utc)
),
settled AS (
  SELECT date(block_time_utc) AS activity_day, COUNT(*) AS settled_auctions, SUM(CAST(amount_raw AS REAL) / 1000000000000000000.0) AS settled_eth FROM mission3_auction_settled GROUP BY date(block_time_utc)
)
SELECT
  d.activity_day,
  COALESCE(c.created_auctions, 0) AS created_auctions,
  COALESCE(b.bids, 0) AS bids,
  COALESCE(b.unique_bidders, 0) AS unique_bidders,
  ROUND(COALESCE(b.bid_eth, 0), 8) AS bid_eth,
  COALESCE(s.settled_auctions, 0) AS settled_auctions,
  ROUND(COALESCE(s.settled_eth, 0), 8) AS settled_eth
FROM days d
LEFT JOIN created c USING (activity_day)
LEFT JOIN bids b USING (activity_day)
LEFT JOIN settled s USING (activity_day)
WHERE d.activity_day IS NOT NULL
ORDER BY d.activity_day DESC;

DROP VIEW IF EXISTS mission3_dog_search_index;
CREATE VIEW mission3_dog_search_index AS
SELECT
  3 AS mission,
  'Base' AS chain,
  8453 AS chain_id,
  t.token_id,
  t.created_block AS auction_created_block,
  t.created_tx AS auction_created_tx,
  t.created_block_time_utc AS auction_created_time_utc,
  CASE WHEN t.auction_state = 'settled' THEN 1 ELSE 0 END AS settled,
  t.settled_block,
  t.settled_tx,
  t.settled_time_utc,
  t.winner,
  t.settled_amount_raw AS amount_raw,
  t.settled_amount_eth AS amount_eth,
  t.bids AS bid_count,
  t.unique_bidder_count,
  'https://opensea.io/item/base/0x09154248ffdbaf8aa877ae8a4bf8ce1503596428/' || t.token_id AS opensea_url,
  'verified' AS confidence,
  'base_logs,archive_indexer' AS sources
FROM mission3_auction_timeline t;

DROP VIEW IF EXISTS mission3_archive_metrics;
CREATE VIEW mission3_archive_metrics AS
SELECT 'raw_logs' AS metric, CAST(COUNT(*) AS TEXT) AS value FROM mission3_raw_logs
UNION ALL SELECT 'auctions_created', CAST(COUNT(*) AS TEXT) FROM mission3_auction_created
UNION ALL SELECT 'bids', CAST(COUNT(*) AS TEXT) FROM mission3_auction_bids
UNION ALL SELECT 'extensions', CAST(COUNT(*) AS TEXT) FROM mission3_auction_extended
UNION ALL SELECT 'settlements', CAST(COUNT(*) AS TEXT) FROM mission3_auction_settled
UNION ALL SELECT 'current_snapshots', CAST(COUNT(*) AS TEXT) FROM mission3_current_auction_snapshots
UNION ALL SELECT 'latest_indexed_block', COALESCE(CAST((SELECT latest_indexed_block FROM mission3_index_state WHERE id = 'mission3' LIMIT 1) AS TEXT), '')
UNION ALL SELECT 'latest_indexed_block_time_utc', COALESCE((SELECT latest_indexed_block_time_utc FROM mission3_index_state WHERE id = 'mission3' LIMIT 1), '')
UNION ALL SELECT 'latest_run_at_utc', COALESCE((SELECT latest_run_at_utc FROM mission3_index_state WHERE id = 'mission3' LIMIT 1), '')
UNION ALL SELECT 'status', COALESCE((SELECT status FROM mission3_index_state WHERE id = 'mission3' LIMIT 1), 'not_run')
UNION ALL SELECT 'unresolved_gaps', CAST((SELECT COUNT(*) FROM mission3_index_gaps WHERE status != 'resolved') AS TEXT);
