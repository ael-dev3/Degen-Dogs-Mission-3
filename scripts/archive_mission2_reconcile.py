#!/usr/bin/env python3
"""Mission 2 reconciliation status helper.

This helper reports the current onchain archive counts and writes the Dune
reconciliation summary. It does not invent Dune values when Dune exports are
missing.
"""
from __future__ import annotations
import json, re, sqlite3
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / 'archive' / 'mission2' / 'data' / 'sqlite' / 'mission2.sqlite'
OUT = ROOT / 'archive' / 'mission2' / 'data' / 'generated' / 'reconciliation_summary.json'
SQL_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

def now(): return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')

def quote_identifier(value: str) -> str:
    if not SQL_IDENTIFIER_RE.match(value):
        raise ValueError(f'unsafe SQL identifier: {value!r}')
    return '"' + value + '"'

def main() -> int:
    conn=sqlite3.connect(DB)
    counts={}
    for table in ['mission2_auction_created','mission2_auction_bids','mission2_auction_extended','mission2_auction_settled','mission2_raw_logs']:
        counts[table]=conn.execute('SELECT COUNT(*) FROM ' + quote_identifier(table)).fetchone()[0]
    dog_range=conn.execute('SELECT MIN(dog_id), MAX(dog_id) FROM mission2_auction_created').fetchone()
    summary={
        'schema_version':1,'updated_at_utc':now(),'status':'onchain_verified_dune_unavailable',
        'local_counts':counts,'dog_range':{'first_token_id':dog_range[0],'last_token_id':dog_range[1]},
        'comparisons':[
            {'target':'AuctionCreated count','local_value':counts['mission2_auction_created'],'dune_value':None,'classification':'missing_in_dune_recovery'},
            {'target':'AuctionBid count','local_value':counts['mission2_auction_bids'],'dune_value':None,'classification':'missing_in_dune_recovery'},
            {'target':'AuctionExtended count','local_value':counts['mission2_auction_extended'],'dune_value':None,'classification':'missing_in_dune_recovery'},
            {'target':'AuctionSettled count','local_value':counts['mission2_auction_settled'],'dune_value':None,'classification':'missing_in_dune_recovery'},
        ],
        'notes':'Official Dune query IDs, SQL, and result exports are not recovered; no match/mismatch claim is made.'
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, indent=2) + "\n", encoding='utf-8')
    print(json.dumps(summary, indent=2))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
