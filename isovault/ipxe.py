"""Generates the iPXE scripts that devices run.

ISO filenames are untrusted, so they only ever reach a script through sanitize_label(). URLs and
kernel arguments are built from generated IDs and server settings, never from filenames.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Sequence

from .profiles import ARM64, X86_64, BootPlan

UNSAFE_LABEL_CHARS = re.compile(r"[^A-Za-z0-9 ._()+,:?/\-]")
PIN_ESCAPE_MS = 5000


def sanitize_label(text: str, limit: int = 66) -> str:
    """Keep menu text readable while removing anything iPXE could interpret, such as ${...}."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    cleaned = UNSAFE_LABEL_CHARS.sub("_", ascii_text.replace("'", "")).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 3].rstrip() + "..."
    return cleaned or "Unnamed"


@dataclass(frozen=True)
class Client:
    mac: str  # normalized, or empty if the device didn't send a valid one
    arch: str  # X86_64 or ARM64
    platform: str  # efi or pcbios


@dataclass(frozen=True)
class MenuItem:
    id: str
    name: str
    group: str
    note: str = ""


def client_arch(build_arch: str) -> str:
    """iPXE's BIOS build is 32-bit, so i386 here doesn't mean the CPU is. Anything x86 is x86_64."""
    return ARM64 if build_arch in ("arm64", "aarch64") else X86_64


def _query(client: Client, **extra: str) -> str:
    params = [f"mac={client.mac}"] if client.mac else []
    params += [f"arch={client.arch}", f"platform={client.platform}"]
    params += [f"{key}={value}" for key, value in extra.items()]
    return "&".join(params)


def _local_boot(client: Client) -> str:
    if client.platform == "pcbios":
        # BIOS firmware often won't move on to the next device after an exit, so boot it directly.
        return "sanboot --no-describe --drive 0x80 || exit 1"
    return "exit 1"


def error_script(message: str) -> str:
    """Shown when a device asks for something we can't boot. Returning fails back to the menu."""
    return "\n".join(
        [
            "#!ipxe",
            f"echo Iso-Vault: {sanitize_label(message, 200)}",
            "prompt --timeout 15000 Press any key to continue... ||",
            "exit 1",
            "",
        ]
    )


def render_menu(
    base_url: str,
    client: Client,
    items: Sequence[MenuItem],
    hidden_count: int = 0,
    timeout_seconds: int = 30,
    default_id: str | None = None,
) -> str:
    query = _query(client)
    lines = ["#!ipxe", "", ":menu"]
    arch_name = "ARM64" if client.arch == ARM64 else "x64"
    firmware = "BIOS" if client.platform == "pcbios" else "UEFI"
    lines.append(f"menu Iso-Vault: choose an ISO ({arch_name} {firmware})")

    labels: dict[str, str] = {}
    current_group = None
    for index, item in enumerate(sorted(items, key=lambda i: (i.group.lower(), i.name.lower()))):
        if item.group != current_group:
            current_group = item.group
            if current_group:
                lines.append(f"item --gap -- {sanitize_label(current_group)}/")
        label = f"iso{index}"
        labels[item.id] = label
        text = sanitize_label(item.name, 50 if item.note else 66)
        if item.note:
            text += f" ({sanitize_label(item.note, 24)})"
        lines.append(f"item {label} {text}")

    if not items:
        lines.append("item --gap -- No ISOs are ready for this device yet.")
    if hidden_count:
        detail = "ISO is hidden because it cannot" if hidden_count == 1 else "ISOs are hidden because they cannot"
        lines.append(f"item --gap -- {hidden_count} {detail} boot here.")

    lines += [
        "item --gap --",
        "item local Boot from local disk",
        "item refresh Refresh this list",
        "item reboot Reboot",
        "item shell iPXE shell",
    ]

    default_label = labels.get(default_id or "", "local")
    timeout = f"--timeout {timeout_seconds * 1000} " if timeout_seconds > 0 else ""
    lines += [
        f"choose {timeout}--default {default_label} selected || goto local",
        "goto ${selected}",
        "",
    ]

    for item_id, label in labels.items():
        lines += [
            f":{label}",
            f"chain {base_url}/ipxe/boot/{item_id}?{query} || goto failed",
            "goto menu",
            "",
        ]

    lines += [
        ":failed",
        "echo Iso-Vault: that ISO did not boot.",
        "prompt --timeout 15000 Press any key to return to the menu... ||",
        "goto menu",
        "",
        ":refresh",
        f"chain --replace {base_url}/ipxe/menu?{_query(client, nopin='1')}",
        "goto menu",
        "",
        ":local",
        _local_boot(client),
        "",
        ":reboot",
        "reboot",
        "",
        ":shell",
        "shell",
        "goto menu",
        "",
    ]
    return "\n".join(lines)


