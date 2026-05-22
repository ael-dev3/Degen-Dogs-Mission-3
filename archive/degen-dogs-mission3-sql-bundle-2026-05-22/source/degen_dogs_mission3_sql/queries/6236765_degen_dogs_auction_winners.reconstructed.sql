-- Degen Dogs - Auction Winners
-- Dune query id: 6236765
-- Owner: @ael_dev
-- Status: reconstructed from publicly indexed Dune snippets, not fetched from Dune's authenticated query_sql API.
-- Notes:
--   * Public search snippets identify this query as "Degen Dogs - Auction Winners".
--   * Snippets confirm base.logs, contract_address 0x3620CA..., AuctionBid topic0 0x115916..., and dune.neynar Farcaster profile join.
--   * Degen Dogs/Superfluid docs list the Base Auction House as 0x8F34fe..., so verify the contract address in Dune before production use.

WITH all_bids AS (
    SELECT
        block_time,
        tx_hash,

        -- Extract Token ID from indexed topic1.
        bytearray_to_uint256(topic1) AS token_id,

        -- Extract bid amount from AuctionBid data payload:
        -- data[1..32]   = sender address, right-aligned
        -- data[33..64]  = value uint256
        -- data[65..96]  = extended bool
        CAST(
            ROUND(
                bytearray_to_uint256(bytearray_substring(data, 33, 32)) / 1e18,
                0
            ) AS BIGINT
        ) AS amount,

        -- Extract bidder address from the first 32-byte ABI slot.
        lower(concat('0x', to_hex(bytearray_substring(data, 13, 20)))) AS bidder_address

    FROM base.logs
    WHERE contract_address = 0x3620CA030a023BCE87EC59a8b0E979bD7607Fdbd
      AND topic0 = 0x1159164c56f277e6fc99c11731bd380e0347deb969b75523398734c252706ea3
),

winning_bids AS (
    SELECT
        *,

        -- Rank 1 = latest bid per Dog.
        ROW_NUMBER() OVER (
            PARTITION BY token_id
            ORDER BY block_time DESC
        ) AS rank
    FROM all_bids
),

current_dog AS (
    SELECT
        MAX(token_id) AS current_dog_id
    FROM all_bids
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
    wb.token_id,
    wb.amount,
    COALESCE(fc.fname, wb.bidder_address) AS winner_identity,
    CASE
        WHEN fc.fname IS NOT NULL THEN concat('https://warpcast.com/', fc.fname)
        ELSE NULL
    END AS farcaster_link,
    wb.bidder_address,
    wb.block_time,
    wb.tx_hash
FROM winning_bids wb
CROSS JOIN current_dog cd
LEFT JOIN farcaster_identities fc
    ON wb.bidder_address = fc.wallet_address
WHERE wb.rank = 1
  -- Filter for completed auctions only.
  AND wb.token_id < cd.current_dog_id
ORDER BY wb.token_id DESC;
