# Windows WSL2 runner

This is the supported design for adding a Windows PC as a second Degen Dogs
publisher. It is a local data producer, not a GitHub Actions self-hosted runner:

```text
Windows Task Scheduler keepalive
  -> isolated WSL2 Ubuntu distro with systemd
     -> 15-second event watcher (durable latest-wins queue, no archive)
        -> path-triggered queued publisher (fixed drainer, bounded timer fallback)
           -> path-triggered immutable Pages verifier (bounded timer fallback)
     -> staggered hourly publisher (bounded refresh, optional incremental archive)
     -> five-minute local/live health probe
        -> Git push to main
           -> existing CI and GitHub Pages deployment
```

Native Windows and Git Bash are not supported for production publication. The
runner imports `fcntl`, inherits advisory-lock descriptors across `/bin/bash`,
checks Unix ownership/mode/link counts, terminates Unix process groups, and uses
`lsof`. A native port would replace several correctness and crash-recovery
guarantees. Keep the clone in WSL's ext4 VHD, never under `/mnt/c` or `/mnt/d`.

## Safety gate

Keep the PC units disabled until all of these are true:

1. The peer-aware publisher and its compare-and-swap collision/recovery
   regression suite are merged to `main`; the WSL clone is clean and current.
2. `.env.local` contains at least two independently operated, credentialed,
   archive-capable endpoints in both `BASE_RPC_URLS` and `BASE_LOG_RPC_URLS`.
   Three operators with a quorum of two gives better outage tolerance.
3. The PC's unique public deploy key is added to the GitHub repository with
   write access. The private key stays mode `0600` inside the WSL VHD.
4. The installer's exact production-shaped Dog NFT rarity-topic RPC quorum
   probe, watcher dry-run, exact-origin check, `git ls-remote`, and
   `git push --dry-run` all succeed.
5. An operator has reviewed and pinned the exact commit containing the
   privileged installer, systemd, anchor, and logrotate assets. The deploy key
   is never allowed to select or update that root-owned bundle.

The installer enforces this gate. Even its internal enable pass leaves services
behind an absent activation marker until the Windows keepalive is verified.
The queued publisher/verifier rollout is not activated by this asset change:
production installation and activation are a separate reviewed Task 8 step.

## Why two publishers are now safe

The filesystem lock at `/var/cache/degen-dogs/refresh.lock` serializes the PC's
watcher and hourly job. It cannot lock the Mac mini. Cross-host safety therefore
comes from the publisher's compare-and-swap Git checks and semantic peer
supersession handling. A peer commit is accepted only when it is a safe
fast-forward and its verified snapshot covers the local event; otherwise the
event remains pending and is regenerated from the new remote baseline.

This protocol assumes every credential or deploy key allowed to write `main`
is fully trusted. Compare-and-swap and recovery protect against accidental
concurrent writers and crashes; they do not authenticate or contain a malicious
push-authorized peer. Runner IDs and commit trailers are provenance labels, not
cryptographic signatures, so do not treat an allowlist of forgeable runner IDs
as a security boundary. Keep the PC deploy key dedicated to this runner, grant
only the repository access it needs, and never reuse it on another host.

Never add an unqualified `--force`, a blind/default `--force-with-lease`, an
automatic generated-file merge, or a blind rebase. Preserve the publisher's
exact immutable `--force-with-lease=<ref>:<baseline>` compare-and-swap; changing
that expected baseline can replace newer verified data with an older snapshot.

The PC watcher runs at seconds `07,22,37,52`, offset from the other host's usual
15-second cycle. The hourly job runs at minute `59`; the observed Mac
publisher starts near minute `29`, producing an approximately 30-minute
aggregate baseline. This staggering is only a best-effort reduction in duplicate
work because the Mac schedule can drift; compare-and-swap remains the correctness
boundary. Re-measure and adjust `config/systemd/degen-dogs-hourly.timer` if the
Mac phase changes.

## Windows and WSL prerequisites

Run Windows Update first. In firmware, enable virtualization and configure
automatic power-on after AC recovery if the PC supports it. Wired Ethernet and
a UPS materially improve uptime.

