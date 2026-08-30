# Browser Live Auction Quorum Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a fail-closed, two-of-three Base RPC auction overlay that updates a visible dashboard to the newest confirmed current-auction state within eight seconds at p95, while retaining the verified GitHub Pages snapshot as the fallback.

**Architecture:** `scripts/build_dashboard.py` will validate a source-controlled public-RPC configuration, an exact static auction tuple, and a content-addressed browser module before embedding an immutable bootstrap attestation. A dependency-free ES module outside the runner publish allowlist will collect heads, select the one-confirmation target, require matching block hash plus `auction()` bytes from two providers, and report a validated observation within a five-second two-phase deadline. The generated dashboard script becomes a hashed module script that imports the verified content-addressed module and routes both static and direct updates through one precedence-aware renderer.

**Tech Stack:** Python 3.14 generator and tests, dependency-free browser JavaScript, Node 22 built-in `node:test`, GitHub Pages, Base JSON-RPC, existing Vite build and CSP hash checks.

**Spec:** `docs/superpowers/specs/2026-08-30-low-latency-auction-data-design.md`

## Global Constraints

- Use Base mainnet chain ID `8453`, auction house `0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA`, and selector `0x7d9f6db5` only.
- Use exactly `https://mainnet.base.org`, `https://base-rpc.publicnode.com`, and `https://base-mainnet.g.alchemy.com/public`; do not emit credentialed runner URLs.
- Require two distinct provider responses at the same exact block number and block hash; retain one confirmation and never render a single-provider result.
- Poll only visible tabs, target `3000` ms cadence, use `2500` ms phase deadlines, a `1048576`-byte streaming response cap, and a `30000` ms maximum retry delay.
- Decode all RPC uint256 values as `BigInt` or canonical decimal strings; do not convert bid wei through JavaScript `Number`.
- Green requires both direct-observation age and canonical block age below `15000` ms; amber covers `15000-30000` ms; older or unavailable quorum is last-good fallback.
- Content Security Policy must retain exact inline script and style hashes, `default-src 'none'`, `base-uri 'none'`, `object-src 'none'`, `form-action 'none'`, and `referrer=no-referrer`.
- No browser wallet request, signing request, secret, user-provided RPC URL, or user-provided contract parameter is permitted.
- Ship this browser stage behind a source-controlled `enabled: false` flag first. Enable only in a separate reviewed source commit after production-origin probes pass.
- Do not alter the Mac runner's archive authority, watcher cadence, or the Pages workflow in this plan. A shared publisher allowlist may be narrowed from `public` to `public/generated` only after regression tests prove all existing Mac/current generated artifacts still stage; it must not change the Mac's runtime role.

---

## Stage Scope and Spec Coverage

This plan implements the spec's browser live-quorum, rendering, freshness, privacy, browser-test, and independently shippable rollout requirements. It deliberately leaves the data-only Pages fast lane and Windows-only non-blocking publisher queue untouched so the seconds-level viewer path can be reviewed, tested, deployed disabled, and enabled without waiting on a second operational refactor. Those two subsystems require their own plans because they change GitHub Actions and privileged runner lifecycle boundaries.

---

## File Structure

- Create: `config/browser-live-auction.json` — public, schema-validated feature configuration, immutable contract/code pin, and fixed RPC origins.
- Create: `browser/live-auction-quorum.js` — source-gated ES module exporting bounded RPC transport, quorum selection, ABI decoding, poller state, and a timer-injected controller.
- Create: `scripts/build_live_auction_module.py` and `public/live-auction-quorum.<sha256>.js` — a deterministic content-addressed static module that Vite copies unchanged to `dist`.
- Create: `scripts/test_live_auction_quorum.mjs` — Node 22 unit tests using fake fetch/streams, timers, visibility, and network state with no browser dependency.
- Modify: `sql/mission3_dashboard.sql` — expose the exact static six-field auction tuple plus snapshot block/hash.
- Modify: `scripts/build_dashboard.py` — validate config/module/tuple, create the immutable bootstrap attestation, embed the browser module, add CSP origins, and merge direct/static rendering safely.
- Modify: `scripts/test_build_dashboard.py`, `scripts/test_validate_dashboard_consistency.py`, and `scripts/test_build_live_snapshot_bundle.py` — generator/static-bundle attestation, CSP, raw-tuple, DOM, source-precedence, and generated-script assertions.
- Modify: `scripts/check_dashboard_ui.py` — public generated-page checks for the exact RPC CSP allowlist, content-addressed module, and live source/freshness controls.
- Modify: `scripts/refresh_and_publish.sh` and `scripts/test_refresh_and_publish.sh` — narrow runner publication from `public` to `public/generated` so configuration and browser code cannot be changed by a data-only runner commit.
- Modify: `package.json` — add one deterministic Node test command and include it in dashboard validation.
- Modify: `scripts/run_pages_validation.sh` — run the Node quorum test before the existing dashboard/UI checks.
- Create: `docs/operations/live-auction-quorum.md` — operator rollout, rollback, SLO sampling, privacy note, and production probe instructions.

## Interfaces

### Config embedded by the generator

