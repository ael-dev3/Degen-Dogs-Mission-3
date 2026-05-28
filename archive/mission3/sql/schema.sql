PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS mission3_raw_logs (
  chain_id INTEGER NOT NULL,
  address TEXT NOT NULL,
  block_number INTEGER NOT NULL,
  block_hash TEXT NOT NULL,
  transaction_hash TEXT NOT NULL,
  transaction_index INTEGER NOT NULL,
  log_index INTEGER NOT NULL,
  removed INTEGER NOT NULL DEFAULT 0,
  topic0 TEXT NOT NULL,
  topic1 TEXT,
  topic2 TEXT,
  topic3 TEXT,
  data TEXT NOT NULL,
  fetched_at_utc TEXT NOT NULL,
  source_rpc TEXT NOT NULL,
  PRIMARY KEY (chain_id, transaction_hash, log_index)
);

CREATE INDEX IF NOT EXISTS idx_mission3_raw_logs_block ON mission3_raw_logs(chain_id, block_number, log_index);
CREATE INDEX IF NOT EXISTS idx_mission3_raw_logs_topic0 ON mission3_raw_logs(chain_id, topic0);

CREATE TABLE IF NOT EXISTS mission3_auction_created (
  token_id INTEGER NOT NULL,
  start_time INTEGER NOT NULL,
  end_time INTEGER NOT NULL,
  block_number INTEGER NOT NULL,
  transaction_hash TEXT NOT NULL,
  log_index INTEGER NOT NULL,
  block_time_utc TEXT,
  PRIMARY KEY (transaction_hash, log_index)
);

CREATE INDEX IF NOT EXISTS idx_mission3_auction_created_token ON mission3_auction_created(token_id);

CREATE TABLE IF NOT EXISTS mission3_auction_bids (
  token_id INTEGER NOT NULL,
  bidder TEXT NOT NULL,
  amount_raw TEXT NOT NULL,
  amount_eth TEXT NOT NULL,
  extended INTEGER NOT NULL,
  block_number INTEGER NOT NULL,
  transaction_hash TEXT NOT NULL,
  log_index INTEGER NOT NULL,
  block_time_utc TEXT,
  PRIMARY KEY (transaction_hash, log_index)
);

CREATE INDEX IF NOT EXISTS idx_mission3_auction_bids_token ON mission3_auction_bids(token_id, block_number, log_index);
CREATE INDEX IF NOT EXISTS idx_mission3_auction_bids_bidder ON mission3_auction_bids(bidder);

CREATE TABLE IF NOT EXISTS mission3_auction_extended (
  token_id INTEGER NOT NULL,
  end_time INTEGER NOT NULL,
  block_number INTEGER NOT NULL,
  transaction_hash TEXT NOT NULL,
  log_index INTEGER NOT NULL,
  block_time_utc TEXT,
  PRIMARY KEY (transaction_hash, log_index)
);

CREATE INDEX IF NOT EXISTS idx_mission3_auction_extended_token ON mission3_auction_extended(token_id);

CREATE TABLE IF NOT EXISTS mission3_auction_settled (
  token_id INTEGER NOT NULL,
  winner TEXT NOT NULL,
  amount_raw TEXT NOT NULL,
  amount_eth TEXT NOT NULL,
  block_number INTEGER NOT NULL,
  transaction_hash TEXT NOT NULL,
  log_index INTEGER NOT NULL,
  block_time_utc TEXT,
  PRIMARY KEY (transaction_hash, log_index)
);

CREATE INDEX IF NOT EXISTS idx_mission3_auction_settled_token ON mission3_auction_settled(token_id);
CREATE INDEX IF NOT EXISTS idx_mission3_auction_settled_winner ON mission3_auction_settled(winner);

CREATE TABLE IF NOT EXISTS mission3_index_state (
  id TEXT PRIMARY KEY,
  chain_id INTEGER NOT NULL,
  auction_house TEXT NOT NULL,
  from_block INTEGER NOT NULL,
  latest_indexed_block INTEGER,
  latest_indexed_block_time_utc TEXT,
  latest_run_at_utc TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT
);

CREATE TABLE IF NOT EXISTS mission3_index_gaps (
  from_block INTEGER NOT NULL,
  to_block INTEGER NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  resolved_at_utc TEXT,
  PRIMARY KEY (from_block, to_block, reason)
);

CREATE TABLE IF NOT EXISTS mission3_current_auction_snapshots (
  snapshot_at_utc TEXT NOT NULL,
  latest_block INTEGER NOT NULL,
  token_id INTEGER,
  start_time INTEGER,
  end_time INTEGER,
  highest_bidder TEXT,
  amount_raw TEXT,
  amount_eth TEXT,
  settled INTEGER,
  source TEXT NOT NULL,
  confidence TEXT NOT NULL,
  PRIMARY KEY (snapshot_at_utc, latest_block)
);
