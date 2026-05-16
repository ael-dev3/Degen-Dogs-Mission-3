DROP TABLE IF EXISTS recent_bids;
CREATE TABLE recent_bids AS
SELECT
  block_time_utc AS bid_time_utc,
  token_id,
  bidder,
  ROUND(bid_eth, 8) AS bid_eth,
  extended,
  block_number,
  tx_hash
FROM auction_bids
ORDER BY block_number DESC, log_index DESC
LIMIT 100;

DROP TABLE IF EXISTS auction_winners;
CREATE TABLE auction_winners AS
WITH bid_counts AS (
  SELECT
    token_id,
    COUNT(*) AS bid_count,
    COUNT(DISTINCT bidder) AS unique_bidders,
    MIN(block_time_utc) AS first_bid_utc,
    MAX(block_time_utc) AS last_bid_utc,
    MAX(bid_eth) AS max_seen_bid_eth
  FROM auction_bids
  GROUP BY token_id
)
SELECT
  s.block_time_utc AS settled_time_utc,
  s.token_id,
  s.winner,
  ROUND(s.amount_eth, 8) AS winning_bid_eth,
  COALESCE(b.bid_count, 0) AS bid_count,
  COALESCE(b.unique_bidders, 0) AS unique_bidders,
  b.first_bid_utc,
  b.last_bid_utc,
  s.block_number,
  s.tx_hash
FROM auction_settled s
LEFT JOIN bid_counts b USING (token_id)
ORDER BY s.token_id DESC;

DROP TABLE IF EXISTS season5_sup_rewards_by_auction;
CREATE TABLE season5_sup_rewards_by_auction AS
WITH season_wins AS (
  SELECT
    token_id,
    winner,
    block_time_utc AS settled_time_utc,
    DATE(block_time_utc) AS reward_day,
    100.0 AS auction_xp
  FROM auction_settled
  WHERE DATE(block_time_utc) BETWEEN '2026-03-25' AND '2026-06-01'
),
daily_xp AS (
  SELECT reward_day, SUM(auction_xp) AS day_xp
  FROM season_wins
  GROUP BY reward_day
)
SELECT
  s.reward_day,
  s.settled_time_utc,
  s.token_id,
  s.winner,
  CAST(s.auction_xp AS INTEGER) AS auction_xp,
  ROUND((1017000.0 / 69.0) * s.auction_xp / d.day_xp, 6) AS sup_reward
FROM season_wins s
JOIN daily_xp d USING (reward_day)
ORDER BY s.reward_day DESC, s.token_id DESC;

DROP TABLE IF EXISTS season5_sup_by_winner;
CREATE TABLE season5_sup_by_winner AS
SELECT
  winner,
  COUNT(*) AS auction_wins,
  SUM(auction_xp) AS auction_xp,
  ROUND(SUM(sup_reward), 6) AS sup_reward,
  GROUP_CONCAT(token_id, ',') AS token_ids
FROM season5_sup_rewards_by_auction
GROUP BY winner
ORDER BY sup_reward DESC, auction_wins DESC, winner;

DROP TABLE IF EXISTS top_woof_holders;
CREATE TABLE top_woof_holders AS
WITH ranked AS (
  SELECT
    ROW_NUMBER() OVER (ORDER BY balance_woof DESC, address) AS rank,
    address,
    balance_woof,
    balance_raw,
    CASE
      WHEN (SELECT CAST(value AS REAL) FROM token_stats WHERE metric = 'woof_total_supply') > 0
      THEN balance_woof * 100.0 / (SELECT CAST(value AS REAL) FROM token_stats WHERE metric = 'woof_total_supply')
      ELSE NULL
    END AS supply_pct
  FROM woof_holders
  WHERE balance_raw != '0'
)
SELECT
  rank,
  address,
  ROUND(balance_woof, 6) AS balance_woof,
  ROUND(supply_pct, 6) AS supply_pct
FROM ranked
WHERE rank <= 50
ORDER BY rank;

DROP TABLE IF EXISTS current_auction;
CREATE TABLE current_auction AS
SELECT
  token_id,
  ROUND(amount_eth, 8) AS current_bid_eth,
  bidder,
  start_time_utc,
  end_time_utc,
  settled,
  latest_block,
  latest_block_time_utc
