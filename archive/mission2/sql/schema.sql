-- Degen Dogs Mission 2 archival SQLite schema.
-- Amounts are stored as exact raw strings. Display amounts are separate and nullable until decimals are verified.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS mission2_index_runs (
  run_id TEXT PRIMARY KEY,
  run_timestamp_utc TEXT NOT NULL,
  chain_id INTEGER NOT NULL,
  rpc_url TEXT NOT NULL,
  from_block INTEGER NOT NULL,
  to_block INTEGER NOT NULL,
  auction_house_address TEXT NOT NULL,
  config_confidence TEXT NOT NULL DEFAULT 'unverified',
  raw_log_path TEXT,
  sqlite_path TEXT,
  manifest_path TEXT,
  warning TEXT
);

CREATE TABLE IF NOT EXISTS mission2_known_contracts (
  name TEXT PRIMARY KEY,
  chain_id INTEGER NOT NULL,
  address TEXT,
  confidence TEXT NOT NULL,
  source TEXT,
  how_to_verify TEXT,
  notes TEXT,
  updated_at_utc TEXT
);

CREATE TABLE IF NOT EXISTS mission2_raw_logs (
  chain_id INTEGER NOT NULL,
  contract_address TEXT NOT NULL,
  block_number INTEGER NOT NULL,
  block_hash TEXT,
  tx_hash TEXT NOT NULL,
  tx_index INTEGER,
  log_index INTEGER NOT NULL,
  block_time_utc TEXT,
  removed INTEGER NOT NULL DEFAULT 0,
  topics_json TEXT NOT NULL,
  data TEXT NOT NULL,
  event_name TEXT,
  topic0 TEXT,
  source_confidence TEXT NOT NULL DEFAULT 'unverified',
  raw_json TEXT NOT NULL,
  first_seen_run_id TEXT,
  last_seen_run_id TEXT,
  PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE TABLE IF NOT EXISTS mission2_auction_created (
  chain_id INTEGER NOT NULL,
  contract_address TEXT NOT NULL,
  block_number INTEGER NOT NULL,
  block_hash TEXT,
  tx_hash TEXT NOT NULL,
  tx_index INTEGER,
  log_index INTEGER NOT NULL,
  block_time_utc TEXT,
  dog_id INTEGER NOT NULL,
  start_time_unix TEXT NOT NULL,
  start_time_utc TEXT,
  end_time_unix TEXT NOT NULL,
  end_time_utc TEXT,
  source_confidence TEXT NOT NULL DEFAULT 'unverified',
  run_id TEXT,
  PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE TABLE IF NOT EXISTS mission2_auction_bids (
  chain_id INTEGER NOT NULL,
  contract_address TEXT NOT NULL,
  block_number INTEGER NOT NULL,
  block_hash TEXT,
  tx_hash TEXT NOT NULL,
  tx_index INTEGER,
  log_index INTEGER NOT NULL,
  block_time_utc TEXT,
  dog_id INTEGER NOT NULL,
  bidder TEXT NOT NULL,
  value_raw TEXT NOT NULL,
  value_display_native TEXT,
  display_decimals_confidence TEXT,
  extended INTEGER NOT NULL,
  source_confidence TEXT NOT NULL DEFAULT 'unverified',
  run_id TEXT,
  PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE TABLE IF NOT EXISTS mission2_auction_extended (
  chain_id INTEGER NOT NULL,
  contract_address TEXT NOT NULL,
  block_number INTEGER NOT NULL,
  block_hash TEXT,
  tx_hash TEXT NOT NULL,
  tx_index INTEGER,
  log_index INTEGER NOT NULL,
  block_time_utc TEXT,
  dog_id INTEGER NOT NULL,
  end_time_unix TEXT NOT NULL,
  end_time_utc TEXT,
  source_confidence TEXT NOT NULL DEFAULT 'unverified',
  run_id TEXT,
  PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE TABLE IF NOT EXISTS mission2_auction_settled (
  chain_id INTEGER NOT NULL,
  contract_address TEXT NOT NULL,
  block_number INTEGER NOT NULL,
  block_hash TEXT,
  tx_hash TEXT NOT NULL,
  tx_index INTEGER,
  log_index INTEGER NOT NULL,
  block_time_utc TEXT,
  dog_id INTEGER NOT NULL,
  winner TEXT NOT NULL,
  amount_raw TEXT NOT NULL,
  amount_display_native TEXT,
  display_decimals_confidence TEXT,
  source_confidence TEXT NOT NULL DEFAULT 'unverified',
  run_id TEXT,
  PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE TABLE IF NOT EXISTS mission2_parameter_updates (
  chain_id INTEGER NOT NULL,
  contract_address TEXT NOT NULL,
  block_number INTEGER NOT NULL,
  block_hash TEXT,
  tx_hash TEXT NOT NULL,
  tx_index INTEGER,
  log_index INTEGER NOT NULL,
  block_time_utc TEXT,
  event_name TEXT NOT NULL,
  parameter_name TEXT NOT NULL,
  value_raw TEXT NOT NULL,
  value_display TEXT,
  source_confidence TEXT NOT NULL DEFAULT 'unverified',
  run_id TEXT,
  PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE TABLE IF NOT EXISTS mission2_woof_vault_allocations (
  address TEXT PRIMARY KEY,
  units_raw TEXT NOT NULL,
  units_display TEXT,
  source_url TEXT NOT NULL,
  source_confidence TEXT NOT NULL,
  interpretation_confidence TEXT NOT NULL,
  note TEXT
);

CREATE TABLE IF NOT EXISTS mission2_recovery_sources (
  source_id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL,
  title TEXT,
  url TEXT,
  local_path TEXT,
  confidence TEXT NOT NULL,
  notes TEXT,
  captured_at_utc TEXT
);

CREATE TABLE IF NOT EXISTS mission2_index_gaps (
  gap_id TEXT PRIMARY KEY,
  chain_id INTEGER NOT NULL,
  from_block INTEGER,
  to_block INTEGER,
  reason TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'unknown',
  detected_at_utc TEXT NOT NULL,
  resolved_at_utc TEXT,
  notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_mission2_raw_logs_block ON mission2_raw_logs(chain_id, block_number, log_index);
CREATE INDEX IF NOT EXISTS idx_mission2_raw_logs_topic0 ON mission2_raw_logs(chain_id, topic0);
CREATE INDEX IF NOT EXISTS idx_mission2_created_dog ON mission2_auction_created(chain_id, dog_id);
CREATE INDEX IF NOT EXISTS idx_mission2_bids_dog ON mission2_auction_bids(chain_id, dog_id, block_number, log_index);
CREATE INDEX IF NOT EXISTS idx_mission2_bids_bidder ON mission2_auction_bids(chain_id, bidder);
CREATE INDEX IF NOT EXISTS idx_mission2_settled_dog ON mission2_auction_settled(chain_id, dog_id);
CREATE INDEX IF NOT EXISTS idx_mission2_settled_winner ON mission2_auction_settled(chain_id, winner);
