DROP TABLE IF EXISTS address_labels;
CREATE TEMP TABLE address_labels AS
SELECT
  LOWER(address) AS address_lc,
  CASE
    WHEN COALESCE(username, '') != '' THEN '@' || username
    ELSE substr(LOWER(address), 1, 6) || '…' || substr(LOWER(address), -4)
  END AS label,
  CASE
    WHEN COALESCE(username, '') != '' THEN 'https://farcaster.xyz/' || username
    ELSE 'https://basescan.org/address/' || LOWER(address)
  END AS url,
  fid,
  username,
  display_name
FROM farcaster_profiles;

DROP TABLE IF EXISTS event_eth_prices;
CREATE TEMP TABLE event_eth_prices AS
SELECT
  date_utc,
  price_usd,
  CAST(price_usd AS REAL) AS price_usd_real,
  source,
  source_detail,
  COALESCE(NULLIF(confidence, ''), 'high') AS confidence,
  timestamp_utc,
  notes
FROM historical_prices_daily
WHERE UPPER(asset_key) = 'ETH'
  AND COALESCE(price_usd, '') != '';

DROP TABLE IF EXISTS recent_bids;
CREATE TABLE recent_bids AS
WITH priced_bids AS (
  SELECT
    b.*,
    hp.price_usd,
    hp.price_usd_real,
    hp.source AS price_source,
    hp.source_detail AS price_source_detail,
    hp.confidence AS price_confidence,
    hp.date_utc AS price_date_utc,
    ROW_NUMBER() OVER (
      PARTITION BY b.block_number, b.log_index, b.tx_hash
      ORDER BY
        CASE WHEN hp.price_usd_real IS NULL THEN 1 ELSE 0 END,
        CASE WHEN hp.date_utc = DATE(b.block_time_utc) THEN 0 ELSE 1 END,
        ABS(CAST(strftime('%s', COALESCE(NULLIF(hp.timestamp_utc, ''), hp.date_utc || 'T12:00:00Z')) AS INTEGER) - CAST(strftime('%s', b.block_time_utc) AS INTEGER))
    ) AS price_rank
  FROM auction_bids b
  LEFT JOIN event_eth_prices hp
    ON hp.date_utc BETWEEN DATE(b.block_time_utc, '-3 day') AND DATE(b.block_time_utc, '+3 day')
)
SELECT
  b.block_time_utc AS bid_time_utc,
  b.token_id,
  COALESCE(l.label, substr(LOWER(b.bidder), 1, 6) || '…' || substr(LOWER(b.bidder), -4)) AS bidder,
  COALESCE(NULLIF(l.url, ''), 'https://basescan.org/address/' || LOWER(b.bidder)) AS bidder_url,
  b.bidder AS bidder_wallet,
  CASE
    WHEN b.price_usd_real IS NOT NULL THEN printf('%.5f ETH ($%.0f)', b.bid_eth, b.bid_eth * b.price_usd_real)
    ELSE printf('%.5f ETH', b.bid_eth)
  END AS bid,
  ROUND(b.bid_eth, 8) AS bid_eth,
  CASE WHEN b.price_usd_real IS NOT NULL THEN ROUND(b.bid_eth * b.price_usd_real, 2) ELSE NULL END AS bid_usd,
  CASE WHEN b.price_usd_real IS NOT NULL THEN ROUND(b.bid_eth * b.price_usd_real, 2) ELSE NULL END AS bid_usd_at_event,
  b.price_usd AS eth_usd_price_at_event,
  b.price_date_utc AS eth_usd_price_date_utc,
  b.price_source AS usd_estimate_source,
  b.price_source_detail AS usd_estimate_source_detail,
  b.price_confidence AS usd_estimate_confidence,
  CASE
    WHEN b.price_usd_real IS NULL THEN 'missing_event_eth_usd'
    WHEN b.price_date_utc = DATE(b.block_time_utc) THEN 'bid_date_eth_usd'
    ELSE 'nearest_bid_date_eth_usd'
  END AS usd_estimate_basis,
  b.extended,
  b.block_number,
  b.tx_hash
FROM priced_bids b
LEFT JOIN address_labels l ON l.address_lc = LOWER(b.bidder)
WHERE b.price_rank = 1
ORDER BY b.block_number DESC, b.log_index DESC
LIMIT 100;