```json
{
  "schema_version": 1,
  "enabled": false,
  "chain_id": 8453,
  "auction_house": "0x8f34fe11ce28893dea6a802c8d0b3d0ffc7f5cea",
  "dog_nft": "0x09154248ffdbaf8aa877ae8a4bf8ce1503596428",
  "selector": "0x7d9f6db5",
  "auction_house_code_sha256": "ade197f5a438f532d3502f579a7f53b141e596c4f6b2c52898f6aa0de0cad253",
  "quorum": 2,
  "confirmations": 1,
  "providers": [
    "https://mainnet.base.org",
    "https://base-rpc.publicnode.com",
    "https://base-mainnet.g.alchemy.com/public"
  ],
  "poll_interval_ms": 3000,
  "phase_timeout_ms": 2500,
  "max_response_bytes": 1048576,
  "retry_max_ms": 30000,
  "code_check_interval_ms": 300000,
  "fresh_ms": 15000,
  "stale_ms": 30000
}
```

`build_dashboard.py` must validate this source-controlled configuration and emit:

```js
const LIVE_AUCTION_BOOTSTRAP=Object.freeze({
  enabled:false,
  chainId:8453,
  auctionHouse:'0x8f34fe11ce28893dea6a802c8d0b3d0ffc7f5cea',
  dogNft:'0x09154248ffdbaf8aa877ae8a4bf8ce1503596428',
  selector:'0x7d9f6db5',
  codeSha256:'ade197f5a438f532d3502f579a7f53b141e596c4f6b2c52898f6aa0de0cad253',
  modulePath:'/Degen-Dogs-Mission-3/live-auction-quorum.<sha256>.js',
  moduleSha256:'<sha256 of the exact module bytes>',
  quorum:2,
  confirmations:1,
  providers:['https://mainnet.base.org','https://base-rpc.publicnode.com','https://base-mainnet.g.alchemy.com/public'],
  pollIntervalMs:3000,
  phaseTimeoutMs:2500,
  maxResponseBytes:1048576,
  retryMaxMs:30000,
  codeCheckIntervalMs:300000,
  freshMs:15000,
  staleMs:30000
});
```

### Browser module API

```js
// browser/live-auction-quorum.js
export {createLiveAuctionPoller, createDirectAuctionPollController, decodeAuctionResult, selectTargetBlock, compareSnapshots, readBoundedJson};

// `fetchImpl`, `now`, `sha256Hex`, and phase AbortController construction are injected for deterministic tests.
// `poll()` returns a typed outcome; `getState()` retains last accepted data and a precise fail-closed reason.
/** @returns {{poll:()=>Promise<LiveAuctionPollOutcome>,getState:()=>LiveAuctionPollState,forceCodeVerification:()=>void,reset:()=>void}} */
export function createLiveAuctionPoller(bootstrap,{fetchImpl,now,sha256Hex,abortControllerFactory}={});
```

```ts
type LiveAuctionObservation={
  blockNumber:number;
  blockHash:string;
  blockTimestampMs:number;
  observedAtMs:number;
  providerCount:number;
  tokenId:string;
  amountWei:string;
  startTimeUnix:string;
  endTimeUnix:string;
  highBidder:string;
  settled:boolean;
  reorged:boolean;
};

type LiveAuctionPollState={
  lastAccepted:LiveAuctionObservation|null;
  freshness:'fresh'|'stale'|'unavailable';
  failureReason:string|null;
};

type LiveAuctionPollOutcome={
  accepted:LiveAuctionObservation|null;
  state:LiveAuctionPollState;
};
```

`poll()` must set and return a fail-closed typed outcome on every error, disagreement, time limit, untrusted code hash, stale target, malformed value, or regression that is not an independently agreed equal-height reorganization. It retains the last accepted observation only in `getState()`, marking it stale with a precise reason rather than pretending that an older static snapshot is newer.

### Dashboard integration API

```js
// Defined inside the generated dashboard script after hydrateCurrentCard.
const normalizeStaticAuctionSnapshot=(context)=>null;
const reconcileAuctionPresentation=({staticContext,directState})=>void 0;
const createDashboardDirectPollController=()=>void 0;
```

`staticContext` is the existing `liveSnapshotContext` plus an exact, unrounded tuple from `current_auction.json`: token ID, amount wei, start/end Unix seconds, lowercase bidder wallet, settled flag, snapshot block number, and snapshot block hash. `reconcileAuctionPresentation()` is the only writer of source/freshness labels and current-card fields after either static hydration or direct-poller state changes.

### Content-addressed module contract

`scripts/build_live_auction_module.py` reads the immutable source `browser/live-auction-quorum.js`, computes SHA-256 over its exact UTF-8 bytes, writes exactly one static file named `public/live-auction-quorum.<digest>.js`, and supports `--check` to reject a missing, stale, duplicate, or byte-mismatched public module. `build_dashboard.py` derives `modulePath` and `moduleSha256` from this validated output; it never reads a module path from metrics, status, or an environment variable. The runner publish allowlist becomes `public/generated` rather than all of `public`, so a data-only runner commit cannot modify either this module or `config/browser-live-auction.json`.

### Task 1: Establish source-pinned configuration, module, and exact static tuple

**Files:**

