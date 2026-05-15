-- Degen Dogs Mission 3: WOOF market activity on Base
-- Visualization: daily candles/volume with buy-sell split.

WITH params AS (
    SELECT 0x3e5c4FA0cAA794516eD0DF77f31daA534918d492 AS woof_token
),
woof_trades AS (
    SELECT
        block_time,
        tx_hash,
        tx_from AS trader,
        amount_usd,
        CASE
            WHEN token_bought_address = (SELECT woof_token FROM params) THEN 'buy'
            ELSE 'sell'
        END AS side,
        CASE
            WHEN token_bought_address = (SELECT woof_token FROM params) THEN token_bought_amount
            ELSE token_sold_amount
        END AS amount_woof,
        CASE
            WHEN token_bought_address = (SELECT woof_token FROM params) THEN token_sold_symbol
            ELSE token_bought_symbol
        END AS paired_symbol
    FROM dex.trades
    WHERE blockchain = 'base'
      AND amount_usd IS NOT NULL
      AND (
        token_bought_address = (SELECT woof_token FROM params)
        OR token_sold_address = (SELECT woof_token FROM params)
      )
),
normalized AS (
    SELECT
        date_trunc('day', block_time) AS day,
        side,
        trader,
        tx_hash,
        amount_usd,
        amount_woof,
        amount_usd / NULLIF(amount_woof, 0) AS execution_price_usd
    FROM woof_trades
    WHERE amount_woof > 0
)
SELECT
    day,
    COUNT(*) AS trades,
    COUNT(DISTINCT trader) AS traders,
    COUNT(DISTINCT tx_hash) AS transactions,
    SUM(amount_usd) AS volume_usd,
    SUM(CASE WHEN side = 'buy' THEN amount_usd ELSE 0 END) AS buy_volume_usd,
    SUM(CASE WHEN side = 'sell' THEN amount_usd ELSE 0 END) AS sell_volume_usd,
    SUM(CASE WHEN side = 'buy' THEN amount_woof ELSE 0 END) AS bought_woof,
    SUM(CASE WHEN side = 'sell' THEN amount_woof ELSE 0 END) AS sold_woof,
    approx_percentile(execution_price_usd, 0.5) AS median_price_usd,
    AVG(execution_price_usd) AS avg_price_usd
FROM normalized
GROUP BY 1
ORDER BY 1 DESC;
