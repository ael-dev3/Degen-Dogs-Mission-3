-- Mission 1 Polygon archive schema. Tables are idempotent and safe to rebuild.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS mission1_raw_logs (
  chain_id INTEGER NOT NULL,
  contract_address TEXT NOT NULL,
  block_number INTEGER NOT NULL,
  block_hash TEXT,
  tx_hash TEXT NOT NULL,
  tx_index INTEGER,
  log_index INTEGER NOT NULL,
  block_time_utc TEXT,
  removed INTEGER DEFAULT 0,
  topics_json TEXT NOT NULL,
  data TEXT NOT NULL,
  event_name TEXT,
  topic0 TEXT,
  source_confidence TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  first_seen_run_id TEXT,
  last_seen_run_id TEXT,
  PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE TABLE IF NOT EXISTS mission1_index_runs (
  run_id TEXT PRIMARY KEY,
  run_timestamp_utc TEXT NOT NULL,
  chain_id INTEGER NOT NULL,
  rpc_url TEXT,
  from_block INTEGER,
  to_block INTEGER,
  auction_house_address TEXT,
  config_confidence TEXT,
  raw_log_path TEXT,
  sqlite_path TEXT,
  manifest_path TEXT,
  warning TEXT
);

CREATE TABLE IF NOT EXISTS mission1_index_state (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at_utc TEXT,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS mission1_index_gaps (
  gap_id TEXT PRIMARY KEY,
  chain_id INTEGER NOT NULL,
  from_block INTEGER,
  to_block INTEGER,
  reason TEXT NOT NULL,
  severity TEXT NOT NULL,
  detected_at_utc TEXT NOT NULL,
  resolved_at_utc TEXT,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS mission1_auction_created (
  chain_id INTEGER NOT NULL,
  contract_address TEXT NOT NULL,
  dog_id INTEGER NOT NULL,
  start_time_unix TEXT,
  start_time_utc TEXT,
  end_time_unix TEXT,
  end_time_utc TEXT,
  block_number INTEGER NOT NULL,
  block_hash TEXT,
  tx_hash TEXT NOT NULL,
  tx_index INTEGER,
  log_index INTEGER NOT NULL,
  block_time_utc TEXT,
  source_confidence TEXT NOT NULL,
  run_id TEXT,
  PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE TABLE IF NOT EXISTS mission1_auction_bids (
  chain_id INTEGER NOT NULL,
  contract_address TEXT NOT NULL,
  dog_id INTEGER NOT NULL,
  bidder TEXT,
  value_raw TEXT,
  value_display_weth TEXT,
  display_decimals_confidence TEXT,
  extended INTEGER,
  block_number INTEGER NOT NULL,
  block_hash TEXT,
  tx_hash TEXT NOT NULL,
  tx_index INTEGER,
  log_index INTEGER NOT NULL,
  block_time_utc TEXT,
  source_confidence TEXT NOT NULL,
  run_id TEXT,
  PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE TABLE IF NOT EXISTS mission1_auction_extended (
  chain_id INTEGER NOT NULL,
  contract_address TEXT NOT NULL,
  dog_id INTEGER NOT NULL,
  end_time_unix TEXT,
  end_time_utc TEXT,
  block_number INTEGER NOT NULL,
  block_hash TEXT,
  tx_hash TEXT NOT NULL,
  tx_index INTEGER,
  log_index INTEGER NOT NULL,
  block_time_utc TEXT,
  source_confidence TEXT NOT NULL,
  run_id TEXT,
  PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE TABLE IF NOT EXISTS mission1_auction_settled (
  chain_id INTEGER NOT NULL,
  contract_address TEXT NOT NULL,
  dog_id INTEGER NOT NULL,
  winner TEXT,
  amount_raw TEXT,
  amount_display_weth TEXT,
  display_decimals_confidence TEXT,
  block_number INTEGER NOT NULL,
  block_hash TEXT,
  tx_hash TEXT NOT NULL,
  tx_index INTEGER,
  log_index INTEGER NOT NULL,
  block_time_utc TEXT,
  source_confidence TEXT NOT NULL,
  run_id TEXT,
  PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE TABLE IF NOT EXISTS mission1_nft_transfers (
  chain_id INTEGER NOT NULL,
  contract_address TEXT NOT NULL,
  token_contract TEXT NOT NULL,
  from_address TEXT,
  to_address TEXT,
  token_id INTEGER,
  value_raw TEXT,
  block_number INTEGER NOT NULL,
  block_hash TEXT,
  tx_hash TEXT NOT NULL,
  tx_index INTEGER,
  log_index INTEGER NOT NULL,
  block_time_utc TEXT,
  source_confidence TEXT NOT NULL,
  run_id TEXT,
  PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE TABLE IF NOT EXISTS mission1_bid_tokens_transfers (
  chain_id INTEGER NOT NULL,
  contract_address TEXT NOT NULL,
  token_contract TEXT NOT NULL,
  from_address TEXT,
  to_address TEXT,
  token_id INTEGER,
  value_raw TEXT,
  block_number INTEGER NOT NULL,
  block_hash TEXT,
  tx_hash TEXT NOT NULL,
  tx_index INTEGER,
  log_index INTEGER NOT NULL,
  block_time_utc TEXT,
  source_confidence TEXT NOT NULL,
  run_id TEXT,
  PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE TABLE IF NOT EXISTS mission1_treasury_transfers (
  chain_id INTEGER NOT NULL,
  contract_address TEXT NOT NULL,
  token_contract TEXT,
  from_address TEXT,
  to_address TEXT,
  value_raw TEXT,
  amount_token TEXT,
  block_number INTEGER,
  tx_hash TEXT,
  log_index INTEGER,
  block_time_utc TEXT,
  source_confidence TEXT,
  notes TEXT,
  PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE TABLE IF NOT EXISTS mission1_stream_events (
  chain_id INTEGER,
  contract_address TEXT,
  event_name TEXT,
  sender TEXT,
  receiver TEXT,
  token TEXT,
  value_raw TEXT,
  block_number INTEGER,
  tx_hash TEXT,
  log_index INTEGER,
  block_time_utc TEXT,
  source_confidence TEXT,
  notes TEXT,
  PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE TABLE IF NOT EXISTS mission1_idle_events (
  chain_id INTEGER,
  contract_address TEXT,
  event_name TEXT,
  from_address TEXT,
  to_address TEXT,
  value_raw TEXT,
  block_number INTEGER,
  tx_hash TEXT,
  log_index INTEGER,
  block_time_utc TEXT,
  source_confidence TEXT,
  notes TEXT,
  PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE TABLE IF NOT EXISTS mission1_donation_events (
  chain_id INTEGER,
  contract_address TEXT,
  event_name TEXT,
  from_address TEXT,
  to_address TEXT,
  value_raw TEXT,
  block_number INTEGER,
  tx_hash TEXT,
  log_index INTEGER,
  block_time_utc TEXT,
  source_confidence TEXT,
  notes TEXT,
  PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE TABLE IF NOT EXISTS mission1_governance_events (
  chain_id INTEGER,
  contract_address TEXT,
  event_name TEXT,
  proposal_id TEXT,
  actor TEXT,
  block_number INTEGER,
  tx_hash TEXT,
  log_index INTEGER,
  block_time_utc TEXT,
  source_confidence TEXT,
  notes TEXT,
  PRIMARY KEY (chain_id, tx_hash, log_index)
);