- Create: `config/browser-live-auction.json`
- Create: `browser/live-auction-quorum.js`, `scripts/build_live_auction_module.py`, and one generated `public/live-auction-quorum.<sha256>.js`
- Modify: `sql/mission3_dashboard.sql`, `scripts/build_dashboard.py:72-110,5534-6010`, and `package.json`
- Modify: `scripts/test_build_dashboard.py:2700-3015`, `scripts/test_validate_dashboard_consistency.py`, `scripts/test_build_live_snapshot_bundle.py`, `scripts/refresh_and_publish.sh`, and `scripts/test_refresh_and_publish.sh`

**Consumes:** existing `AUCTION_HOUSE`, `SELECTOR_AUCTION`, `DEGEN_DOGS`, `metric_lookup()`, `current_auction_source`, and the source-controlled config/module.

**Produces:** `load_browser_live_auction_config() -> dict[str, Any]`, `live_auction_bootstrap() -> dict[str, Any]`, an exact static snapshot tuple, and a deterministic offline `--render-from-generated` build path.

- [ ] **Step 1: Add failing source-boundary tests**

  Add tests that monkeypatch only `BROWSER_LIVE_AUCTION_CONFIG_PATH` and assert the exact source-pinned bootstrap data, fixed provider list, disabled flag, content-addressed module path/digest, and no credential-like URL appear in the generated script. Negative cases must cover a fourth provider, `http`, query/fragment/credentials, wrong chain, mismatched `AUCTION_HOUSE`/`SELECTOR_AUCTION`/`DEGEN_DOGS`, non-lowercase code hash, incorrect `quorum`/`confirmations`, and every disallowed timing/size value.

  Extend SQL/bundle/consistency fixtures to assert that `current_auction.json` contains the unrounded direct-comparison tuple: `token_id`, `amount_wei`, `start_time_unix`, `end_time_unix`, lowercase `bidder_wallet`, `settled`, `snapshot_block_number`, and `snapshot_block_hash`. Assert the rendered bootstrap derives its module filename from bytes matching the public file and reject an extra, stale, or byte-mismatched module file. Add publisher policy tests proving an ordinary current refresh modifies `public/generated` but cannot stage `public/live-auction-quorum.<sha256>.js` or `config/browser-live-auction.json`.

  ```python
  def test_write_html_embeds_valid_disabled_live_auction_bootstrap() -> None:
      dashboard = load_module()
      rendered = render_dashboard_with_metrics(
          dashboard,
          {"onchain_chain_id": "8453"},
      )
      assert "const LIVE_AUCTION_BOOTSTRAP=Object.freeze(" in rendered
      assert "enabled:false" in rendered
      assert "https://mainnet.base.org" in rendered
      assert "https://base-rpc.publicnode.com" in rendered
      assert "https://base-mainnet.g.alchemy.com/public" in rendered
      assert "api_key=" not in rendered.lower()
      assert "0x7d9f6db5" in rendered
      assert dashboard.live_auction_module_path() in rendered
  ```

- [ ] **Step 2: Run the new tests and verify they fail before the boundary exists**

  Run:

  ```powershell
  python scripts/test_build_dashboard.py
  python scripts/test_validate_dashboard_consistency.py
  python scripts/test_build_live_snapshot_bundle.py
  bash scripts/test_refresh_and_publish.sh
  ```

  Expected: failures for missing bootstrap, raw static tuple, content-addressed module, and narrowed publication policy.

- [ ] **Step 3: Add the exact configuration, source module, and module builder**

  Create `config/browser-live-auction.json` with the exact interface above. In `scripts/build_dashboard.py`, add `BROWSER_LIVE_AUCTION_CONFIG_PATH` based on the source file location rather than mutable `ROOT`, then implement strict validation:

  ```python
  def load_browser_live_auction_config() -> dict[str, Any]:
      payload = json.loads(BROWSER_LIVE_AUCTION_CONFIG_PATH.read_text(encoding="utf-8"))
      if set(payload) != {
          "schema_version", "enabled", "chain_id", "auction_house", "dog_nft", "selector",
          "auction_house_code_sha256", "quorum", "confirmations", "providers", "poll_interval_ms",
          "phase_timeout_ms", "max_response_bytes", "retry_max_ms",
          "code_check_interval_ms", "fresh_ms", "stale_ms",
      }:
          raise RuntimeError("live auction quorum config schema is invalid")
      # Require schema 1, a bool, exactly the fixed HTTPS origins, and the exact constants below.
      return payload
  ```

  Require exactly: `quorum=2`, `confirmations=1`, `poll_interval_ms=3000`, `phase_timeout_ms=2500`, `max_response_bytes=1048576`, `retry_max_ms=30000`, `code_check_interval_ms=300000`, `fresh_ms=15000`, and `stale_ms=30000`. `live_auction_bootstrap()` must require exact equality with the existing `AUCTION_HOUSE`, `SELECTOR_AUCTION`, and lowercase `DEGEN_DOGS`, deep-freeze the providers array, validate lowercase address/selector/code-hash syntax, and serialize only source-pinned values. It must not rotate the bytecode pin from mutable metrics or status data.

  Add `browser/live-auction-quorum.js` as the source ESM boundary and `scripts/build_live_auction_module.py` with `--write`/`--check`. It produces exactly one `public/live-auction-quorum.<sha256>.js` whose filename digest and bytes match; it does not read settings from an environment variable.

