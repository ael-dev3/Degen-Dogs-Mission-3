# Windows WSL2 runner

This is the supported design for adding a Windows PC as a second Degen Dogs
publisher. It is a local data producer, not a GitHub Actions self-hosted runner:

```text
Windows Task Scheduler keepalive
  -> isolated WSL2 Ubuntu distro with systemd
     -> 15-second event watcher (bounded current refresh, no archive)
     -> staggered hourly publisher (incremental archive + bounded refresh)
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

## Why two publishers are now safe

The filesystem lock at `/var/cache/degen-dogs/refresh.lock` serializes the PC's
watcher and hourly job. It cannot lock the Mac mini. Cross-host safety therefore
comes from the publisher's compare-and-swap Git checks and semantic peer
supersession handling. A peer commit is accepted only when it is a safe
fast-forward and its verified snapshot covers the local event; otherwise the
event remains pending and is regenerated from the new remote baseline.

Never add an unqualified `--force`, a blind/default `--force-with-lease`, an
automatic generated-file merge, or a blind rebase. Preserve the publisher's
exact immutable `--force-with-lease=<ref>:<baseline>` compare-and-swap; changing
that expected baseline can replace newer verified data with an older snapshot.

The PC watcher runs at seconds `07,22,37,52`, offset from the other host's usual
15-second cycle. The hourly/archive job runs at minute `59`; the observed Mac
publisher starts near minute `29`, producing an approximately 30-minute
aggregate baseline. Re-measure and adjust
`config/systemd/degen-dogs-hourly.timer` if the Mac phase changes.

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

## One-command bootstrap, disabled by default

The PowerShell bootstrap installs an isolated `Ubuntu-24.04` WSL2 distro,
enables systemd, installs Node 22/Python/Git/runtime packages, creates the
unprivileged `degendogs` Linux user, clones the public repository to ext4 at
`/srv/degen-dogs/repo`, creates a hash-locked Python venv, installs npm
dependencies, generates a unique deploy key, and registers a **disabled**
Windows keepalive task.

Choose the exact 40-character commit containing the reviewed runner assets,
then run the first bootstrap from elevated PowerShell. Do not substitute a
branch name or `HEAD`:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
$trustedCommit = '<reviewed-40-character-commit-on-main>'
.\scripts\install_wsl_startup_task.ps1 -TrustedInstallerCommit $trustedCommit
```

The script fetches that exact commit into a root-only bare repository, verifies
that it is on the public repository's `main` history, archives only the
privileged asset allowlist, and records SHA-256 hashes. Ordinary bootstrap and
`-Activate` runs use this frozen bundle and never replace it from floating
`main`. Runtime dashboard code may fast-forward as the unprivileged user, but it
is never executed by root.

The script prints only the public deploy key. Add that public key at:

```text
GitHub repository -> Settings -> Deploy keys -> Add deploy key
```

Enable **Allow write access**. The SSH client uses `IdentitiesOnly=yes`, strict
host-key checking, and GitHub's documented ED25519 host key/fingerprint. It
never uses `ssh-keyscan` and never prints the private key.

Fill the protected WSL configuration:

```powershell
wsl.exe -d DegenDogsRunner -u degendogs -- nano /srv/degen-dogs/repo/.env.local
```

Start from `config/wsl-runner.env.template`. Use separate PC provider keys or
quotas where possible; reusing the Mac's exact quotas makes both runners fail
together under rate limiting.

After the race-safe code is on `main`, RPCs are configured, and the public key
has write access, activate with a Windows credential. Task Scheduler stores the
credential securely so the same Windows account that owns the WSL distro can
start it before interactive login:

```powershell
$credential = Get-Credential "$env:USERDOMAIN\$env:USERNAME"
.\scripts\install_wsl_startup_task.ps1 -Activate -Credential $credential
```

If the Windows account uses Windows Hello without a reusable password, a
reduced-uptime fallback is available:

```powershell
.\scripts\install_wsl_startup_task.ps1 -Activate -AtLogOnOnly
```

That mode cannot start WSL until the user logs on after a reboot. Prefer the
password-backed task for a truly unattended runner.

Activation runs the read-only RPC watcher preflight and Git write dry-run before
enabling either publisher. The Task Scheduler action keeps one root anchor
attached to WSL, while all network/data/Git work runs as the unprivileged Linux
user. `ProtectHome=read-only` lets SSH read its key but prevents services from
changing it; npm writes to `/var/cache/degen-dogs/npm` instead of the home
directory.

Updating privileged assets is a separate reviewed operation. Supply the new
exact commit and activate in the same run so a healthy old runner is not left
intentionally quiesced:

```powershell
$credential = Get-Credential "$env:USERDOMAIN\$env:USERNAME"
.\scripts\install_wsl_startup_task.ps1 `
  -UpgradeTrustedBundle `
  -TrustedInstallerCommit '<new-reviewed-40-character-commit>' `
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

On upgrades, the bootstrap first removes the persistent/runtime activation
markers, disables the keepalive, disables all timers,
waits for watcher/hourly/health services to stop, verifies every old unit is
inactive, and terminates only the isolated runner distro. This prevents a live
publisher from modifying the clone while Git is fast-forwarded or unit files are
replaced. Any quiesce, sync, preflight, or task-start failure leaves both the
Windows keepalive and Linux timers disabled.

Activation is a two-phase commit. The installer can enable unit files, but each
service has `ConditionPathExists=/run/degen-dogs/activation-enabled`. Only after
Task Scheduler reports the keepalive running and the WSL anchor publishes its
ready signal does PowerShell atomically create the persistent armed marker and
runtime marker, start the timers, and verify them. `/run` is cleared on reboot;
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
- `degen-dogs-hourly.service` / `.timer`: minute-59 archive/reconcile;
- `degen-dogs-health.service` / `.timer`: five-minute health and freshness;
- `degen-dogs-runner.target`: boot grouping for all three timers.

Services use bounded timeouts, control-group termination, retry backoff, a
narrow runtime PATH, empty capability sets, read-only system/home mounts, and
explicit write access only to the clone, log directory, and cache/lock
directory. Event refreshes keep archive work off for latency; the staggered
hourly job maintains the archive.

## Verify operation

From elevated PowerShell:

```powershell
Get-ScheduledTask -TaskName 'Degen Dogs WSL Runner'
Get-ScheduledTaskInfo -TaskName 'Degen Dogs WSL Runner'
wsl.exe -d DegenDogsRunner -u root -- systemctl list-timers 'degen-dogs-*'
```

From WSL:

```bash
systemctl status degen-dogs-watcher.timer degen-dogs-hourly.timer degen-dogs-health.timer
systemctl status degen-dogs-watcher.service degen-dogs-hourly.service
journalctl -u degen-dogs-watcher.service -u degen-dogs-hourly.service --since '1 hour ago'
tail -n 80 /var/log/degen-dogs/watch-onchain.log
tail -n 80 /var/log/degen-dogs/refresh.log
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
refresh failures, the shared lock, tracked Git dirt, local/remote status,
GitHub Pages propagation, log growth, and 5 GiB/5% disk-free thresholds.

The existing off-host GitHub freshness watchdog remains independent of both
machines and continues to open/update incidents when raw `main` or Pages is
stale.

## Maintenance and removal

Stop the Windows keepalive before deliberately disabling timers; the anchor
repairs stopped timers once per minute.

Remove the task and services while preserving the distro, repository, private
configuration, SSH key, logs, and caches:

```powershell
.\scripts\install_wsl_startup_task.ps1 -Uninstall
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
