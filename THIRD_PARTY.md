# Third-party software

Iso-Vault's own code is under the [MIT license](LICENSE). The container image also includes these projects, under their own licenses.

## Boot loaders

These are shipped as binaries, and run on the devices that boot from Iso-Vault. Their source code is available at the versions below, as the GPL requires.

| Project | Version | License | Source |
|---|---|---|---|
| [iPXE](https://ipxe.org) | commit `12798ec29aa8a64d8675c4378b99f5fe28447afb` (v2.0.0) | GPL-2.0-or-later, with a UEFI exception | [github.com/ipxe/ipxe](https://github.com/ipxe/ipxe/tree/12798ec29aa8a64d8675c4378b99f5fe28447afb) |
| [wimboot](https://ipxe.org/wimboot) | v2.9.0 | GPL-2.0-or-later | [github.com/ipxe/wimboot](https://github.com/ipxe/wimboot/tree/v2.9.0) |

The iPXE binaries are built from that commit, with the settings in [`ipxe/config/general.h`](ipxe/config/general.h) and the script in [`ipxe/embed.ipxe`](ipxe/embed.ipxe). The [`Dockerfile`](Dockerfile) has the exact build commands.

## Runtime

These come from Alpine Linux's packages, and Alpine's package index lists their licenses and sources: dnsmasq, nginx, Samba, unfs3, rpcbind, Python, Node.js, 7-Zip, libarchive, iproute2 and util-linux.

The web UI uses [Next.js](https://nextjs.org) and [React](https://react.dev), both under the MIT license.