- [ ] **Step 4: Propagate the exact static tuple and keep Task 1 non-module**

  Add the raw fields to `current_auction` in `sql/mission3_dashboard.sql`, sourcing bid wei and state from `current_auction_source`, converting only validated UTC timestamps to Unix seconds, normalizing the wallet to lowercase, and carrying the attested `latest_block` and `snapshot_block_hash`. Make the live snapshot bundle and consistency validator require the same tuple/block/hash so static/direct equality is actually representable.

  Serialize the validated bootstrap with `json.dumps(..., separators=(",", ":"), sort_keys=True)` and prepend it to the existing inline script, but do **not** import the module, change the script type, or add `script-src 'self'` yet. Task 1 must remain a working disabled intermediate commit before Task 2 creates the full poller.

  Extend only `connect-src` with the three validated origins:

  ```python
  connect_src = "connect-src 'self' " + " ".join(config["providers"])
  ```

  Add `--render-from-generated` to `build_dashboard.py` (and a package script) to load the already-validated generated tables, refresh only `index.html`/README deterministically, and make no RPC, archive, or status write. This is the required offline renderer for enabling or disabling the source-controlled browser flag.

- [ ] **Step 5: Narrow the runner public publish path and run the boundary suite**

  Replace the broad `public` publish path with `public/generated` in the publisher's mutation/rollback allowlist, keeping static images and the new content-addressed module source-owned. Update path inventory/rollback tests. Because the committed preexisting `current_auction.json` does not yet contain raw wei/time fields, run one existing verified current-surface refresh to materialize the new tuple; use `--render-from-generated` only for later configuration-only toggles.

  ```powershell
  python scripts/build_live_auction_module.py --write
  python scripts/build_live_auction_module.py --check
  npm run refresh:current
  python scripts/build_dashboard.py --render-from-generated
  python scripts/test_build_dashboard.py
  python scripts/test_validate_dashboard_consistency.py
  python scripts/test_build_live_snapshot_bundle.py
  bash scripts/test_refresh_and_publish.sh
  ```

  Expected: every boundary test passes; `--render-from-generated` changes HTML only when the input source/config changes.

- [ ] **Step 6: Commit the validated, disabled source boundary**

  ```powershell
  git add config/browser-live-auction.json browser/live-auction-quorum.js scripts/build_live_auction_module.py public/live-auction-quorum.*.js sql/mission3_dashboard.sql scripts/build_dashboard.py scripts/test_build_dashboard.py scripts/test_validate_dashboard_consistency.py scripts/test_build_live_snapshot_bundle.py scripts/refresh_and_publish.sh scripts/test_refresh_and_publish.sh package.json index.html README.md
  git commit -m "feat: add live auction bootstrap attestation"
  ```

### Task 2: Build the bounded RPC codec and quorum selection module

**Files:**

- Modify: `browser/live-auction-quorum.js` and regenerate `public/live-auction-quorum.<sha256>.js`
- Create: `scripts/test_live_auction_quorum.mjs`
- Modify: `package.json`

**Consumes:** `LIVE_AUCTION_BOOTSTRAP` and standard browser `fetch`, `AbortController`, `ReadableStream`, `TextDecoder`, and `crypto.subtle`.

**Produces:** named ES-module exports `readBoundedJson()`, `selectTargetBlock()`, `decodeAuctionResult()`, `compareSnapshots()`, `createLiveAuctionPoller()`, and `createDirectAuctionPollController()`.

- [ ] **Step 1: Add failing Node tests for byte bounds, head selection, and ABI decoding**

  Use only Node built-ins. Import `browser/live-auction-quorum.js` directly and inject fake `Response` objects with `ReadableStream` bodies. Add malformed JSON-RPC version/ID/error cases, invalid address padding, unsafe token/timestamp, zero bidder, and code-byte hashing cases alongside the byte limit/head/ABI tests.

  ```js
  import assert from 'node:assert/strict';
  import test from 'node:test';

  test('selectTargetBlock uses second-highest head minus one confirmation', () => {
    assert.equal(api.selectTargetBlock([101, 100, 1], 1), 99);
    assert.equal(api.selectTargetBlock([100, 100, 99], 1), 99);
  });

  test('decodeAuctionResult keeps a uint256 bid as canonical decimal text', () => {
    const raw = auctionWords(818n, 2n ** 200n, 10n, 20n, ADDRESS, 0n);
    assert.equal(api.decodeAuctionResult(raw).amountWei, String(2n ** 200n));
  });

  test('readBoundedJson aborts a response above one mebibyte', async () => {
    await assert.rejects(() => api.readBoundedJson(oversizedResponse(), 1024), /response exceeds/);
  });
  ```

- [ ] **Step 2: Run the new Node test and verify it fails because the required exports are absent**

  Run:

  ```powershell
  node --test scripts/test_live_auction_quorum.mjs
  ```

  Expected: failure because the Task 1 source stub does not yet export the codec functions.

