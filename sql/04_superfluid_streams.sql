-- Degen Dogs Mission 3: Superfluid WOOF stream updates
-- Raw CFAv1 FlowUpdated decoder for Base logs.
-- Positive flow updates mean the absolute flow rate in that event is positive; they are not necessarily new/increased flows.
-- Event signature:
-- FlowUpdated(address indexed token,address indexed sender,address indexed receiver,int96 flowRate,int256 totalSenderFlowRate,int256 totalReceiverFlowRate,bytes userData)

WITH params AS (
    SELECT
        0x3e5c4FA0cAA794516eD0DF77f31daA534918d492 AS woof_token,
        0x57269d2ebcccecdcc0d9d2c0a0b80ead95f344e28ec20f50f709811f209d4e0e AS flow_updated_topic
),
flow_logs AS (
    SELECT
        block_time,
        tx_hash,
        contract_address AS cfa_contract,
        bytearray_substring(topic1, 13, 20) AS token,
        bytearray_substring(topic2, 13, 20) AS sender,
        bytearray_substring(topic3, 13, 20) AS receiver,
        bytearray_to_int256(bytearray_substring(data, 1, 32)) AS flow_rate_raw,
        bytearray_to_int256(bytearray_substring(data, 33, 32)) AS total_sender_flow_rate_raw,
        bytearray_to_int256(bytearray_substring(data, 65, 32)) AS total_receiver_flow_rate_raw
    FROM base.logs
    WHERE topic0 = (SELECT flow_updated_topic FROM params)
      AND bytearray_substring(topic1, 13, 20) = (SELECT woof_token FROM params)
),
normalized AS (
    SELECT
        block_time,
        tx_hash,
        cfa_contract,
        sender,
        receiver,
        CAST(flow_rate_raw AS DOUBLE) / 1e18 AS flow_rate_woof_per_second,
        CAST(flow_rate_raw AS DOUBLE) * 86400 / 1e18 AS flow_rate_woof_per_day,
        CAST(total_sender_flow_rate_raw AS DOUBLE) * 86400 / 1e18 AS total_sender_flow_woof_per_day,
        CAST(total_receiver_flow_rate_raw AS DOUBLE) * 86400 / 1e18 AS total_receiver_flow_woof_per_day
    FROM flow_logs
)
SELECT
    date_trunc('day', block_time) AS day,
    COUNT(*) AS flow_updates,
    COUNT(DISTINCT sender) AS senders,
    COUNT(DISTINCT receiver) AS receivers,
    SUM(CASE WHEN flow_rate_woof_per_second > 0 THEN 1 ELSE 0 END) AS positive_flow_updates,
    SUM(CASE WHEN flow_rate_woof_per_second = 0 THEN 1 ELSE 0 END) AS closed_flow_updates,
    AVG(flow_rate_woof_per_day) AS avg_flow_woof_per_day,
    approx_percentile(flow_rate_woof_per_day, 0.5) AS median_flow_woof_per_day,
    MAX(block_time) AS latest_update
FROM normalized
GROUP BY 1
ORDER BY 1 DESC;
