# Iso-Vault

Drop ISO files in a folder, and boot any device on your network from them. Like Ventoy, but over the network instead of from a USB stick.

- **One command to install**, using Docker.
- **Runs on** x64 and ARM64 Linux, including a Raspberry Pi.
- **Boots** x64 BIOS, x64 UEFI and ARM64 UEFI devices.
- **Fits your network:** runs alongside your router's DHCP with no changes, hands out addresses itself, or lets your router point at it.
- **Web UI** to see your ISOs and devices, and pin an ISO to a device so it boots unattended.

## Quick start

On a Linux machine connected to the same network as the devices you want to boot:

```bash
curl -fsSL https://raw.githubusercontent.com/Cimlah/Iso-Vault/main/install.sh | bash
```

Or from a clone of this repository:

```bash
./install.sh
```

The installer downloads a ready-made image for x64 or ARM64, so a Raspberry Pi doesn't have to compile anything. From a clone, it builds the image itself if the download fails, which can take well over an hour on a Raspberry Pi 4.

Then:

1. Copy an ISO into the `isos` folder the installer printed. Subfolders become groups in the boot menu.
2. On the device, turn off Secure Boot, and choose network boot (often F12 at startup).
3. Pick the ISO from the menu.

The installer also prints the web UI's address and password.

> Docker Desktop on macOS and Windows won't work, because its containers can't see your LAN's broadcasts. Use a Linux machine, or a Linux VM with bridged networking.

## What boots

Network booting can't work quite like Ventoy. An ISO can be presented as a virtual CD at first, but that disappears once the kernel starts, so installers can't find their files. Iso-Vault identifies each ISO's OS family, and boots it the way that family supports.

| Family | Examples | How it runs | Needs |
|---|---|---|---|
| Ubuntu live | Ubuntu Desktop, Mint, Pop!_OS, Zorin | Over NFS | |
| Ubuntu Server | Ubuntu Server | Over NFS | |
| Debian live | Debian Live, Kali, Clonezilla, GParted Live | Over NFS | |
| Debian installer | Debian netinst, DVD | Debian's netboot files | Internet access |
| Fedora / RHEL installers | Fedora Server, RHEL, Alma, Rocky | Over HTTP | |
| Fedora live | Fedora Workstation and spins | Live image in RAM | RAM for the image |
| Arch | Arch Linux, EndeavourOS | Over HTTP | |
| openSUSE | openSUSE DVD installers | Over HTTP | |
| openSUSE network | openSUSE NET installers | Downloads the installer | Internet access |
| Alpine | Alpine Linux | Over HTTP | |
| Windows | Windows 10 and 11, Server | wimboot, then an SMB share | Port 445 free on the server |
| Anything else | Small tools such as memtest | Virtual CD | May not finish booting |

[docs/compatibility.md](docs/compatibility.md) lists what has actually been tested. You can add your own OS families with [custom profiles](docs/custom-profiles.md).

## How it fits your network

The installer checks your network and picks a mode, which you can override with `--dhcp-mode`:

| Mode | What happens | Router changes |
|---|---|---|
| `proxy` | Adds boot info alongside your existing DHCP server, and hands out no addresses | None |
| `server` | Iso-Vault hands out IP addresses itself | Turn off the router's DHCP |
| `upstream` | No DHCP at all. Your router points devices at this server | Set next-server and boot files, see [docs/upstream-dhcp.md](docs/upstream-dhcp.md) |

`server` mode won't hand out addresses if it finds another DHCP server, because two can disrupt a network. It falls back to `proxy` and says why in the web UI. Set `DHCP_FORCE=true` to override this.

## Pinning an ISO to a device

Devices appear in the web UI after they boot once, or you can add one by MAC address. Choose an ISO and:

- **Next boot only:** boots it once, then goes back to the menu. Use this for installers, so a machine that always boots from the network doesn't reinstall itself forever.
- **Every boot:** always boots it.

Pinned devices show a 5-second prompt, so you can still get to the menu.

## Windows network drivers