- [ ] **Step 3: Implement safe transport and pure decoders in the ES module**

  Implement one self-contained ES module, without `eval`, `new Function`, credentials, or dynamically selected RPC origins:

  ```js
  const HEX_64=/^[0-9a-f]{64}$/i;
  export const readBoundedJson=async(response,maxBytes) => {
      if (!response.ok) throw new Error(`RPC unavailable (${response.status})`);
      const length=Number(response.headers.get('content-length') || 0);
      if (Number.isFinite(length) && length > maxBytes) throw new Error('RPC response exceeds limit');
      if (!response.body) throw new Error('RPC response body is unavailable');
      const reader=response.body.getReader(); const chunks=[]; let total=0;
      for (;;) { const {done,value}=await reader.read(); if (done) break; total += value.byteLength;
        if (total > maxBytes) { await reader.cancel(); throw new Error('RPC response exceeds limit'); }
        chunks.push(value);
      }
      return JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(joinChunks(chunks,total)));
  };
  // Define fixed-envelope JSON-RPC helpers, six-word ABI decoding, and named exports here.
  ```

  Every JSON-RPC helper must send a fixed method/parameter envelope and accept only a matching `jsonrpc: "2.0"`, matching request ID, no `error`, and a syntactically valid result. `selectTargetBlock()` must require at least two safe integer heads, return the second-highest head minus the supplied `confirmations`, and reject target block zero. `decodeAuctionResult()` must require exactly `2 + 64 * 6` hexadecimal characters, canonicalize the address to lowercase, reject the zero bidder and non-zero address padding, accept only `0` or `1` for settled, and return canonical decimal strings for every uint256 word (`tokenId`, `amountWei`, `startTimeUnix`, and `endTimeUnix`). It must not use `Number` for ABI words. A block number and timestamp may become a JavaScript number only after a safe-integer check, including the milliseconds conversion. Hash code only after decoding validated even-length hexadecimal code bytes.

- [ ] **Step 4: Add source-level test command and run it**

  Add this package script:

  ```json
  "test:live-auction-quorum": "node --test scripts/test_live_auction_quorum.mjs"
  ```

  Run:

  ```powershell
  npm run test:live-auction-quorum
  ```

  Expected: all codec and bounded-reader tests pass.

- [ ] **Step 5: Commit the pure, dependency-free browser boundary**

  ```powershell
  python scripts/build_live_auction_module.py --write
  python scripts/build_live_auction_module.py --check
  git add browser/live-auction-quorum.js public/live-auction-quorum.*.js scripts/build_live_auction_module.py scripts/test_live_auction_quorum.mjs package.json
  git commit -m "feat: add bounded browser auction quorum codec"
  ```

### Task 3: Implement two-of-three polling, code pinning, and reorganization rules

**Files:**

- Modify: `browser/live-auction-quorum.js` and regenerate `public/live-auction-quorum.<sha256>.js`
- Modify: `scripts/test_live_auction_quorum.mjs`

**Consumes:** Task 1 bootstrap and Task 2 helpers.

**Produces:** `createLiveAuctionPoller(bootstrap, dependencies)` with typed outcomes/state and `createDirectAuctionPollController(dependencies)` with a testable no-overlap scheduler.

- [ ] **Step 1: Add failing poller tests for quorum acceptance and fail-closed paths**

  Add deterministic fakes keyed by URL and method. Cover exact agreement, one-provider agreement, hash disagreement, wrong chain, bytecode mismatch, stale/future block timestamp, equal-height reorg, all-head regression, provider timeout, five-second overall deadline, periodic code verification, and `forceCodeVerification()` after a static attestation change. Require two eligible providers to validate both chain ID and decoded-byte code SHA-256.

  ```js
  test('accepts exactly two matching providers at one confirmed target block', async () => {
    const poller=api.createLiveAuctionPoller(bootstrap,{fetchImpl: agreeingFetch,now: () => BLOCK_TIME_MS + 1000,sha256Hex: async () => CODE_HASH});
    const outcome=await poller.poll();
    assert.deepEqual(outcome.accepted, {
      blockNumber: 100, blockHash: BLOCK_HASH, blockTimestampMs: BLOCK_TIME_MS,
      observedAtMs: BLOCK_TIME_MS + 1000, providerCount: 2, tokenId: '818',
      amountWei: '5500000000000000', startTimeUnix: '1', endTimeUnix: '2',
      highBidder: ADDRESS, settled: false, reorged: false,
    });
  });

  test('never returns a one-provider response', async () => {
    const outcome=await oneMatchingProviderPoller.poll();
    assert.equal(outcome.accepted, null);
    assert.equal(outcome.state.freshness, 'unavailable');
  });

  test('accepts an independently agreed equal-height canonical reorganization', async () => {
    const poller=api.createLiveAuctionPoller(bootstrap,{fetchImpl: sequentialReorgFetch,now,sha256Hex});
    await poller.poll();
    const reorg=(await poller.poll()).accepted;
    assert.equal(reorg.reorged, true);
    assert.equal(reorg.blockNumber, 100);
  });
  ```

  Add controller tests with injected timers, random source, visibility/network signals, and a scripted poller: disabled feature performs zero fetches; hidden/offline aborts scheduling; visibility/online trigger an immediate cycle; no cycles overlap; retry backs off to `retryMaxMs`; success jitter is within `0-250` ms but never makes a three-second cadence miss the eight-second SLO; and a stale outcome produces a renderer callback without replacing last accepted data.