FROM current_auction_source;

DROP TABLE IF EXISTS mission3_metrics;
CREATE TABLE mission3_metrics AS
WITH
bid_stats AS (
  SELECT
    COUNT(*) AS total_bids,
    COUNT(DISTINCT bidder) AS unique_bidders,
    COALESCE(SUM(bid_eth), 0) AS total_bid_eth,
    COALESCE(MAX(bid_eth), 0) AS highest_bid_eth
  FROM auction_bids
),
settle_stats AS (
  SELECT
    COUNT(*) AS settled_auctions,
    COALESCE(SUM(amount_eth), 0) AS total_settled_eth
  FROM auction_settled
),
created_stats AS (
  SELECT COUNT(*) AS created_auctions FROM auction_created
),
season_stats AS (
  SELECT COALESCE(SUM(sup_reward), 0) AS allocated_sup FROM season5_sup_rewards_by_auction
),
holder_stats AS (
  SELECT COUNT(*) AS woof_holders FROM woof_holders WHERE balance_raw != '0'
),
top_holder AS (
  SELECT address, balance_woof FROM woof_holders WHERE balance_raw != '0' ORDER BY balance_woof DESC LIMIT 1
)
SELECT 'network' AS metric, 'base' AS value
UNION ALL SELECT 'latest_block', CAST((SELECT latest_block FROM current_auction_source LIMIT 1) AS TEXT)
UNION ALL SELECT 'latest_block_time_utc', (SELECT latest_block_time_utc FROM current_auction_source LIMIT 1)
UNION ALL SELECT 'auction_house', (SELECT value FROM token_stats WHERE metric = 'auction_house')
UNION ALL SELECT 'dog_nft', (SELECT value FROM token_stats WHERE metric = 'dog_nft')
UNION ALL SELECT 'woof_token', (SELECT value FROM token_stats WHERE metric = 'woof_token')
UNION ALL SELECT 'woof_symbol', (SELECT value FROM token_stats WHERE metric = 'woof_symbol')
UNION ALL SELECT 'woof_total_supply', (SELECT value FROM token_stats WHERE metric = 'woof_total_supply')
UNION ALL SELECT 'woof_holders', CAST((SELECT woof_holders FROM holder_stats) AS TEXT)
UNION ALL SELECT 'top_woof_holder', COALESCE((SELECT address FROM top_holder), '')
UNION ALL SELECT 'top_woof_holder_balance', CAST(ROUND(COALESCE((SELECT balance_woof FROM top_holder), 0), 6) AS TEXT)
UNION ALL SELECT 'current_auction_token_id', CAST((SELECT token_id FROM current_auction_source LIMIT 1) AS TEXT)
UNION ALL SELECT 'current_bid_eth', CAST(ROUND((SELECT amount_eth FROM current_auction_source LIMIT 1), 8) AS TEXT)
UNION ALL SELECT 'current_bidder', (SELECT bidder FROM current_auction_source LIMIT 1)
UNION ALL SELECT 'current_auction_end_utc', (SELECT end_time_utc FROM current_auction_source LIMIT 1)
UNION ALL SELECT 'created_auctions', CAST((SELECT created_auctions FROM created_stats) AS TEXT)
UNION ALL SELECT 'settled_auctions', CAST((SELECT settled_auctions FROM settle_stats) AS TEXT)
UNION ALL SELECT 'total_bids', CAST((SELECT total_bids FROM bid_stats) AS TEXT)
UNION ALL SELECT 'unique_bidders', CAST((SELECT unique_bidders FROM bid_stats) AS TEXT)
UNION ALL SELECT 'total_bid_eth', CAST(ROUND((SELECT total_bid_eth FROM bid_stats), 8) AS TEXT)
UNION ALL SELECT 'total_settled_eth', CAST(ROUND((SELECT total_settled_eth FROM settle_stats), 8) AS TEXT)
UNION ALL SELECT 'highest_bid_eth', CAST(ROUND((SELECT highest_bid_eth FROM bid_stats), 8) AS TEXT)
UNION ALL SELECT 'season5_reward_total_sup', '1017000'
UNION ALL SELECT 'season5_rewards_allocated_sup', CAST(ROUND((SELECT allocated_sup FROM season_stats), 6) AS TEXT);