DROP TABLE IF EXISTS auction_winners_base;
CREATE TEMP TABLE auction_winners_base AS
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
),
priced_settlements AS (
  SELECT
    s.*,
    hp.price_usd,
    hp.price_usd_real,
    hp.source AS price_source,
    hp.source_detail AS price_source_detail,
    hp.confidence AS price_confidence,
    hp.date_utc AS price_date_utc,
    ROW_NUMBER() OVER (
      PARTITION BY s.block_number, s.tx_hash, s.token_id
      ORDER BY
        CASE WHEN hp.price_usd_real IS NULL THEN 1 ELSE 0 END,
        CASE WHEN hp.date_utc = DATE(s.block_time_utc) THEN 0 ELSE 1 END,
        ABS(CAST(strftime('%s', COALESCE(NULLIF(hp.timestamp_utc, ''), hp.date_utc || 'T12:00:00Z')) AS INTEGER) - CAST(strftime('%s', s.block_time_utc) AS INTEGER))
    ) AS price_rank
  FROM auction_settled s
  LEFT JOIN event_eth_prices hp
    ON hp.date_utc BETWEEN DATE(s.block_time_utc, '-3 day') AND DATE(s.block_time_utc, '+3 day')
)
SELECT
  s.block_time_utc AS settled_time_utc,
  s.token_id,
  d.dog_name,
  COALESCE(d.dog_image_url, '') AS dog_image_url,
  COALESCE(d.dog_external_url, '') AS dog_external_url,
  COALESCE(d.dog_opensea_url, '') AS dog_opensea_url,
  COALESCE(d.traits, '') AS traits,
  COALESCE(d.trait_rarity, '') AS trait_rarity,
  COALESCE(d.rarity, '') AS rarity,
  COALESCE(d.rarity_score, 0) AS rarity_score,
  COALESCE(d.metadata_verification_status, 'unavailable') AS metadata_verification_status,
  s.winner AS winner_wallet,
  COALESCE(l.label, substr(LOWER(s.winner), 1, 6) || '…' || substr(LOWER(s.winner), -4)) AS winner,
  COALESCE(NULLIF(l.url, ''), 'https://basescan.org/address/' || LOWER(s.winner)) AS winner_url,
  CASE
    WHEN s.price_usd_real IS NOT NULL THEN printf('%.5f ETH ($%.0f)', s.amount_eth, s.amount_eth * s.price_usd_real)
    ELSE printf('%.5f ETH', s.amount_eth)
  END AS winning_bid,
  ROUND(s.amount_eth, 8) AS winning_bid_eth,
  CASE WHEN s.price_usd_real IS NOT NULL THEN ROUND(s.amount_eth * s.price_usd_real, 2) ELSE NULL END AS winning_bid_usd,
  CASE WHEN s.price_usd_real IS NOT NULL THEN ROUND(s.amount_eth * s.price_usd_real, 2) ELSE NULL END AS winning_bid_usd_at_settlement,
  s.price_usd AS eth_usd_price_at_event,
  s.price_date_utc AS eth_usd_price_date_utc,
  s.price_source AS usd_estimate_source,
  s.price_source_detail AS usd_estimate_source_detail,
  s.price_confidence AS usd_estimate_confidence,
  CASE
    WHEN s.price_usd_real IS NULL THEN 'missing_event_eth_usd'
    WHEN s.price_date_utc = DATE(s.block_time_utc) THEN 'settlement_date_eth_usd'
    ELSE 'nearest_settlement_date_eth_usd'
  END AS usd_estimate_basis,
  COALESCE(b.bid_count, 0) AS bid_count,
  COALESCE(b.unique_bidders, 0) AS unique_bidders,
  b.first_bid_utc,
  b.last_bid_utc,
  s.block_number,
  s.tx_hash
FROM priced_settlements s
LEFT JOIN bid_counts b USING (token_id)
LEFT JOIN address_labels l ON l.address_lc = LOWER(s.winner)
LEFT JOIN dog_metadata d USING (token_id)
WHERE s.price_rank = 1
ORDER BY s.token_id DESC;

DROP TABLE IF EXISTS recent_auction_winners;
CREATE TABLE recent_auction_winners AS
SELECT
  'Dog #' || token_id AS dog,
  dog_image_url,
  dog_external_url,
  dog_opensea_url,
  winner,
  winner_url,
  winning_bid,
  winning_bid_eth,
  winning_bid_usd,
  winning_bid_usd_at_settlement,
  eth_usd_price_at_event,
  eth_usd_price_date_utc,
  usd_estimate_source,
  usd_estimate_source_detail,
  usd_estimate_confidence,
  usd_estimate_basis,
  metadata_verification_status,
  rarity,
  last_bid_utc,
  settled_time_utc
FROM auction_winners_base
ORDER BY token_id DESC
LIMIT 10;

DROP TABLE IF EXISTS auction_winners;
CREATE TABLE auction_winners AS
SELECT *
FROM auction_winners_base
ORDER BY token_id DESC;

DROP TABLE IF EXISTS effective_auction_schedule;
CREATE TEMP TABLE effective_auction_schedule AS
WITH ranked_extensions AS (
  SELECT
    e.*,
    ROW_NUMBER() OVER (
      PARTITION BY e.token_id
      ORDER BY e.block_number DESC, e.log_index DESC
    ) AS extension_rank,
    COUNT(*) OVER (PARTITION BY e.token_id) AS extension_count
  FROM auction_extensions e
)
SELECT
  c.token_id,
  c.start_time_utc,
  c.end_time_utc AS initial_end_time_utc,
  COALESCE(NULLIF(e.end_time_utc, ''), c.end_time_utc) AS end_time_utc,
  COALESCE(e.extension_count, 0) AS extension_count,
  e.end_time_utc AS latest_extension_end_time_utc,
  e.block_number AS latest_extension_block,
  e.tx_hash AS latest_extension_tx_hash,
  e.log_index AS latest_extension_log_index,
  c.block_number,
  c.tx_hash
FROM auction_created c
LEFT JOIN ranked_extensions e
  ON e.token_id = c.token_id AND e.extension_rank = 1;