- [ ] **Step 2: Run the poller tests and verify they fail because `createLiveAuctionPoller()` is not implemented**

  Run:

  ```powershell
  npm run test:live-auction-quorum
  ```

  Expected: failure stating `createLiveAuctionPoller` is not a function.

- [ ] **Step 3: Implement the two-phase poller**

  Use an overall five-second monotonic deadline split into a 2500 ms head phase and a 2500 ms target phase; each phase has one shared `AbortController` and every request consumes the remaining phase budget. In the concurrent head phase, request `eth_blockNumber` and `eth_chainId`; only providers reporting `0x2105` are eligible. Select the deterministic target, then in the target phase request a header and code/call material for every eligible provider. Once a provider header hash is known, issue `eth_call` and `eth_getCode` with an EIP-1898 `{blockHash,requireCanonical:true}` tag when supported; otherwise retry only with the exact validated numeric block tag and mark that provider's capability explicitly. Use `Promise.allSettled()` so one bad provider cannot delay a valid quorum.

  ```js
  const poll=async() => {
    const heads=await probeHeadsAndChainsWithinPhaseDeadline();
    const target=selectTargetBlock(heads.map(item => item.head), bootstrap.confirmations);
    const replies=await probeHeadersCallsAndCodeWithinPhaseDeadline(eligible,target);
    const accepted=selectMatchingSnapshot(replies,bootstrap.quorum);
    return accepted ? acceptOrMarkStale(accepted) : failClosed('quorum_disagreement');
  };
  ```

  `probeTarget()` must group the returned header hash, raw `auction()` result, and code SHA from the same provider at the exact target. `selectMatchingSnapshot()` requires `bootstrap.quorum` providers to agree on the complete `(number, hash, call bytes)` tuple and independently validate the source-pinned code SHA; it rejects a block timestamp more than `staleMs` old or more than `staleMs` in the future. Do the initial code check, repeat it only after `codeCheckIntervalMs`, and force it after a new verified static attestation, but never add a third serial phase. `acceptMonotonic()` rejects a lower height, accepts the same height only if block hash and decoded state match, accepts a same-height differing hash only after the same poller receives a two-provider quorum declaring that reorganization, and marks retained state stale when all heads are below accepted height. `getState()` reports that stale/failed state immediately to the controller.

- [ ] **Step 4: Run all Node tests and check source safety**

  Run:

  ```powershell
  npm run test:live-auction-quorum
  rg -n "(eval\\(|new Function|http:|api[_-]?key|token=|Authorization)" browser/live-auction-quorum.js
  ```

  Expected: Node tests pass; `rg` finds no unsafe dynamic execution, insecure endpoint, or credential marker.

- [ ] **Step 5: Commit the quorum poller**

  ```powershell
  python scripts/build_live_auction_module.py --write
  python scripts/build_live_auction_module.py --check
  git add browser/live-auction-quorum.js public/live-auction-quorum.*.js scripts/test_live_auction_quorum.mjs
  git commit -m "feat: verify live auction state across browser RPC quorum"
  ```

### Task 4: Embed the module and merge direct state into the current card

**Files:**

- Modify: `scripts/build_dashboard.py:5534-6010`
- Modify: `scripts/test_build_dashboard.py:2788-3015`
- Modify: `scripts/check_dashboard_ui.py:220-300`

**Consumes:** `LIVE_AUCTION_BOOTSTRAP`, the source-validated content-addressed module, existing `hydrateCurrentCard()`, `refreshLiveSurface()`, and static `liveSnapshotContext` with the exact tuple.

**Produces:** exact direct/static precedence, visible freshness state, safe wallet-only rendering, and direct-poll lifecycle controls.

- [ ] **Step 1: Add failing generator tests for CSP, source label, and non-regressing overlay hooks**

  Add assertions that the generated page has all three exact RPC origins in `connect-src`, one content-addressed module path/digest matching its public bytes, direct freshness text, a single `reconcileAuctionPresentation()` writer, and no lower-static overwrite of newer direct state. Assert the generated module script's CSP hash is calculated from its final bytes and Vite output retains the exact verified module path/file digest. Add a Node-level disabled fixture with counting fake fetch/timers that proves the direct controller makes zero RPC requests.

  ```python
  assert "connect-src 'self' https://mainnet.base.org https://base-rpc.publicnode.com https://base-mainnet.g.alchemy.com/public" in csp
  assert "const reconcileAuctionPresentation=({staticContext,directState})=>" in script
  assert "await import(LIVE_AUCTION_BOOTSTRAP.modulePath)" in script
  assert "staticSnapshot.blockHash===directObservation.blockHash" in script
  assert "Mission 3 auction feed · live onchain block" in script
  ```

- [ ] **Step 2: Run the builder/UI tests and verify the new assertions fail**

  Run:

  ```powershell
  python scripts/test_build_dashboard.py
  python scripts/check_dashboard_ui.py
  ```

  Expected: the new direct-overlay/CSP markers are absent.