Windows Setup downloads its files over the network, so its installer needs a driver for the device's network card. Stock Windows ISOs often don't have one for newer cards.

If yours doesn't, unpack the card's Windows driver into its own folder under `drivers/x64` (or `drivers/arm64`), and boot again. There's no need to restart anything. The folder needs the driver's unpacked files, such as `.inf`, `.sys` and `.cat`, because an `.exe` or `.zip` is ignored. [docs/troubleshooting.md](docs/troubleshooting.md#windows) has the details.

## Using a reverse proxy

If Caddy, nginx, Traefik, Apache or HAProxy already runs on the server, the installer offers to put the web UI behind it. Caddy on the host can be set up for you, and the others get a ready-to-use config file. Iso-Vault never uses ports 80 or 443, so it won't collide with your proxy.

Only the web UI goes through the proxy. Devices always boot directly from the server's LAN address. See [docs/reverse-proxy.md](docs/reverse-proxy.md).

## Limits

- **Not every ISO will boot.** Unrecognised ISOs are booted as a virtual CD, which often stops once the kernel starts.
- **Secure Boot must be off**, because this build of iPXE isn't signed.
- **The server must be on the same network segment as the devices**, unless your network relays DHCP.
- **A reverse proxy only covers the web UI.** Devices must be able to reach the server's LAN address on the boot port.
- **The web UI needs its own hostname behind a proxy.** Sub-paths such as `/isovault` won't work.
- **ARM64 devices need UEFI PXE.** A Raspberry Pi needs UEFI firmware, and U-Boot boards are best effort.
- **Windows needs a network driver in its installer.** Stock Windows ISOs often don't include one for newer network cards, so you may need to [add it](#windows-network-drivers).
- **A full Windows install hasn't been tested.** Windows Setup has started on one physical PC, but no install has been run to the end.
- **Windows on ARM is experimental.** It hasn't been tested, and [reports for wimboot on ARM are mixed](https://github.com/ipxe/wimboot/issues/4).
- **Trusted networks only.** The boot port and direct UI port use plain HTTP, and the boot port has no login. Devices on your network can read the ISOs and the read-only Samba password, because they need them to boot. Never expose these ports to the internet.

## Settings

The installer writes `.env`. [`.env.example`](.env.example) explains every setting. After changing one, run `docker compose up -d`.

## Updating

From the folder the installer set up, download the new image:

```bash
docker compose pull
```

Then restart with it:

```bash
docker compose up -d
```

Your ISOs, drivers and settings live outside the image, so they're kept. The `latest` tag follows the `main` branch. To stay on one build, set `ISO_VAULT_TAG` in `.env`.

## Monitoring

For a monitor such as Uptime Kuma, check `http://<server-ip>:8080/healthz`, using your `BOOT_PORT` if you changed it. It returns HTTP 200 when nginx and Iso-Vault's core are responding. It doesn't check DHCP, TFTP or your ISOs.

## Troubleshooting

See [docs/troubleshooting.md](docs/troubleshooting.md). The first place to look is usually:

```bash
docker logs iso-vault
```

## Development

| What | Command |
|---|---|
| Python tests | `pytest tests/unit tests/installer` |
| Shell checks | `shellcheck install.sh tests/e2e/client.sh` |
| Web UI type checks and tests | `cd web && npm ci && npm run typecheck && npm test` |
| Container, reverse proxy and browser tests | `docker build -t iso-vault:dev . && pytest tests/integration` |
| Boot real VMs in every DHCP mode | `python3 tests/e2e/fetch_alpine.py && python3 tests/e2e/run_e2e.py` |
| Boot real distro ISOs | `python3 tests/e2e/run_distros.py --help` |

[CI](.github/workflows/ci.yml) runs all of these on pull requests except the distro ISOs, with the container tests and VM boots running natively on both x64 and ARM64. The distro ISOs are [set to run weekly](.github/workflows/distros.yml). Every push to `main` [publishes a new `latest` image](.github/workflows/release.yml).

## License

[MIT](LICENSE). The image includes other projects' software, see [THIRD_PARTY.md](THIRD_PARTY.md).