DROP TABLE IF EXISTS auction_timeline;
CREATE TABLE auction_timeline AS
WITH bid_ranked AS (
  SELECT
    token_id,
    bidder,
    bid_eth,
    block_time_utc,
    block_number,
    log_index,
    ROW_NUMBER() OVER (PARTITION BY token_id ORDER BY block_number DESC, log_index DESC) AS latest_rank
  FROM auction_bids
),
bid_stats AS (
  SELECT
    token_id,
    COUNT(*) AS bids,
    COUNT(DISTINCT bidder) AS unique_bidders,
    MAX(bid_eth) AS high_bid_eth,
    SUM(bid_eth) AS total_bid_eth
  FROM auction_bids
  GROUP BY token_id
),
latest_bid AS (
  SELECT
    token_id,
    bidder AS latest_bidder_wallet,
    COALESCE(l.label, substr(LOWER(bid_ranked.bidder), 1, 6) || '…' || substr(LOWER(bid_ranked.bidder), -4)) AS latest_bidder,
    COALESCE(NULLIF(l.url, ''), 'https://basescan.org/address/' || LOWER(bid_ranked.bidder)) AS latest_bidder_url,
    bid_eth AS latest_bid_eth,
    block_time_utc AS latest_bid_utc
  FROM bid_ranked
  LEFT JOIN address_labels l ON l.address_lc = LOWER(bid_ranked.bidder)
  WHERE latest_rank = 1
),
snapshot AS (
  SELECT token_id AS current_token_id, settled AS current_settled, latest_block_time_utc
  FROM current_auction_source
  LIMIT 1
)
SELECT
  c.token_id,
  COALESCE(d.dog_image_url, '') AS dog_image_url,
  c.start_time_utc,
  c.initial_end_time_utc,
  c.end_time_utc,
  c.extension_count,
  c.latest_extension_end_time_utc,
  c.latest_extension_block,
  c.latest_extension_tx_hash,
  c.latest_extension_log_index,
  CASE
    WHEN s.token_id IS NOT NULL THEN 'settled'
    WHEN CAST(strftime('%s', c.end_time_utc) AS INTEGER) <= CAST(strftime('%s', snapshot.latest_block_time_utc) AS INTEGER) THEN 'ended_unsettled'
    WHEN c.token_id = snapshot.current_token_id AND snapshot.current_settled = 0 THEN 'live'
    ELSE 'scheduled'
  END AS auction_state,
  COALESCE(b.bids, 0) AS bids,
  COALESCE(b.unique_bidders, 0) AS unique_bidders,
  ROUND(COALESCE(b.high_bid_eth, 0), 8) AS high_bid_eth,
  ROUND(COALESCE(b.total_bid_eth, 0), 8) AS total_bid_eth,
  l.latest_bidder,
  l.latest_bidder_url,
  ROUND(COALESCE(l.latest_bid_eth, 0), 8) AS latest_bid_eth,
  l.latest_bid_utc,
  wb.winner,
  wb.winner_url,
  ROUND(COALESCE(s.amount_eth, 0), 8) AS settled_eth,
  s.block_time_utc AS settled_time_utc,
  COALESCE(d.rarity, '') AS rarity,
  COALESCE(d.metadata_verification_status, 'unavailable') AS metadata_verification_status,
  c.tx_hash AS created_tx_hash,
  s.tx_hash AS settled_tx_hash
FROM effective_auction_schedule c
CROSS JOIN snapshot
LEFT JOIN bid_stats b USING (token_id)
LEFT JOIN latest_bid l USING (token_id)
LEFT JOIN auction_settled s USING (token_id)
LEFT JOIN auction_winners_base wb USING (token_id)
LEFT JOIN dog_metadata d USING (token_id)
ORDER BY c.token_id DESC;

DROP TABLE IF EXISTS auction_daily_activity;
CREATE TABLE auction_daily_activity AS
WITH days AS (
  SELECT DATE(block_time_utc) AS activity_day FROM auction_bids WHERE block_time_utc != ''
  UNION
  SELECT DATE(block_time_utc) AS activity_day FROM auction_settled WHERE block_time_utc != ''
  UNION
  SELECT DATE(start_time_utc) AS activity_day FROM auction_created WHERE start_time_utc != ''
),
bids AS (
  SELECT
    DATE(block_time_utc) AS activity_day,
    COUNT(*) AS bids,
    COUNT(DISTINCT bidder) AS unique_bidders,
    COALESCE(SUM(bid_eth), 0) AS bid_eth,
    COALESCE(MAX(bid_eth), 0) AS high_bid_eth
  FROM auction_bids
  WHERE block_time_utc != ''
  GROUP BY DATE(block_time_utc)
),
settled AS (
  SELECT
    DATE(block_time_utc) AS activity_day,
    COUNT(*) AS settled_auctions,
    COALESCE(SUM(amount_eth), 0) AS settled_eth
  FROM auction_settled
  WHERE block_time_utc != ''
  GROUP BY DATE(block_time_utc)
),
created AS (
  SELECT
    DATE(start_time_utc) AS activity_day,
    COUNT(*) AS created_auctions
  FROM auction_created
  WHERE start_time_utc != ''
  GROUP BY DATE(start_time_utc)
)
SELECT
  d.activity_day,
  COALESCE(c.created_auctions, 0) AS created_auctions,
  COALESCE(s.settled_auctions, 0) AS settled_auctions,
  COALESCE(b.bids, 0) AS bids,
  COALESCE(b.unique_bidders, 0) AS unique_bidders,
  ROUND(COALESCE(b.bid_eth, 0), 8) AS bid_eth,
  ROUND(COALESCE(b.high_bid_eth, 0), 8) AS high_bid_eth,
  ROUND(COALESCE(s.settled_eth, 0), 8) AS settled_eth
FROM days d
LEFT JOIN created c USING (activity_day)
LEFT JOIN settled s USING (activity_day)
LEFT JOIN bids b USING (activity_day)
WHERE d.activity_day IS NOT NULL
ORDER BY d.activity_day DESC;

