# Pointing your router at Iso-Vault

In `upstream` mode, Iso-Vault runs no DHCP at all. Your DHCP server tells devices where to boot from.

```bash
./install.sh --dhcp-mode upstream
```

You need to set two things in your DHCP server: the **next server**, which is the Iso-Vault host's IP address, and a **boot file** for each kind of device.

| Device | DHCP option 93 (client architecture) | Boot file |
|---|---|---|
| BIOS | 0 | `undionly.kpxe` |
| x64 UEFI | 7 and 9 | `snponly-x64.efi` |
| ARM64 UEFI | 11 | `snponly-arm64.efi` |

If a device's firmware has trouble with its network card under `snponly`, `ipxe-x64.efi` and `ipxe-arm64.efi` are also available. These use iPXE's own network drivers.

The examples below use `192.168.1.10` for the Iso-Vault host.

## If your router only takes one boot file

Many home routers have a single boot filename field, which can't serve different kinds of device. You have two options:

- **Use `proxy` mode instead.** It needs no router changes at all, and it handles every kind of device: `./install.sh --dhcp-mode proxy`
- **Enter the file for the only kind of device you boot**, usually `snponly-x64.efi`.

## dnsmasq and OpenWrt

For dnsmasq, add this to its configuration:

```
dhcp-match=set:bios,option:client-arch,0
dhcp-match=set:efi-x64,option:client-arch,7
dhcp-match=set:efi-x64,option:client-arch,9
dhcp-match=set:efi-arm64,option:client-arch,11
dhcp-boot=tag:bios,undionly.kpxe,,192.168.1.10
dhcp-boot=tag:efi-x64,snponly-x64.efi,,192.168.1.10
dhcp-boot=tag:efi-arm64,snponly-arm64.efi,,192.168.1.10
```

On OpenWrt, add the same lines to `/etc/dnsmasq.conf`, then run `/etc/init.d/dnsmasq restart`.

## ISC DHCP

```
option arch code 93 = unsigned integer 16;

subnet 192.168.1.0 netmask 255.255.255.0 {
  # ...your existing settings...
  next-server 192.168.1.10;
  if option arch = 00:07 or option arch = 00:09 {
    filename "snponly-x64.efi";
  } elsif option arch = 00:0b {
    filename "snponly-arm64.efi";
  } else {
    filename "undionly.kpxe";
  }
}
```

## Kea

```json
"client-classes": [
  {
    "name": "bios",
    "test": "option[93].hex == 0x0000",
    "next-server": "192.168.1.10",
    "boot-file-name": "undionly.kpxe"
  },
  {
    "name": "efi-x64",
    "test": "option[93].hex == 0x0007 or option[93].hex == 0x0009",
    "next-server": "192.168.1.10",
    "boot-file-name": "snponly-x64.efi"
  },
  {
    "name": "efi-arm64",
    "test": "option[93].hex == 0x000b",
    "next-server": "192.168.1.10",
    "boot-file-name": "snponly-arm64.efi"
  }
]
```

## pfSense and OPNsense

Under **Services > DHCP Server** (**ISC DHCPv4** on OPNsense), open your LAN, then enable **Network Booting**:

- **Next Server:** `192.168.1.10`
- **Default BIOS file name:** `undionly.kpxe`
- **UEFI 64 bit file name:** `snponly-x64.efi`
- **ARM 64 bit file name:** `snponly-arm64.efi`, if your version offers this field

## MikroTik RouterOS

```
/ip dhcp-server network set [find] next-server=192.168.1.10 boot-file-name=snponly-x64.efi
```

This sets a single boot file, so see [above](#if-your-router-only-takes-one-boot-file) if you have mixed devices.

## UniFi

Under **Settings > Networks**, open your network, then **DHCP Service Management**. Set **Network Boot** to your Iso-Vault host's IP address, and the filename to `snponly-x64.efi`. UniFi takes a single filename, so see [above](#if-your-router-only-takes-one-boot-file) if you have mixed devices.

## Checking it worked

Boot a device from the network. If it doesn't reach the menu, `docker logs iso-vault` should show the TFTP download. If nothing appears, the router isn't sending devices to this server. See [troubleshooting](troubleshooting.md).
