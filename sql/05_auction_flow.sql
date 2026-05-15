-- Degen Dogs Mission 3: auction flow on Base
-- Replace {{auction_house_address}} with the confirmed Mission 3 Base auction contract.
-- Use sql/06_contract_discovery.sql to identify candidate auction event emitters; do not use stale Polygon addresses.

WITH params AS (
    SELECT
        {{auction_house_address}} AS auction_house,
        0xd6eddd1118d71820909c1197aa966dbc15ed6f508554252169cc3d5ccac756ca AS auction_created_topic,
        0x1159164c56f277e6fc99c11731bd380e0347deb969b75523398734c252706ea3 AS auction_bid_topic,
        0x6e912a3a9105bdd2af817ba5adc14e6c127c1035b5b648faa29ca0d58ab8ff4e AS auction_extended_topic,
        0xc9f72b276a388619c6d185d146697036241880c36654b1a3ffdad07c24038d99 AS auction_settled_topic
),
logs AS (
    SELECT *
    FROM base.logs
    WHERE contract_address = (SELECT auction_house FROM params)
      AND topic0 IN (
        (SELECT auction_created_topic FROM params),
        (SELECT auction_bid_topic FROM params),
        (SELECT auction_extended_topic FROM params),
        (SELECT auction_settled_topic FROM params)
      )
),
created AS (
    SELECT
        block_time,
        tx_hash,
        'created' AS event_type,
        bytearray_to_uint256(topic1) AS dog_id,
        CAST(NULL AS varbinary) AS bidder_or_winner,
        CAST(NULL AS DOUBLE) AS bid_eth,
        from_unixtime(bytearray_to_uint256(bytearray_substring(data, 1, 32))) AS start_time,
        from_unixtime(bytearray_to_uint256(bytearray_substring(data, 33, 32))) AS end_time
    FROM logs
    WHERE topic0 = (SELECT auction_created_topic FROM params)
),
bids AS (
    SELECT
        block_time,
        tx_hash,
        'bid' AS event_type,
        bytearray_to_uint256(topic1) AS dog_id,
        bytearray_substring(data, 13, 20) AS bidder_or_winner,
        CAST(bytearray_to_uint256(bytearray_substring(data, 33, 32)) AS DOUBLE) / 1e18 AS bid_eth,
        CAST(NULL AS timestamp) AS start_time,
        CAST(NULL AS timestamp) AS end_time
    FROM logs
    WHERE topic0 = (SELECT auction_bid_topic FROM params)
),
extended AS (
    SELECT
        block_time,
        tx_hash,
        'extended' AS event_type,
        bytearray_to_uint256(topic1) AS dog_id,
        CAST(NULL AS varbinary) AS bidder_or_winner,
        CAST(NULL AS DOUBLE) AS bid_eth,
        CAST(NULL AS timestamp) AS start_time,
        from_unixtime(bytearray_to_uint256(bytearray_substring(data, 1, 32))) AS end_time
    FROM logs
    WHERE topic0 = (SELECT auction_extended_topic FROM params)
),
settled AS (
    SELECT
        block_time,
        tx_hash,
        'settled' AS event_type,
        bytearray_to_uint256(topic1) AS dog_id,
        bytearray_substring(data, 13, 20) AS bidder_or_winner,
        CAST(bytearray_to_uint256(bytearray_substring(data, 33, 32)) AS DOUBLE) / 1e18 AS bid_eth,
        CAST(NULL AS timestamp) AS start_time,
        CAST(NULL AS timestamp) AS end_time
    FROM logs
    WHERE topic0 = (SELECT auction_settled_topic FROM params)
)
SELECT * FROM created
UNION ALL
SELECT * FROM bids
UNION ALL
SELECT * FROM extended
UNION ALL
SELECT * FROM settled
ORDER BY block_time DESC;
