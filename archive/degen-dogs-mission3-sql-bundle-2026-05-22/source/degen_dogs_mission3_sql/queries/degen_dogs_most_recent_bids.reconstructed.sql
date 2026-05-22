-- Degen Dogs - Most Recent Bids / DegenDogs Latest Auctions
-- Dashboard card title found publicly: "DegenDogs Latest Auctions".
-- Dune Discover snippet found publicly: "Degen Dogs - Most Recent Bids" by @ael_dev.
-- Status: reconstructed companion query; no official query id or full SQL was exposed in public snippets.
-- Fixed: patched for current Mission 3 Base auction house after Base RPC verification.
-- Notes:
--   * Original public snippets used contract_address 0x3620CA030a023BCE87EC59a8b0E979bD7607Fdbd.
--   * That original snippet address has no code on Base and no recent AuctionBid logs.
--   * Current Mission 3 auction house verified on Base: 0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA.
--   * ETH amounts keep exact wei as amount_wei and fractional ETH as amount_eth.
--   * Verify against the dashboard's query id if exact official Dune SQL is required.

WITH all_bids AS (
    SELECT
        block_time,
        tx_hash,
        bytearray_to_uint256(topic1) AS token_id,
        bytearray_to_uint256(bytearray_substring(data, 33, 32)) AS amount_wei,
        CAST(
            bytearray_to_uint256(bytearray_substring(data, 33, 32))
            AS DOUBLE
        ) / 1e18 AS amount_eth,
        lower(concat('0x', to_hex(bytearray_substring(data, 13, 20)))) AS bidder_address
    FROM base.logs
    WHERE contract_address = 0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA
      AND topic0 = 0x1159164c56f277e6fc99c11731bd380e0347deb969b75523398734c252706ea3
),

farcaster_identities AS (
    SELECT
        lower(trim(address_str)) AS wallet_address,
        arbitrary(fname) AS fname
    FROM dune.neynar.dataset_farcaster_profile_with_addresses
    CROSS JOIN UNNEST(
        split(
            replace(
                replace(
                    replace(
                        replace(verified_addresses, '[', ''),
                        ']',
                        ''
                    ),
                    '"',
                    ''
                ),
                ' ',
                ''
            ),
            ','
        )
    ) AS t(address_str)
    WHERE address_str IS NOT NULL
      AND length(address_str) > 0
    GROUP BY 1
)

SELECT
    b.block_time,
    b.token_id,
    b.amount_wei,
    b.amount_eth,
    COALESCE(fc.fname, b.bidder_address) AS bidder_identity,
    CASE
        WHEN fc.fname IS NOT NULL THEN concat('https://warpcast.com/', fc.fname)
        ELSE NULL
    END AS farcaster_link,
    b.bidder_address,
    b.tx_hash
FROM all_bids b
LEFT JOIN farcaster_identities fc
    ON b.bidder_address = fc.wallet_address
ORDER BY b.block_time DESC
LIMIT 100;