The current PC is already suitable: WSL 2.7 is installed, the active AC plan
does not sleep or hibernate, and `.wslconfig` allocates 8 processors, 20 GB RAM,
8 GB swap, mirrored networking, DNS tunnelling, firewall integration, and
gradual memory reclaim. A matching optional user-level configuration is:

```ini
[wsl2]
memory=20GB
processors=8
swap=8GB
networkingMode=mirrored
firewall=true
dnsTunneling=true
guiApplications=false

[experimental]
autoMemoryReclaim=gradual
```

Do not reduce the PC to a sleep-capable AC plan. Verify from elevated
PowerShell:

```powershell
powercfg /getactivescheme
powercfg /query scheme_current sub_sleep standbyidle
powercfg /query scheme_current sub_sleep hibernateidle
powercfg /requests
```

If needed, preserve always-on AC behavior:

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

The observed battery policy sleeps after ten minutes. Change it only if this is
a laptop that must publish while unplugged; otherwise keep the safer battery
policy.

## Exact-commit bootstrap, disabled by default

The PowerShell bootstrap installs an isolated Ubuntu 24.04 WSL2 distro,
enables systemd, installs Node 22/Python/Git/runtime packages, creates the
unprivileged `degendogs` Linux user, clones the public repository to ext4 at
`/srv/degen-dogs/repo`, creates a hash-locked Python venv, installs npm
dependencies, runs the nonrecursive WSL publication regression suite and build,
generates a unique deploy key, and registers a **disabled** Windows keepalive
task.

Choose the exact 40-character commit containing the reviewed runner assets,
then make a separate detached checkout for that object. For the recommended
pre-login service, use an elevated PowerShell window. Run the bootstrap from
that checkout and treat it as read-only; do not run a convenient copy from a
development worktree. Do not substitute a branch name or `HEAD`:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
$trustedCommit = '<reviewed-40-character-commit-on-main>'
$bootstrapRoot = Join-Path $env:TEMP (
  'DegenDogsBootstrap-' + [Guid]::NewGuid().ToString('N')
)
git clone --filter=blob:none --no-checkout `
  https://github.com/ael-dev3/Degen-Dogs-Mission-3.git $bootstrapRoot
git -C $bootstrapRoot checkout --detach $trustedCommit
if ((git -C $bootstrapRoot rev-parse HEAD) -cne $trustedCommit) {
  throw 'Detached bootstrap checkout does not equal the reviewed commit.'
}
if (git -C $bootstrapRoot status --porcelain=v1 --untracked-files=no) {
  throw 'Detached bootstrap checkout has tracked changes.'
}
$bootstrapScript = Join-Path $bootstrapRoot 'scripts\install_wsl_startup_task.ps1'
(Get-Item -LiteralPath $bootstrapScript).IsReadOnly = $true
& $bootstrapScript -TrustedInstallerCommit $trustedCommit
```

An independently verified read-only `git archive` of the same commit is also
acceptable. Before any WSL, Task Scheduler, or package mutation, the script
fetches public `main` into a temporary bare repository, proves the supplied
commit is an ancestor, and requires its own unfiltered Git blob ID to equal the
script blob at that exact commit. When it detects a surrounding Git checkout,
it additionally requires exact `HEAD` equality and no tracked changes. It then
fetches that exact commit inside WSL into a root-only bare repository, archives
only the privileged asset allowlist, and records SHA-256 hashes. Ordinary
bootstrap, `-Activate`, and `-Uninstall` runs require the same exact commit
argument and attest both the elevated script and the installed frozen bundle
before changing host state. Supplying the already installed commit does not
require `-UpgradeTrustedBundle`; a different commit does. Runtime dashboard
code may fast-forward as the unprivileged user, but it is never executed by
root.

This source self-check prevents accidental commit/checkout mismatch. It is not
a malware boundary: a malicious local PowerShell file, Git executable,
administrator, network trust store, or already-compromised host can lie about
or remove its own checks. Review the bootstrap before elevation and keep the
detached checkout isolated until installation and activation finish.

### No-UAC current-user fallback

When WSL2 is already operational but Windows elevation is unavailable, the
same exact detached checkout supports a current-user fallback from an ordinary
PowerShell window. Bootstrap it in disabled state with:

