# Custom boot profiles

A profile tells Iso-Vault how to recognise an OS family, and how to boot it. If your ISO isn't recognised, you can often add support with a small text file, and no code.

Put `.toml` files in `data/profiles.d/`, then restart with `docker compose restart`. A profile with the same `name` as a built-in one replaces it. The built-in profiles are in [`isovault/profiles/`](../isovault/profiles), and make good examples.

## Example

```toml
name = "my-distro"
label = "My Distro"          # shown in the web UI
priority = 60                # higher runs first; built-in profiles use 40 to 70
menu_note = "needs internet" # optional, shown in the boot menu
warning = "Shown in the web UI."  # optional

[detect]
all = ["live/rootfs.img"]    # every one of these must exist
any = ["live/vmlinuz*"]      # at least one of these must exist
none = ["casper/"]           # none of these may exist

[detect.contains]
".disk/info" = "My Distro"   # this file must contain this text

[boot]
method = "linux"
kernel = ["live/vmlinuz", "live/vmlinuz*"]   # the first match is used
initrd = ["live/initrd.img", "live/initrd*"]
extra_initrd = ["live/ucode.img"]            # optional; all matches load before the initrd

# Variants are tried in order. The first whose requirements are met is used.
[[boot.variant]]
requires = ["nfs"]
args = "boot=live nfsroot={server}:{nfs_path} ip=dhcp"

[[boot.variant]]
args = "boot=live fetch={http}/live/rootfs.img ip=dhcp"
warning = "This loads into RAM. Allow {ram_gib} GiB."
```

## Paths

Paths are relative to the root of the ISO. They ignore case, because ISOs vary, and they can use `*` and `?`. A path ending in `/` only matches a folder.

## Boot methods

| Method | What it does |
|---|---|
| `linux` | Boots a kernel and initrd from the ISO |
| `windows` | Uses wimboot and the Samba share, for Windows ISOs |
| `debian-netboot` | Fetches Debian's netboot files, for Debian installer ISOs |
| `sanboot` | Boots the ISO as a virtual CD |

## Variables for kernel arguments

| Variable | Value | Example |
|---|---|---|
| `{server}` | The server's IP address | `192.168.1.10` |
| `{http}` | URL of the ISO's files, with no trailing slash | `http://192.168.1.10:8080/files/abc` |
| `{iso_url}` | URL of the ISO file itself | `http://192.168.1.10:8080/raw/abc.iso` |
| `{nfs_path}` | NFS path of the ISO's files | `/run/isovault/mnt/abc` |
| `{karch}` | The device's kernel architecture | `x86_64` or `aarch64` |
| `{debarch}` | The device's Debian architecture | `amd64` or `arm64` |
| `{mac_hyphen}` | The device's MAC address | `52-54-00-aa-bb-cc` |
| `{iso_gib}` | The ISO's size in GiB | `3.2` |
| `{ram_gib}` | RAM to load the ISO into memory, as a tmpfs is limited to half of RAM | `7` |
| `{glob:PATH}` | The first file matching a pattern | `{glob:boot/modloop-*}` becomes `boot/modloop-virt` |

`{karch}` and `{debarch}` also work in `kernel` and `initrd` paths.

## Requirements

A variant or a `[boot]` section can list `requires`:

- `nfs`: the NFS share is running.
- `smb`: the Samba share is running.

## Checking your profile

`docker logs iso-vault` lists any profile it couldn't load, and shows which profile each ISO matched. The web UI's **OS family** column shows the same. A broken profile is skipped and doesn't stop anything else.

If you get a popular distro working, a pull request adding its profile would be welcome.
