-- Degen Dogs Mission 3: Base contract discovery helper
-- Purpose: identify candidate Dog NFT and auction-house contracts before hard-coding address-dependent panels.
-- Run this in Dune, review candidates manually, then set {{dog_nft_address}} and {{auction_house_address}}.

WITH auction_topics AS (
    SELECT
        0xd6eddd1118d71820909c1197aa966dbc15ed6f508554252169cc3d5ccac756ca AS auction_created_topic,
        0x1159164c56f277e6fc99c11731bd380e0347deb969b75523398734c252706ea3 AS auction_bid_topic,
        0x6e912a3a9105bdd2af817ba5adc14e6c127c1035b5b648faa29ca0d58ab8ff4e AS auction_extended_topic,
        0xc9f72b276a388619c6d185d146697036241880c36654b1a3ffdad07c24038d99 AS auction_settled_topic
),
nft_transfer_candidates AS (
    SELECT
        contract_address,
        collection,
        COUNT(*) AS transfer_events,
        COUNT(DISTINCT token_id) AS token_ids,
        MIN(block_time) AS first_seen,
        MAX(block_time) AS last_seen
    FROM nft.transfers
    WHERE blockchain = 'base'
      AND block_time >= TIMESTAMP '2026-01-01'
      AND (
        lower(collection) LIKE '%degen%dog%'
        OR lower(collection) LIKE '%degen dogs%'
      )
    GROUP BY 1, 2
),
nft_trade_candidates AS (
    SELECT
        nft_contract_address AS contract_address,
        collection,
        COUNT(*) AS trade_count,
        SUM(amount_usd) AS trade_volume_usd,
        MIN(block_time) AS first_trade,
        MAX(block_time) AS last_trade
    FROM nft.trades
    WHERE blockchain = 'base'
      AND block_time >= TIMESTAMP '2026-01-01'
      AND (
        lower(collection) LIKE '%degen%dog%'
        OR lower(collection) LIKE '%degen dogs%'
      )
    GROUP BY 1, 2
),
nft_candidates AS (
    SELECT
        'dog_nft_candidate' AS candidate_type,
        COALESCE(n.contract_address, t.contract_address) AS contract_address,
        COALESCE(n.collection, t.collection) AS label,
        COALESCE(n.transfer_events, 0) + COALESCE(t.trade_count, 0) AS evidence_count,
        COALESCE(n.token_ids, 0) AS secondary_count,
        t.trade_volume_usd AS volume_usd,
        COALESCE(LEAST(n.first_seen, t.first_trade), n.first_seen, t.first_trade) AS first_seen,
        COALESCE(GREATEST(n.last_seen, t.last_trade), n.last_seen, t.last_trade) AS last_seen,
        'Matched Base NFT transfer/trade collection name. Review before setting {{dog_nft_address}}.' AS note
    FROM nft_transfer_candidates n
    FULL OUTER JOIN nft_trade_candidates t
      ON n.contract_address = t.contract_address
),
auction_candidates AS (
    SELECT
        'auction_house_candidate' AS candidate_type,
        contract_address,
        'Nouns-style auction event emitter' AS label,
        COUNT(*) AS evidence_count,
        COUNT_IF(topic0 = (SELECT auction_bid_topic FROM auction_topics)) AS secondary_count,
        CAST(NULL AS DOUBLE) AS volume_usd,
        MIN(block_time) AS first_seen,
        MAX(block_time) AS last_seen,
        'Matched AuctionCreated/AuctionBid/AuctionExtended/AuctionSettled topic signatures on Base. Review event payloads before setting {{auction_house_address}}.' AS note
    FROM base.logs
    WHERE block_time >= TIMESTAMP '2026-01-01'
      AND topic0 IN (
        (SELECT auction_created_topic FROM auction_topics),
        (SELECT auction_bid_topic FROM auction_topics),
        (SELECT auction_extended_topic FROM auction_topics),
        (SELECT auction_settled_topic FROM auction_topics)
      )
    GROUP BY 1, 2, 3, 6, 9
    HAVING COUNT(*) >= 2
)
SELECT * FROM nft_candidates
UNION ALL
SELECT * FROM auction_candidates
ORDER BY candidate_type, evidence_count DESC, last_seen DESC;
