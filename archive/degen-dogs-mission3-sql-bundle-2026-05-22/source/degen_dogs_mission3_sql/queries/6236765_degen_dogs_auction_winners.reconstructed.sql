-- Degen Dogs - Auction Winners
-- Dune query id: 6236765
-- Owner: @ael_dev
-- Status: reconstructed from publicly indexed Dune snippets, not fetched from Dune's authenticated query_sql API.
-- Fixed: patched for current Mission 3 Base auction house after Base RPC verification.
-- Notes:
--   * Original public snippets used contract_address 0x3620CA030a023BCE87EC59a8b0E979bD7607Fdbd.
--   * That original snippet address has no code on Base and no recent AuctionBid logs.
--   * Current Mission 3 auction house verified on Base: 0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA.
--   * Winners are decoded from AuctionSettled events instead of inferred from latest bids.
--   * ETH amounts keep exact wei as amount_wei and fractional ETH as amount_eth.

WITH settled_auctions AS (
    SELECT
        block_time,
        tx_hash,

        -- Extract Token ID from indexed topic1.
        bytearray_to_uint256(topic1) AS token_id,

        -- Extract settlement amount from AuctionSettled data payload:
        -- data[1..32]   = winner address, right-aligned
        -- data[33..64]  = amount uint256
        bytearray_to_uint256(bytearray_substring(data, 33, 32)) AS amount_wei,
        CAST(
            bytearray_to_uint256(bytearray_substring(data, 33, 32))
            AS DOUBLE
        ) / 1e18 AS amount_eth,

        -- Extract winner address from the first 32-byte ABI slot.
        lower(concat('0x', to_hex(bytearray_substring(data, 13, 20)))) AS winner_address

    FROM base.logs
    WHERE contract_address = 0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA
      AND topic0 = 0xc9f72b276a388619c6d185d146697036241880c36654b1a3ffdad07c24038d99
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
    sa.token_id,
    sa.amount_wei,
    sa.amount_eth,
    COALESCE(fc.fname, sa.winner_address) AS winner_identity,
    CASE
        WHEN fc.fname IS NOT NULL THEN concat('https://warpcast.com/', fc.fname)
        ELSE NULL
    END AS farcaster_link,
    sa.winner_address,
    sa.block_time,
    sa.tx_hash
FROM settled_auctions sa
LEFT JOIN farcaster_identities fc
    ON sa.winner_address = fc.wallet_address
ORDER BY sa.token_id DESC;
