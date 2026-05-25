# Live auction bidding module attempt archive

Archived commit: `82bdf57008e03561bb1f3813bbf1a0d387d3b36d` (`[verified] Add live auction bidding module`)

Status: archived/reverted from the active GitHub Pages site at Ael's request.

## What this preserved

- `attempt.patch.gz` — gzipped full binary-safe patch for commit `82bdf57`.
- `COMMIT.md` — commit metadata and file-stat summary.
- `changed-files.txt` — active files touched by the attempt.
- `source-snapshot/` — easy-to-read copies of the attempted `scripts/build_dashboard.py`, `index.html`, and `README.md` from that commit.

## Attempt summary

This attempt added a browser-side live auction card and wallet bidding flow to the static dashboard. It read Base auction state from the browser and submitted user-signed bids directly to the auction house. The active site is being reverted to the cached/static dashboard flow; this folder keeps the attempt for provenance and possible future reference.

## Restore/reference

To inspect the exact attempt later, review `attempt.patch.gz` or the original commit `82bdf57008e03561bb1f3813bbf1a0d387d3b36d`. To re-apply in a scratch branch, start from the parent commit `9870e760c06920277a57079b0a516139a757c043` or a compatible tree and apply/cherry-pick intentionally after review.

## Caveat

This is archived provenance, not active production code. Do not treat the wallet-bidding module as the current public dashboard behavior.

Decompress with `gzip -dc attempt.patch.gz > attempt.patch` if a plain patch file is needed locally.