def render_pinned(base_url: str, client: Client, iso_id: str, name: str) -> str:
    """Boots the pinned ISO, with a short window to get to the menu instead."""
    return "\n".join(
        [
            "#!ipxe",
            f"echo Iso-Vault: this device is pinned to {sanitize_label(name)}",
            f"prompt --timeout {PIN_ESCAPE_MS} Press any key for the menu... && goto menu ||",
            f"chain {base_url}/ipxe/boot/{iso_id}?{_query(client, pin='1')} ||",
            "echo Iso-Vault: the pinned ISO did not boot.",
            ":menu",
            f"chain --replace {base_url}/ipxe/menu?{_query(client, nopin='1')}",
            "",
        ]
    )


def _failure_tail() -> list[str]:
    return [
        ":failed",
        "echo Iso-Vault: this ISO could not be loaded.",
        "exit 1",
        "",
    ]


def render_linux(client: Client, label: str, file_base: str, plan: BootPlan) -> str:
    # UEFI kernels find their initrd through iPXE's virtual file. BIOS passes it directly.
    magic = "initrd=initrd.magic " if client.platform == "efi" else ""
    lines = [
        "#!ipxe",
        "imgfree",
        f"echo Iso-Vault: booting {sanitize_label(label)}",
        f"kernel {file_base}/{plan.kernel} {magic}{plan.args} || goto failed",
    ]
    lines += [f"initrd {file_base}/{path} || goto failed" for path in plan.initrds]
    lines += ["boot || goto failed", ""]
    return "\n".join(lines + _failure_tail())


def render_debian_netboot(client: Client, label: str, netboot_url: str, extra_args: str = "") -> str:
    magic = "initrd=initrd.magic " if client.platform == "efi" else ""
    arguments = f"{magic}{extra_args}".strip()
    return "\n".join(
        [
            "#!ipxe",
            "imgfree",
            f"echo Iso-Vault: booting {sanitize_label(label)} using the Debian netboot files",
            f"kernel {netboot_url}/linux {arguments} || goto failed",
            f"initrd {netboot_url}/initrd.gz || goto failed",
            "boot || goto failed",
            "",
        ]
        + _failure_tail()
    )


def render_windows(
    client: Client,
    base_url: str,
    iso_id: str,
    label: str,
    file_base: str,
    bcd: str,
    boot_sdi: str,
    boot_wim: str,
) -> str:
    loader = "wimboot.arm64" if client.arch == ARM64 else "wimboot"
    scripts = f"{base_url}/ipxe/windows/{iso_id}"
    return "\n".join(
        [
            "#!ipxe",
            "imgfree",
            f"echo Iso-Vault: booting {sanitize_label(label)}",
            f"kernel {base_url}/loaders/{loader} || goto failed",
            f"initrd --name winpeshl.ini {scripts}/winpeshl.ini winpeshl.ini || goto failed",
            f"initrd --name startup.bat {scripts}/startup.bat startup.bat || goto failed",
            f"initrd --name BCD {file_base}/{bcd} BCD || goto failed",
            f"initrd --name boot.sdi {file_base}/{boot_sdi} boot.sdi || goto failed",
            f"initrd --name boot.wim {file_base}/{boot_wim} boot.wim || goto failed",
            "boot || goto failed",
            "",
        ]
        + _failure_tail()
    )


def render_sanboot(label: str, iso_url: str) -> str:
    return "\n".join(
        [
            "#!ipxe",
            f"echo Iso-Vault: booting {sanitize_label(label)} as a virtual CD",
            f"sanboot --no-describe {iso_url} || goto failed",
            "",
        ]
        + _failure_tail()
    )
