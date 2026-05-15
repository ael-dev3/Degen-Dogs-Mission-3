export type Fact = {
  label: string;
  value: string;
  detail: string;
};

export type Module = {
  title: string;
  description: string;
  metric: string;
  state: 'ready' | 'parameterized' | 'discovery';
};

export type QueryFile = {
  file: string;
  title: string;
  purpose: string;
  state: 'ready' | 'parameterized' | 'discovery';
};

export type Source = {
  title: string;
  detail: string;
  href: string;
};

export const facts: Fact[] = [
  {
    label: '$WOOF contract',
    value: '0x3e5c…d492',
    detail: 'Base Mainnet Pure Super Token, verified with onchain symbol/name/decimals calls.',
  },
  {
    label: 'Token supply',
    value: '100B',
    detail: '100,000,000,000 WOOF total supply, 18 decimals.',
  },
  {
    label: 'Mission launch',
    value: 'Jan 2026',
    detail: 'Mission 3 moved from Degen Chain L3 to Base Mainnet.',
  },
  {
    label: 'Auction cadence',
    value: '24h',
    detail: 'Daily Base auctions via Farcaster mini app, native ETH bids.',
  },
  {
    label: 'Stream window',
    value: '90d',
    detail: 'New WOOF/SUP acquired by Degen Dogs is streamed to Dog holders over 90 days.',
  },
  {
    label: 'Vault + rewards',
    value: '20%',
    detail: '10% staking rewards plus 10% WOOF Vault streaming airdrop, each over 365 days.',
  },
];

export const modules: Module[] = [
  {
    title: 'WOOF market activity',
    description: 'Daily DEX volume, buy/sell pressure, unique traders, and execution price from Base trades.',
    metric: 'dex.trades',
    state: 'ready',
  },
  {
    title: 'Transfer-ledger distribution',
    description: 'Balances reconstructed from ERC20 transfers, bucketed into holder tiers. Realtime Superfluid accrual is tracked separately.',
    metric: 'erc20_base.evt_Transfer',
    state: 'ready',
  },
  {
    title: 'Streaming pressure',
    description: 'Superfluid flow updates filtered to WOOF, normalized into per-day flow and active receiver counts.',
    metric: 'base.logs',
    state: 'parameterized',
  },
  {
    title: 'Auction flow',
    description: 'Auction created, bid, extension, and settled events decoded from raw Base logs after contract confirmation.',
    metric: '{{auction_house_address}}',
    state: 'discovery',
  },
];

export const queries: QueryFile[] = [
  {
    file: 'sql/00_sources_and_constants.sql',
    title: 'Sources and constants',
    purpose: 'Single source of truth for WOOF address, docs facts, and Dune parameter defaults.',
    state: 'ready',
  },
  {
    file: 'sql/01_mission3_kpis.sql',
    title: 'Mission 3 KPI strip',
    purpose: 'Supply, transfer volume, holders, DEX volume, and latest activity timestamp.',
    state: 'ready',
  },
  {
    file: 'sql/02_woof_market_activity.sql',
    title: 'WOOF market activity',
    purpose: 'Daily price, notional volume, buy/sell mix, and unique traders on Base.',
    state: 'ready',
  },
  {
    file: 'sql/03_holder_distribution.sql',
    title: 'Transfer-ledger distribution',
    purpose: 'Token transfer ledger reconstruction, top wallets, balances, and distribution buckets. Excludes unrealized realtime streaming deltas.',
    state: 'ready',
  },
  {
    file: 'sql/04_superfluid_streams.sql',
    title: 'Superfluid stream updates',
    purpose: 'Raw-log decode for CFA flow updates involving the WOOF super token.',
    state: 'parameterized',
  },
  {
    file: 'sql/05_auction_flow.sql',
    title: 'Auction flow',
    purpose: 'Nouns-style auction events on Base using a Dune address parameter.',
    state: 'parameterized',
  },
  {
    file: 'sql/06_contract_discovery.sql',
    title: 'Contract discovery',
    purpose: 'Find and verify Mission 3 Base NFT and auction contracts before hard-coding panels.',
    state: 'discovery',
  },
];

export const sources: Source[] = [
  {
    title: 'Dune dashboard',
    detail: 'Original public dashboard target for rebuild and query parity.',
    href: 'https://dune.com/ael_dev/degen-dogs-mission-3',
  },
  {
    title: 'Mission 3 docs',
    detail: 'Base migration, auction rules, WOOF economics, and stream windows.',
    href: 'https://docs.degendogs.club/introduction.md',
  },
  {
    title: '$WOOF docs',
    detail: 'Token address, supply, Streme/Superfluid context, and reward allocations.',
    href: 'https://docs.degendogs.club/basics/woof.md',
  },
  {
    title: 'Streamonomics docs',
    detail: '90-day streaming model and Dog holder equal-share mechanics.',
    href: 'https://docs.degendogs.club/basics/streamonomics.md',
  },
];

export const terminalLines = [
  'chain: base_mainnet',
  'token: WOOF / 18 decimals',
  'supply: 100,000,000,000',
  'panels: kpis, market, holders, streams, auctions',
  'site: github_pages',
];
