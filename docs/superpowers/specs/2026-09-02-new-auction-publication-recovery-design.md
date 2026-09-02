# New-Auction Publication Recovery Design

## Incident

The public dashboard stopped at Dog #820 and Base block 50,755,309 even
though the Windows/WSL watcher had quorum-confirmed Dog #821.  The queued
publisher always launched a bounded `current` refresh.  That refresh added Dog
#821 to the dashboard timeline but did not run the independent Mission 3
archive index, so `validate_mission3_archive_parity()` failed with
`missing_archive=[821]`.  Every attempt rolled back correctly, but the root WSL
anchor treated the failed one-shot publisher as a fatal anchor condition,
removed `/run/degen-dogs/activation-enabled`, and exited.  Task Scheduler then
restarted the same failing cycle every five minutes.

## Goals

- Publish a newly created auction without weakening archive parity.
- Preserve the low-latency current-only path for bids on the same Dog.
- Keep the WSL anchor and activation markers alive while retryable workers fail.
- Preserve queue records and rollback/quarantine guarantees until publication
  succeeds.
- Recover the existing Dog #821 generation without discarding generated or
  quarantined evidence.

## Non-goals

- Do not relax `validate_mission3_archive_parity()`.
- Do not run the full historical data build for every bid.
- Do not clear the publication queue or reset the production checkout by hand.
- Do not let queue JSON or environment input select arbitrary commands.

## Design

### Conditional archive promotion

`drain_publication_queue.py` will derive a trusted publisher scope from the
validated queue target and the fixed `generated/refresh_status.json` in the
normal clean queue path.
The target token comes from `publication_target["observation"]["token_id"]`.
The last published token comes from `current_dog_token_id` in the committed
status file.

- Equal, valid token IDs: launch the existing current-only path with
  `DEGEN_DOGS_RUN_MISSION3_ARCHIVE=0`.
- Different token IDs: launch an incremental Mission 3 archive pass before the
  bounded current refresh with `DEGEN_DOGS_RUN_MISSION3_ARCHIVE=1`.
- Missing, malformed, or unreadable committed status: fail safe toward the
  incremental archive path.  This costs time but preserves accuracy.
- A generating recovery journal: force the incremental archive path rather
  than trusting files that may belong to a partially generated attempt.  The
  Bash recovery union preserves any stronger journaled full/archive scope.

The queue schema remains `run_scope=current`; it describes the latency-sensitive
observation.  The publisher recovery journal and commit trailer record the
actual derived scope (`archive` on a transition).  No value from the queue is
used as a command or executable path.

The fixed Bash publisher's deferred-publication context gate will accept only
`current` and `archive`.  `full` and `archive_full` remain invalid for a queued
deferred launch.  Generation/digest validation and the authenticated queue
target remain mandatory, so accepting `archive` widens the generated surface
without widening caller authority.

### Anchor failure isolation

The root anchor will continue requiring all activation units to be loaded,
enabled, and active.  A missing triggered worker unit remains fatal.  A failed
one-shot publisher or Pages verifier becomes an observed worker failure rather
than an anchor failure: the anchor logs it, keeps the activation and ready
markers installed, and lets the existing path/timer retry policy run.

The anchor must not clear failed worker state or delete queue data.  Successful
subsequent starts naturally replace the one-shot service result.  Signals,
invalid activation markers, missing units, and disabled activation units remain
fail-closed conditions that terminate the anchor and remove runtime markers.

## Security and data integrity

- Scope selection reads only a validated queue record and a repository file at
  a fixed path.
- Unparseable status promotes work; it never suppresses archive verification.
- The fixed Bash publisher, inherited refresh-lock descriptor, exact Git path
  allowlist, CAS push, rollback, and quarantine behavior remain unchanged.
- Anchor changes do not grant the unprivileged runner control over systemd or
  root markers.

## Tests

- Unit test: same token keeps both refresh flags at `0`.
- Unit test: a new token sets archive to `1` and full refresh to `0`.
- Unit test: absent/malformed committed status promotes to archive.
- Queue integration test: a Dog transition launches the fixed Bash publisher
  with archive scope and retains the generation/digest binding.
- Bash publisher test: an authenticated deferred archive launch passes the
  context gate, runs archive indexing before the bounded current refresh, and
  records archive scope; deferred full/archive-full launches remain rejected.
- Publisher regression fixture: current-only Dog transition reproduces
  `missing_archive`, while promoted incremental archive plus current refresh
  passes parity.
- Anchor asset test: failed publisher service does not terminate the anchor or
  remove activation markers; missing worker unit still fails closed.
- Existing queue, publisher, WSL asset, policy, dashboard, archive, and Pages
  suites remain green.

## Rollout and recovery

1. Land the code and regression tests on `main` after complete local/WSL gates.
2. Build a new immutable trusted bundle and reinstall through the attested WSL
   installer; do not patch the root anchor in place.
3. Let the preserved generation-5 queue record trigger the promoted archive
   refresh for Dog #821.
4. Verify raw `main`, GitHub Pages, block hash, bundle digest, token, bid, and
   bidder match the production checkout.
5. Confirm the anchor stays up through an injected failed one-shot worker and a
   successful retry.

## Success criteria

- Dog #821 is publicly visible with its quorum-confirmed block/hash and exact
  auction tuple.
- The publication queue reaches lag zero without manual deletion.
- A failed publisher attempt does not stop the anchor or activation units.
- Same-Dog bid publication retains the current-only fast path.
