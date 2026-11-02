# Troubleshooting

Start with the log, which shows DHCP requests, downloads and any errors:

```bash
docker logs -f iso-vault
```

## A device doesn't reach the menu

Watch the log while the device tries to boot.

| What you see in the log | What it means | What to do |
|---|---|---|
| Nothing at all | The device's request isn't reaching the server | Check the device is set to network boot, and the server is on the same network segment. Check the server's firewall |
| DHCP lines, but no `sent ... snponly` or `undionly` | DHCP works, but the TFTP download doesn't | Open UDP port 69 in the firewall |
| A file was sent, but no `/ipxe/menu` | iPXE started, but can't reach the boot port | Open the boot port (default TCP 8080) in the firewall |

If `ufw` or `firewalld` is active, the installer prints the rules you need. Run it again to see them.

**The device says "Access Denied" or "Security Violation".** Turn off Secure Boot in its firmware settings.

**Other PXE servers on the network.** If something else is answering, such as WDS or another PXE server, the device may use that instead.

## The menu appears, but an ISO isn't listed

Check the web UI:

- **Waiting for copy to finish:** the file is still changing. It's picked up about 10 seconds after it stops.
- **Can't read:** the file isn't a valid ISO, or it's damaged. Check its checksum.
- **Can't boot:** the reason is shown under its name, usually that a share isn't running.

An ISO also won't be listed if it's for a different architecture. The menu says when ISOs are hidden for this reason.

## An ISO starts booting, then fails

- **A Linux ISO stops at a prompt saying it can't find its files:** this can happen if its OS family wasn't recognised, or a distro changed its layout. Check the **OS family** column in the web UI. If it says **Generic**, try a [custom profile](custom-profiles.md).
- **The device runs out of memory, or says `No space left on device`:** some ISOs load entirely into RAM, and this needs about twice the ISO's size, because the space they load into is limited to half of RAM. The web UI warns you about these. Give the device more RAM, or make sure NFS is running.
- **It hangs partway through a network mount:** check that TCP and UDP ports 111 and 2049 are open.

### Windows

**`No network adapter is listed above`:** Windows PE has no driver for this network card, which is common on newer hardware. To add one:

1. **Find out which card it is.** At the command prompt on the device, this lists the devices with no driver. Look for a line starting with `PCI\VEN_`, where `VEN_8086` is Intel and `VEN_10EC` is Realtek:

   ```
   wmic path Win32_PnPEntity where "ConfigManagerErrorCode<>0" get Name,DeviceID
   ```

2. **Get the driver.** Download the card's Windows driver, usually from your motherboard's support page, and unpack it until you can see its `.inf`, `.sys` and `.cat` files. An installer `.exe` won't work directly, but 7-Zip can often open one.
3. **Copy it to the server.** Put that folder under `drivers/x64`, or `drivers/arm64` for ARM64 devices, for example `drivers/x64/realtek/`.
4. **Boot the device again.** You don't need to restart Iso-Vault. The web UI shows `Loads network drivers:` under the Windows ISO.

A driver is only used if all of these are true, and the web UI says why if one is skipped:

- Its files sit directly beside the `.inf`, because subfolders of a driver aren't included. If a download has folders for several Windows versions, copy only the one matching your ISO.
- Its file names use only letters, digits, dots, dashes and underscores, and are up to 31 characters long.
- No other driver has a file with the same name.
- The total across all drivers stays within 40 files, so keep only the ones you need.

The installed Windows may need the same driver after Setup finishes, so keep a copy to hand.

If you need to get past this straight away, you can also load a driver from a USB stick at the command prompt, then try again:

```
drvload E:\path\to\driver.inf
startup.bat
```

**`Could not connect` with an adapter listed:** the error shown above the network settings says why.

| Error | What it means | What to do |
|---|---|---|
| 53 | Can't reach the share | Check the device has an IP address, and that TCP port 445 is open on the server |
| 67 | Share not found | Check `docker logs iso-vault` for Samba errors |
| 86 or 1326 | Login rejected | This is likely a bug, so please report it |

**The Windows entry is missing or says it can't boot:** port 445 is in use on the server, usually by Samba on a NAS. See below.

## A port is already in use

Iso-Vault uses host networking, so it shares ports with everything else on the server. This is most common on a NAS.

| Port | Used for | If it's taken |
|---|---|---|
| UDP 67, 4011 | DHCP | Iso-Vault won't start. Use `upstream` mode, or stop the other DHCP server |
| UDP 69 | TFTP | Iso-Vault won't start |
| TCP 8080 | Boot files | Iso-Vault won't start. Change `BOOT_PORT` |
| TCP 8081 | Web UI | The UI is off, but booting still works. Change `UI_PORT` |
| TCP/UDP 111, 2049 | NFS | NFS is off, so live systems load into RAM instead |
| TCP 445 | Samba | Samba is off, so Windows ISOs can't boot |

## Server mode says another DHCP server is active

This is a safety check, because two DHCP servers can disrupt a network. Iso-Vault stays in `proxy` mode, which still lets devices boot. To override it, set `DHCP_FORCE=true` in `.env`, then run `docker compose up -d`.

## I forgot the web UI password

```bash
grep ADMIN_PASSWORD .env
```

If it's not there, the password was generated at first start:

```bash
sudo cat data/secrets.json
```

To change it, set `ADMIN_PASSWORD` in `.env`, then run `docker compose up -d`.

## Running without privileged mode

`privileged: true` is there so ISOs can be loop-mounted, which is instant and uses no extra disk. Without it, Iso-Vault extracts each ISO into the data folder instead, which takes time and disk space, but works in places that forbid mounts, such as LXC.

In `compose.yml`, replace `privileged: true` with:

```yaml
    cap_add:
      - NET_ADMIN
      - NET_RAW
      - NET_BIND_SERVICE
```

## Checking a new install on real hardware

The automated tests boot VMs on a private network, so they can't exercise your real network. After installing, it's worth checking these once:

1. [ ] **Proxy mode on your network.** With your router's DHCP left as it is, boot a physical UEFI device from the network. Check it gets an address from the router, then reaches the Iso-Vault menu.
2. [ ] **Boot an ISO.** Copy a small ISO, such as Alpine's, into the `isos` folder, then boot it on that device.
3. [ ] **The DHCP safety check.** With your router's DHCP on, set `DHCP_MODE=server` and run `docker compose up -d`. The web UI should warn that another DHCP server is active, and other devices on the network should keep working. Set it back to `proxy` afterwards.
4. [ ] **Your reverse proxy,** if you use one. Log in through its hostname, pin an ISO to a device, and check the device boots it.
5. [ ] **Windows,** if you use it. Copy a Windows ISO into the `isos` folder, boot it on a physical device, and check that Windows Setup starts. If it stops at `Could not connect`, [see above](#windows). A full install has never been tested, so finish one if you have a disk you can erase.