DROP TABLE IF EXISTS auction_bidder_leaderboard;
CREATE TABLE auction_bidder_leaderboard AS
WITH bid_ranked AS (
  SELECT
    bidder,
    token_id,
    bid_eth,
    block_time_utc,
    block_number,
    log_index,
    ROW_NUMBER() OVER (PARTITION BY bidder ORDER BY block_number DESC, log_index DESC) AS latest_rank
  FROM auction_bids
),
bidder_stats AS (
  SELECT
    bidder,
    COUNT(*) AS bids,
    COUNT(DISTINCT token_id) AS auctions_bid,
    COALESCE(SUM(bid_eth), 0) AS bid_eth,
    COALESCE(MAX(bid_eth), 0) AS high_bid_eth
  FROM auction_bids
  GROUP BY bidder
),
latest_bid AS (
  SELECT bidder, token_id AS latest_bid_token_id, block_time_utc AS latest_bid_utc
  FROM bid_ranked
  WHERE latest_rank = 1
),
winner_stats AS (
  SELECT
    winner AS bidder,
    COUNT(*) AS auction_wins,
    COALESCE(SUM(amount_eth), 0) AS winning_eth
  FROM auction_settled
  GROUP BY winner
)
SELECT
  COALESCE(lab.label, substr(LOWER(b.bidder), 1, 6) || '…' || substr(LOWER(b.bidder), -4)) AS bidder,
  COALESCE(NULLIF(lab.url, ''), 'https://basescan.org/address/' || LOWER(b.bidder)) AS bidder_url,
  b.bidder AS bidder_wallet,
  b.bids,
  b.auctions_bid,
  ROUND(b.bid_eth, 8) AS bid_eth,
  ROUND(b.high_bid_eth, 8) AS high_bid_eth,
  COALESCE(w.auction_wins, 0) AS auction_wins,
  ROUND(COALESCE(w.winning_eth, 0), 8) AS winning_eth,
  l.latest_bid_token_id,
  l.latest_bid_utc
FROM bidder_stats b
LEFT JOIN latest_bid l USING (bidder)
LEFT JOIN winner_stats w USING (bidder)
LEFT JOIN address_labels lab ON lab.address_lc = LOWER(b.bidder)
ORDER BY b.bid_eth DESC, b.bids DESC, b.bidder
LIMIT 100;

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
  COALESCE(l.label, substr(LOWER(s.winner), 1, 6) || '…' || substr(LOWER(s.winner), -4)) AS winner,
  COALESCE(NULLIF(l.url, ''), 'https://basescan.org/address/' || LOWER(s.winner)) AS winner_url,
  s.winner AS winner_wallet,
  CAST(s.auction_xp AS INTEGER) AS auction_xp,
  ROUND((1017000.0 / 69.0) * s.auction_xp / d.day_xp, 6) AS sup_reward
FROM season_wins s
JOIN daily_xp d USING (reward_day)
LEFT JOIN address_labels l ON l.address_lc = LOWER(s.winner)
ORDER BY s.reward_day DESC, s.token_id DESC;

DROP TABLE IF EXISTS season5_sup_by_winner;
CREATE TABLE season5_sup_by_winner AS
SELECT
  winner,
  winner_url,
  winner_wallet,
  COUNT(*) AS auction_wins,
  SUM(auction_xp) AS auction_xp,
  ROUND(SUM(sup_reward), 6) AS sup_reward,
  GROUP_CONCAT(token_id, ',') AS token_ids
FROM season5_sup_rewards_by_auction
GROUP BY winner, winner_url, winner_wallet
ORDER BY sup_reward DESC, auction_wins DESC, winner;

DROP TABLE IF EXISTS top_woof_holders;
CREATE TABLE top_woof_holders AS
WITH ranked AS (
  SELECT
    ROW_NUMBER() OVER (ORDER BY balance_woof DESC, address) AS rank,
    address,
    COALESCE(l.label, substr(LOWER(address), 1, 6) || '…' || substr(LOWER(address), -4)) AS holder,
    COALESCE(NULLIF(l.url, ''), 'https://basescan.org/address/' || LOWER(address)) AS holder_url,
    balance_woof,
    balance_raw,
    CASE
      WHEN (SELECT CAST(value AS REAL) FROM token_stats WHERE metric = 'woof_total_supply') > 0
      THEN balance_woof * 100.0 / (SELECT CAST(value AS REAL) FROM token_stats WHERE metric = 'woof_total_supply')
      ELSE NULL
    END AS supply_pct
  FROM woof_holders
  LEFT JOIN address_labels l ON l.address_lc = LOWER(address)
  WHERE balance_raw != '0'
)
SELECT
  rank,
  holder,
  holder_url,
  address AS holder_wallet,
  ROUND(balance_woof, 6) AS balance_woof,
  ROUND(supply_pct, 6) AS supply_pct
FROM ranked
WHERE rank <= 50
ORDER BY rank;