- [ ] **Step 3: Import the external module, then add overlay state and rendering**

  In `write_html()`, convert the existing UI code to `<script type="module">` only now that Task 2 exports exist. Load the module only with `await import(LIVE_AUCTION_BOOTSTRAP.modulePath)` after checking the frozen, source-derived content-addressed path/digest. Add `script-src 'self'` plus the inline module hash so this exact static module can load; the generated page/CSP checker must reject any other import path. Keep the script hash calculation after all inline module bytes are final. `public/live-auction-quorum.<sha256>.js` is outside the runner publish allowlist and therefore cannot change in a data-only commit.

  Add direct state variables and functions with these behavior rules:

  ```js
  let staticAuctionContext=null;
  let directAuctionState={lastAccepted:null,freshness:'unavailable',failureReason:null};

  const reconcileAuctionPresentation=({staticContext,directState})=>{
    const staticSnapshot=normalizeStaticAuctionSnapshot(staticContext);
    const directObservation=directState.lastAccepted;
    // Select direct when it is higher, or when same height/hash/tuple agrees.
    // Retain a higher direct observation after its next poll fails, marking it stale.
    // Use static only when its exact tuple/hash is equal or its verified block is higher.
    // A same-height hash change can enter only through a direct reorg outcome.
    if (!directObservation) return renderVerifiedStatic(staticContext);
    const dogUrl=`https://opensea.io/item/base/${LIVE_AUCTION_BOOTSTRAP.dogNft}/${directObservation.tokenId}`;
    const bidderUrl=`https://basescan.org/address/${directObservation.highBidder}`;
    // Write text via textContent. Format ETH directly from amountWei; never use Number.
    // Keep static Farcaster identity only for an exact wallet match.
    // When direct data is ahead, label history 'published', suppress USD/rewards, and clear stale token metadata.
    // On a token transition clear image, traits, rarity, history, and bidder-derived rewards; recompute countdown with BigInt.
  };
  ```

  Add a source label that reports direct source, block, observation age, block age, and `2/3` or `3/3` agreement. Green requires both ages below `freshMs`; amber uses the worse age up to `staleMs`; otherwise retain the last-good page state and explicitly report fallback.

- [ ] **Step 4: Wire independently scheduled static/direct lifecycle with one renderer**

  Start existing static `refreshNow()` and the direct controller independently; do not serialize direct polling behind static snapshot fetch/verification. Create the poller/controller only when `LIVE_AUCTION_BOOTSTRAP.enabled` is true. Wire the controller's injected events to initial visible load, `visibilitychange` to visible, and `online`; stop/abort while hidden or offline. The controller, not the DOM script, owns no-overlap scheduling, a start-time cadence, bounded `0-250` ms success jitter, and exponential retry capped at `retryMaxMs`.

  Replace direct calls to `setVerificationState()`/current-card DOM writers with assignments to `staticAuctionContext` or `directAuctionState` followed by `reconcileAuctionPresentation()`. Normalize every static hydration using raw wei/time/wallet/settled/block/hash fields. When a new verified static attestation is received, call `poller.forceCodeVerification()` before its next direct cycle. A verified static snapshot can replace direct only if it is higher, or it matches every normalized tuple field and hash at the same height; retain newer direct data on later direct failure and label it stale rather than visibly regressing. A same-height differing hash can replace direct only through the direct module's independently agreed reorganization result. If a verified `refresh_status` attestation presents an auction-house code SHA-256 different from `LIVE_AUCTION_BOOTSTRAP.codeSha256`, disable direct polling for the session, retain static content, and render a precise code-pin mismatch fallback reason. Existing static verification errors continue to retain last-good content.

- [ ] **Step 5: Run generated-page validation, build, and Node tests**

  Run:

  ```powershell
  npm run test:live-auction-quorum
  python scripts/build_live_auction_module.py --check
  python scripts/build_dashboard.py --render-from-generated
  python scripts/test_build_dashboard.py
  python scripts/check_dashboard_ui.py
  npm run build
  ```

  Expected: all commands exit `0`; generated `index.html` and built `dist/index.html` have one hashed inline module script, the exact content-addressed source-derived module path/digest, matching CSP hash, and only the approved new `connect-src` origins.

- [ ] **Step 6: Commit the disabled direct overlay**

  ```powershell
  git add browser/live-auction-quorum.js public/live-auction-quorum.*.js scripts/build_live_auction_module.py scripts/build_dashboard.py scripts/test_build_dashboard.py scripts/check_dashboard_ui.py package.json index.html README.md
  git commit -m "feat: add gated live auction dashboard overlay"
  ```

### Task 5: Make the new validation mandatory and document controlled rollout

**Files:**

- Modify: `scripts/run_pages_validation.sh`
- Modify: `package.json`
- Create: `docs/operations/live-auction-quorum.md`
- Modify: `scripts/test_run_pages_validation.sh`

**Consumes:** `npm run test:live-auction-quorum`, existing Pages validation contract, and generated dashboard checks.

**Produces:** a gate that cannot deploy the browser overlay without its deterministic Node tests and a precise operator playbook.

- [ ] **Step 1: Add a failing validation-runner test requiring the Node quorum test**

  In the existing shell test fixture, require a command invocation matching the package script before dashboard validation:

  ```bash
  assert_contains "$validation_script" 'npm run test:live-auction-quorum'
  ```

- [ ] **Step 2: Run the validation-runner test and verify it fails**

  Run:

  ```powershell
  bash scripts/test_run_pages_validation.sh
  ```

  Expected: failure because the new Node test is not yet part of the Pages validation sequence.

- [ ] **Step 3: Add the test to Pages validation and write the operator document**

  Run the Node test after dependencies are installed and before generated-page checks. Extend the dashboard aggregate test script so every full CI run also executes the Node browser-quorum test. The operator document must contain these concrete commands and pass/fail expectations:

  ```bash
  npm run test:live-auction-quorum
  python3 scripts/test_build_dashboard.py
  python3 scripts/check_dashboard_ui.py
  npm run build
  ```

  Document: the public-RPC/IP disclosure, the `enabled:false` starting state, content-addressed module check, how to inspect CSP on production, how to compare the UI block with a two-provider server-side probe, and the exact SLO protocol. The SLO starts when an auction event block has one successor and is retrievable from two configured providers; it ends when an agreed state is committed to the visible DOM with a green freshness indicator. Run 100 deterministic replays in **each** cold-load, warm-load, and visibility-return class, plus 30 production-origin samples in **each** cold-load and warm-load class. Require p95 at or below eight seconds in every class with at least two healthy providers. Document rollback as a source flag plus regenerated HTML commit, not as an unrendered configuration edit.

- [ ] **Step 4: Run the focused validation suite**

  Run:

  ```powershell
  bash scripts/test_run_pages_validation.sh
  npm run test:live-auction-quorum
  python scripts/test_rpc_redaction.py
  python scripts/test_build_dashboard.py
  python scripts/check_dashboard_ui.py
  ```

  Expected: every validation command passes.

- [ ] **Step 5: Commit the rollout guardrails**

  ```powershell
  git add scripts/run_pages_validation.sh scripts/test_run_pages_validation.sh package.json docs/operations/live-auction-quorum.md
  git commit -m "test: gate live auction quorum deployment"
  ```

### Task 6: Deploy disabled code, verify production, then enable in a separate source commit

**Files:**

- Modify: `config/browser-live-auction.json`, then regenerate and commit `index.html` (and README only if the deterministic renderer changes it) after disabled-code production checks pass.

**Consumes:** the fully passing Tasks 1-5 build, existing full Pages workflow, public dashboard URL, and the three public Base RPC origins.

**Produces:** enabled live auction quorum with a verified rollback path.

- [ ] **Step 1: Run the complete pre-deployment suite from a clean checkout**

  Run:

  ```powershell
  git status --short
  npm run test:live-auction-quorum
  python scripts/build_live_auction_module.py --check
  python scripts/test_build_dashboard.py
  python scripts/check_dashboard_ui.py
  bash scripts/test_run_pages_validation.sh
  npm run build
  ```

  Expected: clean status before generating artifacts; every command exits `0`.

- [ ] **Step 2: Commit and push the disabled implementation through the existing full Pages gate**

  Run only after the task commits are reviewed:

  ```powershell
  git push origin main
  ```

  Expected: the existing full workflow succeeds and the public page retains static behavior because `enabled` is false; it must make zero browser RPC requests.

- [ ] **Step 3: Probe production origin and exact CSP before enabling**

  Run a read-only PowerShell probe:

  ```powershell
  $page = Invoke-WebRequest 'https://ael-dev3.github.io/Degen-Dogs-Mission-3/' -Headers @{ 'Cache-Control'='no-cache' }
  $page.StatusCode
  $page.Content -match "connect-src &#x27;self&#x27; https://mainnet.base.org https://base-rpc.publicnode.com https://base-mainnet.g.alchemy.com/public"
  ```

  Expected: status `200` and `True`. Extract the production module path, fetch that exact content-addressed file, and compare its SHA-256 to the bootstrap value. Then run one `eth_chainId` request with `Origin: https://ael-dev3.github.io` against each fixed endpoint and require `0x2105` plus an allowed CORS origin.

