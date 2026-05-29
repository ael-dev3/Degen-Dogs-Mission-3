-- Template only. Confirm exact Dune schema/columns before use.
-- Goal: return one daily USD price row per asset/date with source metadata.

select
  date_trunc('day', timestamp) as day,
  blockchain,
  contract_address,
  symbol,
  price
from prices.day
where
  (
    symbol in ('ETH', 'WETH', 'DEGEN', 'WDEGEN')
    or contract_address in (
      from_hex('7ceb23fd6bc0add59e62ac25578270cff1b9f619') -- Polygon WETH; confirm Dune table type before use
    )
  )
  and timestamp >= timestamp '2022-03-01'
  and timestamp < current_timestamp
order by day asc;
