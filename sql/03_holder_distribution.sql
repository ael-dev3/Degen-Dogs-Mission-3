-- Degen Dogs Mission 3: WOOF transfer-ledger distribution
-- Reconstructs ERC20 transfer-ledger balances from Transfer events.
-- Note: WOOF is a Superfluid Pure Super Token. This query intentionally excludes unrealized realtime stream accrual/depletion; pair it with sql/04_superfluid_streams.sql for streaming state.

WITH params AS (
    SELECT 0x3e5c4FA0cAA794516eD0DF77f31daA534918d492 AS woof_token
),
ledger AS (
    SELECT
        "to" AS wallet,
        CAST(value AS DOUBLE) / 1e18 AS delta_woof
    FROM erc20_base.evt_Transfer
    WHERE contract_address = (SELECT woof_token FROM params)
      AND "to" <> 0x0000000000000000000000000000000000000000

    UNION ALL

    SELECT
        "from" AS wallet,
        -CAST(value AS DOUBLE) / 1e18 AS delta_woof
    FROM erc20_base.evt_Transfer
    WHERE contract_address = (SELECT woof_token FROM params)
      AND "from" <> 0x0000000000000000000000000000000000000000
),
balances AS (
    SELECT
        wallet,
        SUM(delta_woof) AS balance_woof
    FROM ledger
    GROUP BY 1
    HAVING SUM(delta_woof) > 0.000000000001
),
ranked AS (
    SELECT
        wallet,
        balance_woof,
        balance_woof / 100000000000 AS supply_share,
        ROW_NUMBER() OVER (ORDER BY balance_woof DESC) AS holder_rank,
        CASE
            WHEN balance_woof >= 1000000000 THEN '1B+'
            WHEN balance_woof >= 100000000 THEN '100M-1B'
            WHEN balance_woof >= 10000000 THEN '10M-100M'
            WHEN balance_woof >= 1000000 THEN '1M-10M'
            WHEN balance_woof >= 100000 THEN '100K-1M'
            WHEN balance_woof >= 10000 THEN '10K-100K'
            ELSE '<10K'
        END AS balance_bucket
    FROM balances
)
SELECT
    holder_rank,
    wallet,
    balance_woof,
    supply_share,
    balance_bucket,
    SUM(balance_woof) OVER (ORDER BY holder_rank) / 100000000000 AS cumulative_supply_share
FROM ranked
ORDER BY holder_rank
LIMIT 250;
