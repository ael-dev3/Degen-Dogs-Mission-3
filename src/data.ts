export type Kpi = {
  label: string;
  value: string;
  sub: string;
  tone?: 'green' | 'amber' | 'muted';
};

export type DashboardPanel = {
  name: string;
  source: string;
  status: 'ready' | 'parameterized' | 'discovery';
};

export type ContractStatus = {
  label: string;
  address: string;
  status: 'verified' | 'pending';
};

export const kpis: Kpi[] = [
  { label: 'WOOF supply', value: '100B', sub: '18 decimals', tone: 'green' },
  { label: 'WOOF contract', value: '0x3e5c…d492', sub: 'verified Base token', tone: 'green' },
  { label: 'Auction cadence', value: '24h', sub: 'native ETH bids' },
  { label: 'Holder stream', value: '90d', sub: 'WOOF / SUP window' },
  { label: 'Vault + rewards', value: '20%', sub: '365d streams' },
  { label: 'Address coverage', value: '1 / 3', sub: 'NFT + auction pending', tone: 'amber' },
];

export const dashboardPanels: DashboardPanel[] = [
  { name: 'Mission 3 KPI strip', source: 'sql/01_mission3_kpis.sql', status: 'ready' },
  { name: 'WOOF market activity', source: 'sql/02_woof_market_activity.sql', status: 'ready' },
  { name: 'Transfer-ledger distribution', source: 'sql/03_holder_distribution.sql', status: 'ready' },
  { name: 'Superfluid stream updates', source: 'sql/04_superfluid_streams.sql', status: 'parameterized' },
  { name: 'Auction flow', source: 'sql/05_auction_flow.sql', status: 'parameterized' },
  { name: 'Contract discovery', source: 'sql/06_contract_discovery.sql', status: 'discovery' },
];

export const contractStatuses: ContractStatus[] = [
  { label: 'WOOF', address: '0x3e5c…d492', status: 'verified' },
  { label: 'Dog NFT', address: 'not published', status: 'pending' },
  { label: 'Auction house', address: 'not published', status: 'pending' },
];