```powershell
& $bootstrapScript -TrustedInstallerCommit $trustedCommit -AtLogOnOnly
```

For a missing runner distro, this mode imports Canonical's versioned Ubuntu
24.04.4 AMD64 WSL image beneath the Windows `LocalApplicationData` known
folder. The image URL and SHA-256 are pinned together in reviewed code; the
script downloads over HTTPS and verifies the exact digest before
`wsl --import`. A current-user/distro file lock serializes install, activation,
uninstall, and recovery. Each import receives a unique location and durable
host receipt; rollback may unregister or remove files only when that exact
attempt token matches the WSL registration. Every existing directory from the
known-folder root through the import is rechecked and reparse points are
rejected. It never copies the unrelated default Ubuntu distro and never enables
Windows features or machine policy.

Every mode holds current-user/task-name and current-user/distro locks for the
full lifecycle.
Immediately before registration, the installer re-attests and removes only an
exact managed predecessor; registration never force-overwrites a task that
appears concurrently.

The disabled Task Scheduler definition is attested before use: it belongs to
the current SID, uses `InteractiveToken` and least privilege, has exactly one
logon trigger plus the five-minute watchdog, contains no boot trigger, and
invokes only the exact System32 `wsl.exe` anchor action. After the deploy key
and RPC configuration are ready, activate with:

```powershell
& $bootstrapScript -TrustedInstallerCommit $trustedCommit -Activate -AtLogOnOnly
```

This fallback starts only after that Windows user signs in and cannot recover
while the user is signed out. It also cannot repair Windows Time, sleep, or
power policy. Keep the user signed in and prefer the password-backed elevated
task when true boot/pre-login uptime becomes available. Rotate the Ubuntu image
pin only by reviewing and changing its official versioned URL and checksum
together in a new trusted commit.

The script prints only the public deploy key. Add that public key at:

```text
GitHub repository -> Settings -> Deploy keys -> Add deploy key
```

Enable **Allow write access**. The SSH client uses `IdentitiesOnly=yes`, strict
host-key checking, and GitHub's documented ED25519 host key/fingerprint. It
never uses `ssh-keyscan` and never prints the private key. The bootstrap also
verifies the downloaded NodeSource repository signing key against its pinned
full fingerprint before installing the keyring or trusting the apt source.

Fill the protected WSL configuration:

```powershell
wsl.exe -d DegenDogsRunner -u degendogs -- nano /srv/degen-dogs/repo/.env.local
```

Start from `config/wsl-runner.env.template`. Use separate PC provider keys or
quotas where possible; reusing the Mac's exact quotas makes both runners fail
together under rate limiting.

The hourly launcher accepts only `DEGEN_DOGS_RUN_MISSION3_ARCHIVE=0` or `1` from
that protected file. Its absent-key fallback remains `1` for backward
compatibility, but the fresh WSL template explicitly sets `0` because a new
clone does not contain the ignored Mission 3 SQLite/raw archive. Keep `0` for a
latency-only peer; change it to `1` only after seeding an archive-capable runner
that is responsible for incremental archive freshness. The 15-second watcher
always forces archive work off regardless of this setting, and the launcher
continues to pin the Git remote, branch, pull, and push policy after loading
local configuration.

After the race-safe code is on `main`, RPCs are configured, and the public key
has write access, activate with a Windows credential. Task Scheduler stores the
credential securely so the same Windows account that owns the WSL distro can
start it before interactive login:

```powershell
$credential = Get-Credential "$env:USERDOMAIN\$env:USERNAME"
& $bootstrapScript -TrustedInstallerCommit $trustedCommit -Activate -Credential $credential
```

If the Windows account uses Windows Hello without a reusable password, use the
current-user fallback described above:

```powershell
& $bootstrapScript -TrustedInstallerCommit $trustedCommit -Activate -AtLogOnOnly
```

That mode cannot start WSL until the user logs on after a reboot and stays
signed in. Prefer the password-backed task for a truly unattended runner.

