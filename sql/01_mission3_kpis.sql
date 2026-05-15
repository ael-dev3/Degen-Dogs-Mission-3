-- Degen Dogs Mission 3: KPI strip
-- Panels: total supply, transfer-ledger activity, transfer-ledger holders, DEX volume, latest WOOF movement.
-- Note: WOOF is a Superfluid Pure Super Token. ERC20 Transfer events do not include unrealized realtime stream accrual/depletion; stream deltas are tracked in sql/04_superfluid_streams.sql.

WITH params AS (
    SELECT 0x3e5c4FA0cAA794516eD0DF77f31daA534918d492 AS woof_token
),
transfers AS (
    SELECT
        evt_block_time AS block_time,
        evt_tx_hash AS tx_hash,
        "from" AS from_address,
        "to" AS to_address,
        CAST(value AS DOUBLE) / 1e18 AS amount_woof
    FROM erc20_base.evt_Transfer
    WHERE contract_address = (SELECT woof_token FROM params)
),
ledger AS (
    SELECT to_address AS wallet, amount_woof AS delta_woof
    FROM transfers
    WHERE to_address <> 0x0000000000000000000000000000000000000000

    UNION ALL

    SELECT from_address AS wallet, -amount_woof AS delta_woof
    FROM transfers
    WHERE from_address <> 0x0000000000000000000000000000000000000000
),
balances AS (
    SELECT wallet, SUM(delta_woof) AS balance_woof
    FROM ledger
    GROUP BY 1
    HAVING SUM(delta_woof) > 0.000000000001
),
dex_activity AS (
    SELECT
        SUM(amount_usd) AS dex_volume_usd,
        COUNT(*) AS dex_trades,
        COUNT(DISTINCT tx_from) AS active_traders,
        MAX(block_time) AS latest_trade_time
    FROM dex.trades
    WHERE blockchain = 'base'
      AND (
        token_bought_address = (SELECT woof_token FROM params)
        OR token_sold_address = (SELECT woof_token FROM params)
      )
),
transfer_activity AS (
    SELECT
        COUNT(*) AS transfer_count,
        COUNT(DISTINCT tx_hash) AS transfer_txs,
        SUM(amount_woof) AS transfer_volume_woof,
        MAX(block_time) AS latest_transfer_time
    FROM transfers
)
SELECT 'Total supply' AS metric, CAST(100000000000 AS DOUBLE) AS value, 'WOOF' AS unit
UNION ALL
SELECT 'Transfer-ledger holders', CAST(COUNT(*) AS DOUBLE), 'wallets' FROM balances
UNION ALL
SELECT 'Transfer count', CAST(transfer_count AS DOUBLE), 'events' FROM transfer_activity
UNION ALL
SELECT 'Transfer volume', transfer_volume_woof, 'WOOF' FROM transfer_activity
UNION ALL
SELECT 'DEX volume', COALESCE(dex_volume_usd, 0), 'USD' FROM dex_activity
UNION ALL
SELECT 'DEX trades', CAST(COALESCE(dex_trades, 0) AS DOUBLE), 'trades' FROM dex_activity
UNION ALL
SELECT 'Active DEX traders', CAST(COALESCE(active_traders, 0) AS DOUBLE), 'wallets' FROM dex_activity
UNION ALL
SELECT 'Latest WOOF transfer age', CAST(date_diff('hour', latest_transfer_time, CURRENT_TIMESTAMP) AS DOUBLE), 'hours' FROM transfer_activity;