- [ ] **Step 4: Enable the feature in its own reviewed commit**

  Change the config boolean, then deterministically regenerate the deployed HTML:

  ```json
  "enabled": true
  ```

  Run the offline renderer, module checker, complete pre-deployment suite, then commit the config and generated HTML together:

  ```powershell
  python scripts/build_dashboard.py --render-from-generated
  python scripts/build_live_auction_module.py --check
  git add config/browser-live-auction.json index.html README.md
  git commit -m "feat: enable live auction quorum"
  $enableCommit = git rev-parse HEAD
  git push origin main
  ```

- [ ] **Step 5: Verify live behavior and SLO, with an immediate rollback rule**

  Open a fresh production page and a backgrounded-then-visible page. Confirm the label identifies live on-chain data, a Base block, both ages, and at least `2/3` agreement. Independently query two fixed RPC providers at the displayed block and compare the raw `auction()` result.

  If two-provider agreement cannot be obtained, if a direct field regresses, if a stale direct block is green, or if p95 exceeds eight seconds in any documented required class, revert the recorded enable commit (which restores both the flag and rendered bootstrap):

  ```powershell
  git revert --no-edit $enableCommit
  git push origin main
  ```

  Expected after rollback: the dashboard reverts to the existing verified Pages-only live snapshot without data deletion or runner changes.