Activation runs the read-only RPC watcher preflight and Git write dry-run before
enabling either publisher. The Task Scheduler action keeps one root anchor
attached to WSL, while all network/data/Git work runs as the unprivileged Linux
user. `ProtectHome=read-only` lets SSH read its key but prevents services from
changing it; npm writes to `/var/cache/degen-dogs/npm` instead of the home
directory.

Updating privileged assets is a separate reviewed operation. Supply the new
exact commit and activate in the same run so a healthy old runner is not left
intentionally quiesced. Create a new isolated detached checkout using the same
verification block above, set `$newBootstrapScript` to its read-only script, and
then run:

```powershell
$newTrustedCommit = '<new-reviewed-40-character-commit-on-main>'
$newBootstrapRoot = Join-Path $env:TEMP (
  'DegenDogsBootstrap-' + [Guid]::NewGuid().ToString('N')
)
git clone --filter=blob:none --no-checkout `
  https://github.com/ael-dev3/Degen-Dogs-Mission-3.git $newBootstrapRoot
git -C $newBootstrapRoot checkout --detach $newTrustedCommit
if ((git -C $newBootstrapRoot rev-parse HEAD) -cne $newTrustedCommit) {
  throw 'Detached upgrade checkout does not equal the reviewed commit.'
}
if (git -C $newBootstrapRoot status --porcelain=v1 --untracked-files=no) {
  throw 'Detached upgrade checkout has tracked changes.'
}
$newBootstrapScript = Join-Path $newBootstrapRoot 'scripts\install_wsl_startup_task.ps1'
(Get-Item -LiteralPath $newBootstrapScript).IsReadOnly = $true
$credential = Get-Credential "$env:USERDOMAIN\$env:USERNAME"
& $newBootstrapScript `
  -UpgradeTrustedBundle `
  -TrustedInstallerCommit $newTrustedCommit `
  -Activate `
  -Credential $credential