DROP TABLE IF EXISTS current_auction;
CREATE TABLE current_auction AS
WITH eth_price AS (
  SELECT
    COALESCE(CAST((SELECT value FROM token_stats WHERE metric = 'eth_usd_price') AS REAL), 0) AS eth_usd,
    COALESCE((SELECT value FROM token_stats WHERE metric = 'eth_usd_source'), 'unavailable') AS eth_usd_source
),
base AS (
  SELECT
    c.*,
    CASE
      WHEN c.end_time_utc = '' OR c.latest_block_time_utc = '' THEN NULL
      WHEN CAST(strftime('%s', c.end_time_utc) AS INTEGER) <= CAST(strftime('%s', c.latest_block_time_utc) AS INTEGER) THEN 0
      ELSE CAST(strftime('%s', c.end_time_utc) AS INTEGER) - CAST(strftime('%s', c.latest_block_time_utc) AS INTEGER)
    END AS seconds_left
  FROM current_auction_source c
)
SELECT
  c.token_id,
  COALESCE(d.dog_name, 'Degen Dog #' || c.token_id) AS dog_name,
  COALESCE(d.dog_image_url, '') AS dog_image_url,
  COALESCE(d.dog_external_url, '') AS dog_external_url,
  COALESCE(d.dog_opensea_url, '') AS dog_opensea_url,
  COALESCE(d.traits, '') AS traits,
  COALESCE(d.trait_rarity, '') AS trait_rarity,
  COALESCE(d.rarity, '') AS rarity,
  COALESCE(d.rarity_score, 0) AS rarity_score,
  COALESCE(d.metadata_verification_status, 'unavailable') AS metadata_verification_status,
  printf('%.5f ETH ($%.0f)', c.amount_eth, c.amount_eth * eth_price.eth_usd) AS current_bid,
  ROUND(c.amount_eth, 8) AS current_bid_eth,
  ROUND(c.amount_eth * eth_price.eth_usd, 2) AS current_bid_usd,
  ROUND(c.amount_eth * eth_price.eth_usd, 2) AS current_bid_usd_live,
  CAST(eth_price.eth_usd AS TEXT) AS eth_usd_price_live,
  DATE(COALESCE(NULLIF(c.latest_block_time_utc, ''), 'now')) AS eth_usd_price_date_utc,
  'current_eth_usd_price' AS usd_estimate_source,
  eth_price.eth_usd_source AS usd_estimate_source_detail,
  'live_current' AS usd_estimate_confidence,
  'current_eth_usd_price' AS usd_estimate_basis,
  CASE
    WHEN LOWER(c.bidder) = '0x0000000000000000000000000000000000000000' THEN 'no bids yet'
    ELSE COALESCE(l.label, substr(LOWER(c.bidder), 1, 6) || '…' || substr(LOWER(c.bidder), -4))
  END AS bidder,
  CASE
    WHEN LOWER(c.bidder) = '0x0000000000000000000000000000000000000000' THEN ''
    ELSE COALESCE(NULLIF(l.url, ''), 'https://basescan.org/address/' || LOWER(c.bidder))
  END AS bidder_url,
  c.bidder AS bidder_wallet,
  c.start_time_utc,
  c.end_time_utc,
  CASE
    WHEN c.settled != 0 THEN 'settled'
    WHEN c.seconds_left = 0 THEN 'ended_unsettled'
    ELSE 'live'
  END AS auction_state,
  c.seconds_left AS seconds_remaining,
  CASE
    WHEN c.seconds_left IS NULL THEN ''
    WHEN c.seconds_left <= 0 THEN 'ended'
    ELSE printf('%02d:%02d:%02d', c.seconds_left / 3600, (c.seconds_left / 60) % 60, c.seconds_left % 60)
  END AS time_remaining,
  c.settled,
  c.latest_block,
  c.latest_block_time_utc
FROM base c
CROSS JOIN eth_price
LEFT JOIN address_labels l ON l.address_lc = LOWER(c.bidder)
LEFT JOIN dog_metadata d USING (token_id);

DROP TABLE IF EXISTS current_auction_bid_history;
CREATE TABLE current_auction_bid_history AS
WITH eth_price AS (
  SELECT
    COALESCE((SELECT value FROM token_stats WHERE metric = 'eth_usd_price'), '0') AS eth_usd_text,
    COALESCE(CAST((SELECT value FROM token_stats WHERE metric = 'eth_usd_price') AS REAL), 0) AS eth_usd,
    COALESCE((SELECT value FROM token_stats WHERE metric = 'eth_usd_source'), 'unavailable') AS eth_usd_source
),
current_row AS (
  SELECT * FROM current_auction LIMIT 1
)
SELECT
  b.block_time_utc AS bid_time_utc,
  b.token_id,
  'Dog #' || b.token_id AS dog,
  COALESCE(l.label, substr(LOWER(b.bidder), 1, 6) || '…' || substr(LOWER(b.bidder), -4)) AS bidder,
  COALESCE(NULLIF(l.url, ''), 'https://basescan.org/address/' || LOWER(b.bidder)) AS bidder_url,
  b.bidder AS bidder_wallet,
  printf('%.5f ETH ($%.0f)', b.bid_eth, b.bid_eth * eth_price.eth_usd) AS bid,
  ROUND(b.bid_eth, 8) AS bid_eth,
  ROUND(b.bid_eth * eth_price.eth_usd, 2) AS bid_usd,
  eth_price.eth_usd_text AS eth_usd_price_live,
  DATE(COALESCE(NULLIF(c.latest_block_time_utc, ''), 'now')) AS eth_usd_price_date_utc,
  'current_eth_usd_price' AS usd_estimate_source,
  eth_price.eth_usd_source AS usd_estimate_source_detail,
  'live_current' AS usd_estimate_confidence,
  'current_auction_bid_history_live_eth_usd' AS usd_estimate_basis,
  b.extended,
  b.block_number,
  b.log_index,
  b.tx_hash
FROM auction_bids b
JOIN current_row c ON c.token_id = b.token_id
CROSS JOIN eth_price
LEFT JOIN address_labels l ON l.address_lc = LOWER(b.bidder)
ORDER BY b.block_number DESC, b.log_index DESC;

DROP TABLE IF EXISTS current_latest_bid;
CREATE TABLE current_latest_bid AS
WITH current_row AS (
  SELECT * FROM current_auction LIMIT 1
),
latest_bid AS (
  SELECT
    b.token_id,
    b.block_time_utc AS bid_time_utc,
    b.tx_hash,
    ROW_NUMBER() OVER (PARTITION BY b.token_id ORDER BY b.block_number DESC, b.log_index DESC) AS bid_rank
  FROM auction_bids b
  JOIN current_row c ON c.token_id = b.token_id
)
SELECT
  'Dog #' || c.token_id AS dog,
  c.dog_image_url,
  c.dog_external_url,
  c.dog_opensea_url,
  c.current_bid AS latest_bid,
  c.current_bid_eth AS latest_bid_eth,
  c.current_bid_usd AS latest_bid_usd,
  c.current_bid_usd_live AS latest_bid_usd_live,
  c.eth_usd_price_live,
  c.eth_usd_price_date_utc,
  c.usd_estimate_source,
  c.usd_estimate_source_detail,
  c.usd_estimate_confidence,
  c.usd_estimate_basis,
  c.bidder,
  c.bidder_url,
  c.bidder_wallet,
  COALESCE((SELECT bid_time_utc FROM latest_bid WHERE bid_rank = 1), c.latest_block_time_utc) AS bid_time_utc,
  c.auction_state,
  c.time_remaining,
  c.end_time_utc AS auction_end_utc,
  c.traits,
  c.trait_rarity,
  c.rarity,
  c.metadata_verification_status
