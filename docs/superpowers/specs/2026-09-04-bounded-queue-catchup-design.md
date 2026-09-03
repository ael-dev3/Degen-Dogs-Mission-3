# Bounded Queue Catch-up Budget Design

**Status:** Approved for implementation under the operator's standing instruction to choose and deploy the fastest reliable design.

## Objective

Keep the low-latency publisher bounded while giving a legitimate archive catch-up enough time to finish. The current queue drainer uses a hard-coded 240-second total budget and reserves cleanup time from it. During this incident, a correct single-worker archive scan reached 775 of 1,032 ranges before the drainer terminated it at about 230 seconds. The staged archive is intentionally all-or-nothing, so the next retry had to start the same scan again.

The service currently has a five-minute systemd start timeout. Raising only the Python budget would therefore move the termination boundary into systemd and would not make recovery reliable.

## Safety invariants

- The publisher remains a non-agentic, deterministic local program.
- Runtime remains strictly bounded at both the Python process and systemd unit.
- The queue, authenticated journal, archive staging, validation, Git compare-and-swap, and Pages verification protocols remain unchanged.
- Timeout cleanup continues to terminate and reap the complete child process group before releasing the shared publication lock.
- Invalid configuration fails closed with exit code 78 before a publisher child is launched.
- The runtime-budget control is never forwarded as dynamic input to the publisher child.
- Existing injectable sub-minute budgets remain available to unit tests; operator bounds apply only to environment configuration accepted by `main()`.
- Health must not classify a fresh, progressing publication as stale before its configured bounded runtime can expire.

## Design

Add `DEGEN_DOGS_QUEUE_RUNTIME_BUDGET_SECONDS` as a whole-process queue-drainer budget. Its default is 900 seconds, its minimum accepted operator value is 300 seconds, and its maximum is 2,700 seconds. The 15-minute default covers the observed single-worker archive scan plus bounded surface generation, validation, publication, and cleanup margin. The 45-minute ceiling preserves a finite upper bound under degraded but progressing provider conditions.

Only an absent value selects the default. After the existing protected-environment loader has normalized unquoted assignment whitespace, a present value must be a canonical ASCII positive decimal integer with no sign, decimal point, embedded or quoted whitespace, exponent, or leading zero. Values outside 300 through 2,700 inclusive are configuration errors. Error diagnostics use a fixed message and never echo the rejected value.

Parse this control only in `main()` and pass the resulting float to `drain_publication_queue()`. Do not enforce the operator range inside the injectable function because existing deterministic tests use shorter synthetic deadlines. The existing `DEGEN_DOGS_QUEUE_` dynamic-field filter removes the setting from the child environment.

Raise `degen-dogs-publisher.service` `TimeoutStartSec` to 50 minutes. This is five minutes longer than the maximum Python budget, so Python retains responsibility for ordered process-group cleanup and systemd remains an independent final bound. Keep `TimeoutStopSec=30s` and `KillMode=control-group` unchanged.

Align queue health with the same validated setting. An inactive queued observation remains stale after 180 seconds. While the shared lock and matching authenticated publication journal prove that the exact queued generation is actively being processed, use the configured runtime budget plus 180 seconds as the stale boundary. This is 1,080 seconds by default and at most 2,880 seconds, still below systemd's 3,000-second hard stop. Health consumes the drainer's parser and constants instead of defining a second accepted-value contract. A configured catch-up can progress without a false failure, while a stuck process, missing journal, expired budget, or inactive old queue remains visible.

Document the default and accepted range in the protected environment template and WSL operations guide. The setting belongs in `.env.local`; the fixed launcher loads it before invoking the drainer.

## Acceptance criteria

- Absent configuration passes exactly 900 seconds to the drainer.
- Boundary values 300 and 2,700 are accepted.
- Non-canonical, zero, below-minimum, and above-maximum values return exit 78 without launching the drainer.
- Directly injected short budgets used by unit tests still work.
- The child publisher environment does not contain the queue budget control.
- Active queue health uses the configured budget plus 180 seconds; inactive queue health still fails after 180 seconds.
- The installed publisher unit attests exactly `TimeoutStartSec=50min`.
- WSL asset, queue-drainer, and installer regression suites pass.
- The preserved real queue completes through its authenticated journal, validation, Git push, Pages proof, and finalization under the new bounded service window.