```

Immediately before each runtime installer/activation pass, the bootstrap
independently fetches public `main` into a root-only temporary bare repository,
exports its exact tree as a read-only manifest, and fast-forwards the
unprivileged clone to that same SHA. It refuses dirty, non-main, divergent,
unexpected-origin, locally-ahead, hidden-index, or byte-different clones.
Activation separately fetches the configured SSH origin and requires
`HEAD == origin/main`.

The dependency/test pass removes any earlier receipt before it starts. It runs
the publication-state, watcher, queue-drainer, detached-verifier, publisher,
telemetry, runner-health, and delayed-publication integration tests once as the
unprivileged runner, then performs the dashboard build. Only that complete pass
may atomically create
`/var/lib/degen-dogs/bootstrap-test-receipt.json`. The private root-owned receipt
is bound to the exact runtime commit, the exact frozen trusted-installer commit,
and its strict schema. The separate `-Activate` pass uses `--skip-bootstrap` and
refuses a missing, legacy, malformed, linked, non-private, wrong-schema, or
commit-mismatched receipt; changing the trusted installer therefore invalidates
the previous test policy even when the runtime commit is unchanged. Portable
asset checks, Windows policy checks, and the privileged rendered-systemd
isolation proof remain separate release gates and are not recursively executed
by this bootstrap suite.

On upgrades, the bootstrap first removes the persistent/runtime activation
markers, disables the keepalive, disables every timer and path unit, waits for
watcher/hourly/health/publisher/verifier services to stop, verifies every old
unit is inactive, and terminates only the isolated runner distro. This prevents
a live publisher from modifying the clone while Git is fast-forwarded or unit
files are replaced. Any quiesce, sync, preflight, or task-start failure leaves
the Windows keepalive and every Linux activation unit disabled.

Activation is a two-phase commit. The installer can enable unit files, but each
service has `ConditionPathExists=/run/degen-dogs/activation-enabled`. Only after
Task Scheduler reports the keepalive running and the WSL anchor publishes its
ready signal does PowerShell atomically create the persistent armed marker and
runtime marker, start the timers/path units, and verify all four new activation
units are active and both trigger-started services are loaded/not failed.
`/run` is cleared on reboot;
the scheduled anchor recreates the runtime marker only when the root-owned
persistent marker exists. A setup crash cannot leave an unverified publisher
able to start at the next boot.

## Privileged installation boundary

Never run `sudo bash scripts/install_wsl_runner.sh` from the runner-owned clone.
Git index flags and configuration can hide worktree changes, so doing that
would turn a deploy-key compromise into root execution. The Linux installer is
an internal entrypoint invoked only through the hash-verified frozen wrapper at
`/usr/local/libexec/degen-dogs-wsl-installer`, with a root-owned exact runtime
manifest supplied by the Windows bootstrap. Use the PowerShell commands above
for install, activation, and trusted-bundle upgrades.

The systemd assets are:

- `degen-dogs-watcher.service` / `.timer`: one-shot check every 15 seconds;
- `degen-dogs-hourly.service` / `.timer`: minute-59 bounded reconcile, with
  optional incremental archive maintenance;
- `degen-dogs-health.service` / `.timer`: five-minute health and freshness;
- `degen-dogs-publisher.service` / `.path` / `.timer`: drain only
  `/var/cache/degen-dogs/publication/latest.json` immediately, with a fast
  bounded fallback retry;
- `degen-dogs-pages-verifier.service` / `.path` / `.timer`: verify only
  `/var/cache/degen-dogs/publication/pending.json`, with a sub-minute bounded
  fallback retry;
- `degen-dogs-runner.target`: groups the four publisher/verifier path/timer
  activation units; the bounded oneshot services remain trigger-started.

Services use bounded timeouts, control-group termination, a narrow runtime
PATH, empty capability sets, read-only system/home mounts, and explicit write
access only to the clone, log directory, and cache/lock directory. The watcher
does not self-restart or use a service start limit: its 15-second timer is the
single retry owner, so repeated successful checks cannot suppress later event
checks. The hourly and health services retain bounded retry backoff. Event
refreshes keep archive work off for latency. On a seeded archive-capable runner,
the staggered hourly job also maintains the archive; a latency-only peer may opt
out as described above.

The WSL watcher alone pins `MISSION3_WATCHER_PUBLICATION_MODE=queue` after the
protected environment file is loaded. Repository, environment-template, and
Mac launchd defaults remain `inline`; archive behavior is unchanged. The
publisher service executes only
`/srv/degen-dogs/repo/scripts/run_wsl_runner_job.sh publisher`, which selects
only `drain_publication_queue.py`, and may write the
repository, log, and lock directories. The verifier service executes the same
fixed launcher with only the `verifier` branch, which selects only
`verify_pages_deployment.py`; systemd mounts the repository read-only and
allows writes only below `/var/log/degen-dogs` and `/var/cache/degen-dogs`.
Verifier exit `2` means the exact pending proof is unresolved or waiting for
its authenticated journal, so systemd treats it as a successful bounded wait
and the path/timer retries later.

## Verify operation

From the same Windows account (elevation is needed only for the pre-login
mode):

```powershell
Get-ScheduledTask -TaskName 'Degen Dogs WSL Runner'
Get-ScheduledTaskInfo -TaskName 'Degen Dogs WSL Runner'
wsl.exe -d DegenDogsRunner -u root -- systemctl list-timers 'degen-dogs-*'
```

The five-minute watchdog deliberately uses `MultipleInstances=IgnoreNew`. Treat
Task Scheduler result `0x800710E0` as `watchdog launch suppressed (healthy)`
only when the exact task is enabled and `Running`, its `IgnoreNew` policy has
passed XML attestation, Task Scheduler reports exactly one running instance,
and the Linux anchor, activation marker, and timers have all passed the final
liveness check. The installer prints that classification only after proving all
of those conditions. If any proof is missing, it leaves the raw result
unsuppressed; do not add a second task or weaken the single-instance policy.

From WSL:

```bash
systemctl status degen-dogs-watcher.timer degen-dogs-hourly.timer degen-dogs-health.timer \
  degen-dogs-publisher.path degen-dogs-publisher.timer \
  degen-dogs-pages-verifier.path degen-dogs-pages-verifier.timer
systemctl status degen-dogs-watcher.service degen-dogs-hourly.service \
  degen-dogs-publisher.service degen-dogs-pages-verifier.service
