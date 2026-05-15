-- Degen Dogs Mission 3: sources and constants
-- Use this as a small Dune table/text panel and as the source of defaults for other queries.

WITH constants AS (
    SELECT
        'base' AS blockchain,
        0x3e5c4FA0cAA794516eD0DF77f31daA534918d492 AS woof_token,
        'Degen Dogs WOOF' AS token_name,
        'WOOF' AS symbol,
        18 AS decimals,
        CAST(100000000000 AS DOUBLE) AS total_supply_woof,
        CAST(0.10 AS DOUBLE) AS staking_rewards_supply_share,
        CAST(0.10 AS DOUBLE) AS vault_airdrop_supply_share,
        90 AS dog_holder_stream_days,
        365 AS staking_and_vault_stream_days,
        'https://docs.degendogs.club/introduction.md' AS mission_docs,
        'https://docs.degendogs.club/basics/woof.md' AS woof_docs,
        'https://docs.degendogs.club/basics/streamonomics.md' AS streamonomics_docs,
        'https://dune.com/ael_dev/degen-dogs-mission-3' AS source_dashboard
)
SELECT *
FROM constants;
