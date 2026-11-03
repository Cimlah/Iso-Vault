# Compatibility

An OS is only listed as tested here after it has actually passed. Most of these results come from `tests/e2e/`, which boots QEMU virtual machines from Iso-Vault over a private network. A pass needs the expected text on the VM's console and, where it applies, proof in the server's log that the OS's files were really served.

Very little has been run on physical hardware yet, so see [below](#physical-hardware) for what has. The [checklist](troubleshooting.md#checking-a-new-install-on-real-hardware) covers checking your own setup.

Last run: November 2026.

## Boot chain

Every combination reached Alpine Linux's login screen, with all files coming from Iso-Vault.

| Client | `server` | `proxy` | `upstream` | Pinned ISO |
|---|---|---|---|---|
| x64 BIOS (SeaBIOS) | Pass | Pass | Pass | Pass |
| x64 UEFI (OVMF) | Pass | Pass | Pass | Pass |
| ARM64 UEFI (AAVMF) | Pass | Pass | Pass | Pass |

All of these passed in CI on both an x64 and an ARM64 runner, with the server image built natively on each. On each runner, clients of the other architecture ran under emulation. The container tests in `tests/integration/` passed on both runners too.

The image published to `ghcr.io` is a separate build, with ARM64 built partly under emulation, and these tests don't run against it.

The BIOS client uses QEMU's iPXE ROM, not a vendor PXE ROM such as Intel's. In `proxy` mode it therefore takes the path for iPXE clients, so the standard path for a BIOS PXE ROM in `proxy` mode hasn't been tested.

## Operating systems

"Reached" is how far the test goes, which isn't a complete install. These were run by hand on an ARM64 host, and aren't part of the CI run on each pull request. The weekly `distros.yml` workflow is set up to repeat them, but it hasn't run yet.

| OS | Client | Recipe | Reached | Evidence |
|---|---|---|---|---|
| Alpine 3.24.3 virt | All three | `alpine`, over HTTP | Login screen | Fetched `modloop-virt` |
| Ubuntu Desktop 24.04.4 | x64 UEFI | `ubuntu-casper`, over NFS | systemd started | ISO not downloaded |
| Ubuntu Server 24.04.5 | ARM64 UEFI | `ubuntu-server`, over NFS | systemd started | ISO not downloaded |
| Ubuntu Server 24.04.5 | ARM64 UEFI | `ubuntu-server`, in RAM | systemd started | Whole ISO downloaded, with 8 GB of RAM |
| Debian Live 13.7.0 standard | x64 UEFI | `debian-live`, over NFS | systemd started | squashfs not downloaded |
| Clonezilla 3.3.1-35 | x64 UEFI | `debian-live`, over NFS | Automatic login | squashfs not downloaded |
| Debian netinst 13.7.0 | x64 UEFI | `debian-installer` | Installer started | Served Debian's netboot files |
| Fedora Workstation 44 | x64 UEFI | `fedora-live` | systemd started | Fetched `squashfs.img` |
| Fedora Everything 44 netinst | x64 UEFI | `anaconda` | Installer environment started | Fetched `install.img` |
| AlmaLinux 9 boot | ARM64 UEFI | `anaconda` | Installer environment started | Fetched `install.img` |
| Arch Linux (November 2026) | x64 UEFI | `arch` | systemd started | Fetched `airootfs.sfs` |
| openSUSE Tumbleweed DVD | x64 UEFI | `opensuse` | YaST starting | Fetched `boot/x86_64/root` |
| openSUSE Tumbleweed NET | x64 UEFI | `opensuse-net` | YaST starting | Downloaded its installer from the internet |

Debian Live's result is from before `ip=dhcp` was removed from the `debian-live` recipe and `username=user` was added, so it needs running again. The Clonezilla test only watches the serial console, so it can't see whether Clonezilla's own menu starts.

The generic virtual CD recipe was tested by forcing it onto Alpine's ISO. On x64 BIOS it reached ISOLINUX, and on x64 UEFI it reached GRUB. On ARM64 UEFI it continued to the kernel, then stopped with `Mounting boot media failed`, which is the [known limit](../README.md#what-boots) of this recipe.

## Physical hardware

One physical x64 PC, booting from a Raspberry Pi 4 on a real LAN:

| OS | Result |
|---|---|
| Ubuntu | Booted |
| Clonezilla | Booted |
| GParted Live | Desktop started |
| Windows 10 22H2 | Windows PE started, loaded a real Realtek driver (`rt640x64`) from the drivers folder, connected to the Samba share and started Windows Setup |

Clonezilla and GParted Live both failed with `NFS over TCP not available` while the `debian-live` recipe set `ip=dhcp`, and both got past that once it was removed. GParted Live then stopped at `debian login: --noclear (automatic login)`, and its desktop started once `username=user` was added. These tests used a custom profile on the Pi, because its image didn't have the fixes yet. Clonezilla's result on this PC is from before `username=user`.

The Windows test stopped once Setup had started, because going further would have erased that PC's disk. So copying Windows from the share to a disk, and booting the result, haven't been tested.

## Not tested yet

These are expected to work, but nothing has proved it.

| What | Why it matters |
|---|---|
| **A full Windows install, on any architecture** | Setup has started on one physical PC, but no install has been run to the end. The VMs can't help, because they use a network card that Windows PE has no driver for |
| **Windows on ARM** | It has never been booted from Iso-Vault, and [reports for wimboot on ARM are mixed](https://github.com/ipxe/wimboot/issues/4) |
| Other physical hardware | Real network cards and firmware vary far more than VMs |
| Real routers in `upstream` mode | The tests use dnsmasq as the router. The [router examples](upstream-dhcp.md) other than dnsmasq haven't been run |
| Routers that only set option 66 | The first stage looks for it, but the tests always set next-server |
| UEFI HTTP Boot | The DHCP replies are configured, but no client has booted this way |
| Raspberry Pi and U-Boot boards | They need UEFI PXE, and none has been tried |
| Mint, Pop!_OS, Zorin, Kali, EndeavourOS, RHEL, Rocky | They should match the recipes above, but each distro can differ. The [custom profiles](custom-profiles.md) guide covers what to do if one doesn't |

## Running these yourself

```bash
python3 tests/e2e/fetch_alpine.py
python3 tests/e2e/run_e2e.py
python3 tests/e2e/run_distros.py
```

`run_distros.py` downloads about 20 GB. Use `--only` to choose specific entries, for example `--only arch-x64,debian-live-x64`.