journalctl -u degen-dogs-publisher.service -u degen-dogs-pages-verifier.service --since '1 hour ago'
tail -n 80 /var/log/degen-dogs/watch-onchain.log
tail -n 80 /var/log/degen-dogs/refresh.log
tail -n 80 /var/log/degen-dogs/pages-verifier.jsonl
sudo -u degendogs bash -p /srv/degen-dogs/repo/scripts/run_wsl_runner_job.sh preflight
sudo -u degendogs bash -p /srv/degen-dogs/repo/scripts/run_wsl_runner_job.sh health
```

A one-shot watcher service is normally inactive between checks. Judge it by
the timer's active state, the service `Result=success`, advancing
`.local/mission3_onchain_tracker_state.json`, and the health report. A transient
failure changes `Result`; a later successful retry returns it to `success`.

Application logs and JSONL telemetry rotate at 8 MiB with eight compressed
generations. Journald retains the systemd exit history. The health probe also
checks timer state, worker results, pending watcher events, consecutive RPC and
refresh failures, the latest terminal publication result, the shared lock,
tracked Git dirt, local/remote status, GitHub Pages propagation, log growth, and
5 GiB/5% disk-free thresholds. A fresh generated status cannot hide a failed or
explicitly non-pushed terminal publication.

The existing off-host GitHub freshness watchdog remains independent of both
machines and continues to open/update incidents when raw `main` or Pages is
stale.

## Maintenance and removal

Stop the Windows keepalive before deliberately disabling timers or path units;
the anchor repairs stopped activation units once per minute.

Quiesce and rollback remove both activation markers first, disable all timers
and path units, stop every worker including publisher/verifier, and fail closed
unless every unit is inactive. Uninstall removes all six queued-worker unit
files while preserving the repository, protected environment, SSH key, logs,
and queue/cache state for operator recovery.

### Deploy-key and pinned-key rotation

Rotate the PC deploy key during a planned maintenance window; never overwrite
the active private key while a publisher can run. Use the exact detached
bootstrap script above to uninstall the task/services (the distro, repository,
configuration, and keys are preserved), generate a new ED25519 key at a
temporary sibling path as `degendogs`, and add only its public half to GitHub as
a second write-enabled deploy key. Test that candidate with the pinned
`degen_dogs_known_hosts` file, `IdentitiesOnly=yes`, `BatchMode=yes`, strict
host-key checking, no proxy, `git ls-remote`, and a push dry-run. Only after
those checks pass should the operator atomically move the old fixed-path key to
a private backup and the candidate into
`~/.ssh/degen_dogs_windows_ed25519`, then rerun the exact-commit bootstrap and
activation. Verify a publication from `windows-wsl`; only then delete the old
GitHub deploy key and its private backup. Never put either private key or its
backup in the repository, terminal transcript, or ticket.

Upstream authentication-key changes require code review, not a live fallback.
For a GitHub SSH host-key rotation, verify the new key and full fingerprint in
GitHub's official fingerprint documentation, update the byte-exact managed
`known_hosts` content and fingerprint regression together, and deploy that
reviewed exact commit. Never replace the pin with `ssh-keyscan`. For a
NodeSource signing-key rotation, obtain the key from its official HTTPS source,
independently verify the full primary fingerprint, update the pinned fingerprint
and regression in one reviewed commit, and use `-UpgradeTrustedBundle`. If the
old key is unexpectedly rejected before that review completes, leave the runner
disabled.

Remove the task and services while preserving the distro, repository, private
configuration, SSH key, logs, and caches:

```powershell
& $bootstrapScript -TrustedInstallerCommit $trustedCommit -Uninstall
```

For a non-elevated current-user installation, preserve the mode selection:

```powershell
& $bootstrapScript -TrustedInstallerCommit $trustedCommit -AtLogOnOnly -Uninstall
```

Linux-only removal is also available:

```bash
sudo /usr/local/libexec/degen-dogs-wsl-installer --uninstall
```

Back up the isolated distro after a healthy activation:

```powershell
wsl.exe --export DegenDogsRunner D:\Backups\DegenDogsRunner.tar.gz --format tar.gz
```

Never export `.env.local`, the private SSH key, caches, logs, or the WSL image
into the Git repository.