FROM current_row c;

DROP TABLE IF EXISTS auction_feed;
CREATE TABLE auction_feed AS
WITH current_row AS (
  SELECT * FROM current_auction LIMIT 1
),
latest_bid AS (
  SELECT
    b.token_id,
    b.block_time_utc AS bid_time_utc,
    ROW_NUMBER() OVER (PARTITION BY b.token_id ORDER BY b.block_number DESC, b.log_index DESC) AS bid_rank
  FROM auction_bids b
  JOIN current_row c ON c.token_id = b.token_id
),
recent_settled AS (
  SELECT * FROM auction_winners_base ORDER BY token_id DESC LIMIT 10
),
combined AS (
  SELECT
    0 AS sort_order,
    c.token_id,
    CASE
      WHEN c.auction_state = 'live' THEN 'ongoing'
      WHEN c.auction_state = 'ended_unsettled' THEN 'ended pending settlement'
      ELSE c.auction_state
    END AS status,
    'Dog #' || c.token_id AS dog,
    c.dog_image_url,
    c.dog_external_url,
    c.dog_opensea_url,
    c.bidder AS bidder_winner,
    c.bidder_url AS bidder_winner_url,
    c.bidder_wallet AS bidder_winner_wallet,
    c.current_bid AS bid,
    c.current_bid_eth AS amount_eth,
    c.current_bid_usd AS amount_usd,
    NULL AS amount_usd_at_event,
    c.eth_usd_price_live AS eth_usd_price_live,
    NULL AS eth_usd_price_at_event,
    c.eth_usd_price_date_utc AS eth_usd_price_date_utc,
    c.usd_estimate_source AS usd_estimate_source,
    c.usd_estimate_source_detail AS usd_estimate_source_detail,
    c.usd_estimate_confidence AS usd_estimate_confidence,
    c.usd_estimate_basis AS usd_estimate_basis,
    COALESCE((SELECT bid_time_utc FROM latest_bid WHERE bid_rank = 1), c.latest_block_time_utc) AS last_bid_utc,
    '' AS settled_time_utc,
    c.time_remaining,
    c.end_time_utc AS auction_end_utc,
    c.traits,
    c.trait_rarity,
    c.rarity,
    c.rarity_score,
    c.metadata_verification_status
  FROM current_row c
  UNION ALL
  SELECT
    1 AS sort_order,
    token_id,
    'settled' AS status,
    'Dog #' || token_id AS dog,
    dog_image_url,
    dog_external_url,
    dog_opensea_url,
    winner AS bidder_winner,
    winner_url AS bidder_winner_url,
    winner_wallet AS bidder_winner_wallet,
    winning_bid AS bid,
    winning_bid_eth AS amount_eth,
    winning_bid_usd AS amount_usd,
    winning_bid_usd_at_settlement AS amount_usd_at_event,
    NULL AS eth_usd_price_live,
    eth_usd_price_at_event,
    eth_usd_price_date_utc,
    usd_estimate_source,
    usd_estimate_source_detail,
    usd_estimate_confidence,
    usd_estimate_basis,
    last_bid_utc,
    settled_time_utc,
    '' AS time_remaining,
    '' AS auction_end_utc,
    traits,
    trait_rarity,
    rarity,
    rarity_score,
    metadata_verification_status
  FROM recent_settled
)
SELECT
  status,
  dog,
  dog_image_url,
  dog_external_url,
  dog_opensea_url,
  bidder_winner,
  bidder_winner_url,
  bidder_winner_wallet,
  bid,
  amount_eth,
  amount_usd,
  amount_usd_at_event,
  eth_usd_price_live,
  eth_usd_price_at_event,
  eth_usd_price_date_utc,
  usd_estimate_source,
  usd_estimate_source_detail,
  usd_estimate_confidence,
  usd_estimate_basis,
  CASE
    WHEN status = 'settled' THEN settled_time_utc
    ELSE last_bid_utc
  END AS auction_time_utc,
  time_remaining,
  auction_end_utc,
  last_bid_utc,
  settled_time_utc,
  metadata_verification_status,
  rarity,
  traits,
  trait_rarity
