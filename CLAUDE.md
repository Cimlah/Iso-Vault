# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

The core uses only the Python standard library, so the Python tests just need `pytest`. There's no Python project file or linter config.

```bash
pip install pytest
python -m pytest tests/unit tests/installer
python -m pytest tests/unit/test_drivers.py -k name_clash   # one test
shellcheck install.sh tests/e2e/client.sh
```

Web UI, from `web/`:

```bash
npm ci
npm run typecheck
npm test
npx vitest run tests/routes.test.ts -t "accepts a token"    # one test
```

These need Docker, and use the image `iso-vault:dev`. Override it with `ISO_VAULT_IMAGE` for the integration tests, or `--image` for the VM boots.

```bash
docker build -t iso-vault:dev .
python -m pytest tests/integration -s
python -m pytest tests/integration/test_container.py -k samba_share   # one test
python3 tests/e2e/fetch_alpine.py                                     # once, before run_e2e
python3 tests/e2e/run_e2e.py                                          # QEMU VMs, 12 combinations
python3 tests/e2e/run_e2e.py --modes proxy --firmwares uefi-arm64 --no-pinned
python3 tests/e2e/run_distros.py --only arch-x64                      # real ISOs, about 20 GB for all of them
```

The browser specs in `tests/ui/` run in a Playwright container, started by `tests/integration/test_ui.py`.

The integration tests also work from inside a container, which helps on a machine with no local Python:

```bash
docker run --rm -v "$PWD":/repo:ro -w /repo -v /var/run/docker.sock:/var/run/docker.sock -e PYTHONDONTWRITEBYTECODE=1 python:3.14-alpine sh -c 'apk add -q docker-cli bash; pip install -q pytest; python -m pytest tests/integration -q -p no:cacheprovider'
```

## Architecture

Everything runs in one privileged container on host networking, because DHCP needs LAN broadcasts and ISOs need loop mounts. The tests replace the LAN with private Docker networks.

**Processes.** `python -m isovault run` (`isovault/__main__.py`) is the main process. It reads settings once from the environment, settles the DHCP mode, and renders `templates/` into `/run/isovault`. `services.Supervisor` then runs dnsmasq, nginx, rpcbind and unfsd, smbd, and the Next.js standalone server. The main loop rescans the ISO folder.

**What the network can reach.** The core (`api.py`) serves HTTP only on the Unix socket `/run/isovault/core.sock`.

- On `BOOT_PORT`, nginx forwards only `/ipxe/` and `/healthz` to the core, and serves `/files/`, `/raw/`, `/loaders/` and `/netboot/` itself. This port has no login.
- The core's `/api/` routes stay on the socket. The web UI on `UI_PORT` calls them through `web/lib/core.ts`.
- `web/proxy.ts` is the login check (Next.js 16's name for middleware), and route handlers check the session again.

**Boot chain.** Firmware gets a loader from dnsmasq, chosen by client architecture (`templates/dnsmasq/`, with one file per DHCP mode). It fetches an iPXE loader over TFTP, which runs `ipxe/embed.ipxe`, then `templates/boot.ipxe`, then `/ipxe/menu`. `ipxe.py` generates the menu or boot script, using pins from `state.py`.

**ISO pipeline.** `scanner.py` waits for a file to stop changing. `mounts.py` then loop-mounts it read-only, or extracts it to `/data/cache` (`ISO_ACCESS`). `profiles.py` identifies it with the first matching TOML profile in `isovault/profiles/` or `/data/profiles.d`, and uses the first `[[boot.variant]]` whose `requires` are met. iPXE's virtual CD disappears once the kernel starts, which is why these profiles exist. A new distro usually needs a profile, not Python.

**Windows.** `ipxe.render_windows` boots wimboot, which injects `templates/windows/`, the driver files and the ISO's boot files into Windows PE. `startup.bat` loads the drivers, connects to the Samba share and runs `Z:\setup.exe`. wimboot allows 63 files and 31-character names, and puts files flat in `X:\Windows\System32`, so `drivers.py` includes each driver whole or skips it with a warning.

**Image build.** The iPXE, wimboot and web stages of the `Dockerfile` run on `$BUILDPLATFORM`, because their output is the same for both architectures. Only the final stage is built per architecture.

## Easy to get wrong

- Templates use `@@NAME@@` placeholders (`services.render`), because nginx and iPXE use `$`.
- Generated iPXE scripts can't contain quote characters, so their messages can't use apostrophes. `test_no_script_contains_a_quote_character` checks this.
- `startup.bat` is served with CRLF line endings, whatever the template has.
- Files on the Samba share have no execute bit (`mode=0444` on mounts, `0644` when extracted), so `templates/smb.conf` needs `acl allow execute always` for Windows to run Setup. `smbclient` can't ask for execute rights, so test this with `Stack.smb_open` in `tests/integration/harness.py`.
- `run()` removes secrets from the environment before starting services, and only the web UI gets what it needs back. `test_services_dont_inherit_secrets` checks this.
- In integration tests, list a volume fixture before `stack` in the test's parameters, so the volume outlives the container.
- The test VMs use a network card that Windows PE has no driver for, so Windows Setup can only be tested on physical hardware.
- Clonezilla patches `live-boot` so that any `ip=` means a static address and DHCP never runs, so the `debian-live` profile leaves it out. It looks like a network fault: `NFS over TCP not available` and `Network is unreachable`, with a working card and DHCP server.
- GParted Live takes its auto-login name only from `username=`, so the `debian-live` profile sets it, even though that's live-config's default. Without it, the console stops at `debian login: --noclear (automatic login)` and the desktop never starts.

## Docs

`docs/compatibility.md` is the record of what has been tested. Only list something there after it has actually passed, and say how far it got. If a test on real hardware changes what's proven, update it.

## CI and releases

- `ci.yml` runs on pull requests and on `main`. The container tests and VM boots run natively on `ubuntu-24.04` and `ubuntu-24.04-arm`.
- `release.yml` publishes a multi-architecture image to `ghcr.io/cimlah/iso-vault` on `v*` tags and on every push to `main`, including merged Dependabot PRs. `latest` follows `main`, and the tests don't run against this build.
- `distros.yml` runs the real ISOs weekly, or on demand.

## Trying a local build on a Raspberry Pi

Compiling on a Pi 4 takes well over an hour, so build on a faster machine and send the image over:

```bash
docker build --platform linux/arm64 -t ghcr.io/cimlah/iso-vault:latest .
docker save ghcr.io/cimlah/iso-vault:latest | gzip | ssh <user>@<pi> 'gunzip | docker load'
```

Then run `docker compose up -d --no-build` on the Pi. Don't run `./install.sh` or `docker compose pull` there, because they'd replace it with the published image.
