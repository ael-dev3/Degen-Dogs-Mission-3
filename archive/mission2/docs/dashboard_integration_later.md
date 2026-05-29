# Future Dashboard Integration Later

Do not expose Mission 2 data in the live Mission 3 dashboard until Ael explicitly asks.

When approved, use only the verified generated archive artifacts:

- `archive/mission2/data/generated/mission2_dog_search_index.json`
- `archive/mission2/data/generated/mission2_auction_winners.csv`
- `archive/mission2/data/generated/mission2_bidder_leaderboard.csv`
- `archive/mission2/data/generated/manifest.json`

Recommended public caveats:

- Historical Degen Chain archive.
- Onchain auction data verified from logs and contract getters.
- Dune reconciliation pending until official query IDs/SQL/results are recovered.
- Reward/stream accounting is incomplete.

Possible future filters:

- Dog ID
- winner wallet
- bidder wallet
- final amount
- transaction hash
- auction date
- Mission 2 vs Mission 3 transition