FROM combined
ORDER BY sort_order ASC, token_id DESC;

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
  SELECT holder, balance_woof FROM top_woof_holders ORDER BY rank LIMIT 1
),
profile_stats AS (
  SELECT COUNT(*) AS farcaster_profiles FROM farcaster_profiles
)
SELECT 'network' AS metric, 'base' AS value
UNION ALL SELECT 'site_url', 'https://ael-dev3.github.io/Degen-Dogs-Mission-3/'
UNION ALL SELECT 'latest_block', CAST((SELECT latest_block FROM current_auction_source LIMIT 1) AS TEXT)
UNION ALL SELECT 'latest_block_time_utc', (SELECT latest_block_time_utc FROM current_auction_source LIMIT 1)
UNION ALL SELECT 'onchain_verification_status', (SELECT value FROM token_stats WHERE metric = 'onchain_verification_status')
UNION ALL SELECT 'onchain_verification_scope', (SELECT value FROM token_stats WHERE metric = 'onchain_verification_scope')
UNION ALL SELECT 'onchain_chain_id', (SELECT value FROM token_stats WHERE metric = 'onchain_chain_id')
UNION ALL SELECT 'snapshot_block_hash', (SELECT value FROM token_stats WHERE metric = 'snapshot_block_hash')
UNION ALL SELECT 'snapshot_confirmations', (SELECT value FROM token_stats WHERE metric = 'snapshot_confirmations')
UNION ALL SELECT 'rpc_quorum_size', (SELECT value FROM token_stats WHERE metric = 'rpc_quorum_size')
UNION ALL SELECT 'rpc_quorum_agreement', (SELECT value FROM token_stats WHERE metric = 'rpc_quorum_agreement')
UNION ALL SELECT 'rpc_quorum_providers', (SELECT value FROM token_stats WHERE metric = 'rpc_quorum_providers')
UNION ALL SELECT 'log_rpc_quorum_providers', (SELECT value FROM token_stats WHERE metric = 'log_rpc_quorum_providers')
UNION ALL SELECT 'auction_house_code_sha256', (SELECT value FROM token_stats WHERE metric = 'auction_house_code_sha256')
UNION ALL SELECT 'dog_nft_code_sha256', (SELECT value FROM token_stats WHERE metric = 'dog_nft_code_sha256')
UNION ALL SELECT 'auction_house', (SELECT value FROM token_stats WHERE metric = 'auction_house')
UNION ALL SELECT 'dog_nft', (SELECT value FROM token_stats WHERE metric = 'dog_nft')
UNION ALL SELECT 'dog_total_supply', (SELECT value FROM token_stats WHERE metric = 'dog_total_supply')
UNION ALL SELECT 'dog_token_uri_verification_status', (SELECT value FROM token_stats WHERE metric = 'dog_token_uri_verification_status')
UNION ALL SELECT 'dog_token_uri_present_count', (SELECT value FROM token_stats WHERE metric = 'dog_token_uri_present_count')
UNION ALL SELECT 'dog_token_uri_unavailable_count', (SELECT value FROM token_stats WHERE metric = 'dog_token_uri_unavailable_count')
UNION ALL SELECT 'dog_metadata_verification_status', (SELECT value FROM token_stats WHERE metric = 'dog_metadata_verification_status')
UNION ALL SELECT 'dog_metadata_onchain_verified_count', (SELECT value FROM token_stats WHERE metric = 'dog_metadata_onchain_verified_count')
UNION ALL SELECT 'dog_metadata_unavailable_count', (SELECT value FROM token_stats WHERE metric = 'dog_metadata_unavailable_count')
UNION ALL SELECT 'woof_token', (SELECT value FROM token_stats WHERE metric = 'woof_token')
UNION ALL SELECT 'woof_symbol', (SELECT value FROM token_stats WHERE metric = 'woof_symbol')
UNION ALL SELECT 'woof_total_supply', (SELECT value FROM token_stats WHERE metric = 'woof_total_supply')
UNION ALL SELECT 'woof_holder_verification_status', (SELECT value FROM token_stats WHERE metric = 'woof_holder_verification_status')
UNION ALL SELECT 'woof_holder_discovery_source', (SELECT value FROM token_stats WHERE metric = 'woof_holder_discovery_source')
UNION ALL SELECT 'woof_holder_candidate_count', (SELECT value FROM token_stats WHERE metric = 'woof_holder_candidate_count')
UNION ALL SELECT 'woof_holder_balance_sum_raw', (SELECT value FROM token_stats WHERE metric = 'woof_holder_balance_sum_raw')
UNION ALL SELECT 'woof_holder_expected_supply_raw', (SELECT value FROM token_stats WHERE metric = 'woof_holder_expected_supply_raw')
UNION ALL SELECT 'woof_usd_price', (SELECT value FROM token_stats WHERE metric = 'woof_usd_price')
UNION ALL SELECT 'woof_usd_source', (SELECT value FROM token_stats WHERE metric = 'woof_usd_source')
UNION ALL SELECT 'sup_token', (SELECT value FROM token_stats WHERE metric = 'sup_token')
UNION ALL SELECT 'sup_symbol', (SELECT value FROM token_stats WHERE metric = 'sup_symbol')
UNION ALL SELECT 'sup_usd_price', (SELECT value FROM token_stats WHERE metric = 'sup_usd_price')
UNION ALL SELECT 'sup_usd_source', (SELECT value FROM token_stats WHERE metric = 'sup_usd_source')
UNION ALL SELECT 'eth_usd_price', (SELECT value FROM token_stats WHERE metric = 'eth_usd_price')
UNION ALL SELECT 'eth_usd_source', (SELECT value FROM token_stats WHERE metric = 'eth_usd_source')
UNION ALL SELECT 'reward_basis_dogs', (SELECT value FROM token_stats WHERE metric = 'reward_basis_dogs')
UNION ALL SELECT 'reward_basis_source', (SELECT value FROM token_stats WHERE metric = 'reward_basis_source')
UNION ALL SELECT 'reward_snapshot_utc', (SELECT value FROM token_stats WHERE metric = 'reward_snapshot_utc')
UNION ALL SELECT 'reward_excludes', (SELECT value FROM token_stats WHERE metric = 'reward_excludes')
UNION ALL SELECT 'reward_observed_dogs_count', (SELECT value FROM token_stats WHERE metric = 'reward_observed_dogs_count')
UNION ALL SELECT 'reward_observed_woof_received', (SELECT value FROM token_stats WHERE metric = 'reward_observed_woof_received')
UNION ALL SELECT 'reward_observed_woof_flow_per_day', (SELECT value FROM token_stats WHERE metric = 'reward_observed_woof_flow_per_day')
UNION ALL SELECT 'reward_observed_woof_per_dog_per_day', (SELECT value FROM token_stats WHERE metric = 'reward_observed_woof_per_dog_per_day')
UNION ALL SELECT 'reward_observed_sup_received', (SELECT value FROM token_stats WHERE metric = 'reward_observed_sup_received')
UNION ALL SELECT 'reward_observed_sup_flow_per_day', (SELECT value FROM token_stats WHERE metric = 'reward_observed_sup_flow_per_day')
UNION ALL SELECT 'reward_observed_sup_per_dog_per_day', (SELECT value FROM token_stats WHERE metric = 'reward_observed_sup_per_dog_per_day')
UNION ALL SELECT 'reward_basis_note', (SELECT value FROM token_stats WHERE metric = 'reward_basis_note')
UNION ALL SELECT 'reward_woof_received', (SELECT value FROM token_stats WHERE metric = 'reward_woof_received')
UNION ALL SELECT 'reward_woof_received_usd', (SELECT value FROM token_stats WHERE metric = 'reward_woof_received_usd')
UNION ALL SELECT 'reward_woof_flow_per_day', (SELECT value FROM token_stats WHERE metric = 'reward_woof_flow_per_day')
UNION ALL SELECT 'reward_woof_flow_usd_per_day', (SELECT value FROM token_stats WHERE metric = 'reward_woof_flow_usd_per_day')
UNION ALL SELECT 'reward_woof_per_dog_per_day', (SELECT value FROM token_stats WHERE metric = 'reward_woof_per_dog_per_day')
UNION ALL SELECT 'reward_woof_per_dog_usd_per_day', (SELECT value FROM token_stats WHERE metric = 'reward_woof_per_dog_usd_per_day')
UNION ALL SELECT 'reward_sup_received', (SELECT value FROM token_stats WHERE metric = 'reward_sup_received')
UNION ALL SELECT 'reward_sup_received_usd', (SELECT value FROM token_stats WHERE metric = 'reward_sup_received_usd')
UNION ALL SELECT 'reward_sup_flow_per_day', (SELECT value FROM token_stats WHERE metric = 'reward_sup_flow_per_day')
UNION ALL SELECT 'reward_sup_flow_usd_per_day', (SELECT value FROM token_stats WHERE metric = 'reward_sup_flow_usd_per_day')
UNION ALL SELECT 'reward_sup_per_dog_per_day', (SELECT value FROM token_stats WHERE metric = 'reward_sup_per_dog_per_day')
UNION ALL SELECT 'reward_sup_per_dog_usd_per_day', (SELECT value FROM token_stats WHERE metric = 'reward_sup_per_dog_usd_per_day')
UNION ALL SELECT 'reward_total_flow_usd_per_day', (SELECT value FROM token_stats WHERE metric = 'reward_total_flow_usd_per_day')
UNION ALL SELECT 'reward_total_per_dog_usd_per_day', (SELECT value FROM token_stats WHERE metric = 'reward_total_per_dog_usd_per_day')
UNION ALL SELECT 'reward_current_bid_payback_days', COALESCE((SELECT value FROM token_stats WHERE metric = 'reward_current_bid_payback_days'), 'N/A')
UNION ALL SELECT 'reward_current_bid_daily_roi_pct', COALESCE((SELECT value FROM token_stats WHERE metric = 'reward_current_bid_daily_roi_pct'), 'N/A')
UNION ALL SELECT 'reward_current_bid_apr_pct', COALESCE((SELECT value FROM token_stats WHERE metric = 'reward_current_bid_apr_pct'), 'N/A')
UNION ALL SELECT 'reward_current_bid_apr_display', COALESCE((SELECT value FROM token_stats WHERE metric = 'reward_current_bid_apr_display'), 'N/A')
UNION ALL SELECT 'woof_holders', CASE
  WHEN (SELECT value FROM token_stats WHERE metric = 'woof_holder_verification_status') = 'candidate_complete_onchain_quorum_verified'
  THEN CAST((SELECT woof_holders FROM holder_stats) AS TEXT)
  ELSE 'unavailable'
END
UNION ALL SELECT 'top_woof_holder', COALESCE((SELECT holder FROM top_holder), '')
UNION ALL SELECT 'top_woof_holder_balance', CASE
  WHEN (SELECT value FROM token_stats WHERE metric = 'woof_holder_verification_status') = 'candidate_complete_onchain_quorum_verified'
  THEN CAST(ROUND(COALESCE((SELECT balance_woof FROM top_holder), 0), 6) AS TEXT)
  ELSE 'unavailable'
END
UNION ALL SELECT 'farcaster_profiles_resolved', CAST((SELECT farcaster_profiles FROM profile_stats) AS TEXT)
UNION ALL SELECT 'current_auction_token_id', CAST((SELECT token_id FROM current_auction_source LIMIT 1) AS TEXT)
UNION ALL SELECT 'current_auction_status', (SELECT auction_state FROM current_auction LIMIT 1)
UNION ALL SELECT 'current_bid_eth', CAST(ROUND((SELECT amount_eth FROM current_auction_source LIMIT 1), 8) AS TEXT)
UNION ALL SELECT 'current_bid_usd', CAST(ROUND((SELECT current_bid_usd FROM current_auction LIMIT 1), 2) AS TEXT)
UNION ALL SELECT 'current_bidder', (SELECT bidder FROM current_auction LIMIT 1)
UNION ALL SELECT 'current_bidder_wallet', (SELECT bidder_wallet FROM current_auction LIMIT 1)
UNION ALL SELECT 'current_auction_end_utc', (SELECT end_time_utc FROM current_auction_source LIMIT 1)
UNION ALL SELECT 'created_auctions', CAST((SELECT created_auctions FROM created_stats) AS TEXT)
UNION ALL SELECT 'settled_auctions', CAST((SELECT settled_auctions FROM settle_stats) AS TEXT)
UNION ALL SELECT 'total_bid_eth', CAST(ROUND((SELECT total_bid_eth FROM bid_stats), 8) AS TEXT)
UNION ALL SELECT 'total_settled_eth', CAST(ROUND((SELECT total_settled_eth FROM settle_stats), 8) AS TEXT)
UNION ALL SELECT 'highest_bid_eth', CAST(ROUND((SELECT highest_bid_eth FROM bid_stats), 8) AS TEXT)
UNION ALL SELECT 'season5_reward_total_sup', '1017000'
UNION ALL SELECT 'season5_rewards_allocated_sup', CAST(ROUND((SELECT allocated_sup FROM season_stats), 6) AS TEXT);
