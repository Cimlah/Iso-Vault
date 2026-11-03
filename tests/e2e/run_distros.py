#!/usr/bin/env python3
"""Boots real distro ISOs through Iso-Vault, in VMs, and checks how far they get.

These download gigabytes and run under emulation, so they're for occasional use rather than every
change. A pass means the OS got far enough to prove its recipe works, such as reaching systemd with
its root filesystem mounted over the network.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_e2e import CONSOLE, FIRMWARES, docker, say  # noqa: E402


# systemd colours its messages, so "Welcome to Arch Linux!" isn't one unbroken string on the wire.
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?=]*[A-Za-z]")


@dataclass(frozen=True)
class Distro:
    name: str
    index_url: str  # A folder listing, or the ISO itself if `pattern` is empty
    firmware: str
    profile: str
    # Text on the serial console that proves the recipe worked.
    success: tuple[str, ...]
    pattern: str = ""
    memory: int = 2048
    extra_args: str = ""
    # Debian's installer downloads its files from the internet.
    needs_internet: bool = False
    env: dict[str, str] = field(default_factory=dict)
    # A word on the console can appear too early, so the server's log has to back up a pass.
    must_fetch: tuple[str, ...] = ()
    must_not_fetch: tuple[str, ...] = ()
    # Reuses another entry's download, for testing the same ISO a different way.
    iso_from: str = ""


DISTROS = (
    Distro(
        name="arch-x64",
        index_url="https://geo.mirror.pkgbuild.com/iso/latest/archlinux-x86_64.iso",
        firmware="uefi-x64",
        profile="arch",
        success=("Welcome to Arch Linux",),
        memory=3072,
        must_fetch=("arch/x86_64/airootfs.sfs",),
    ),
    Distro(
        name="debian-live-x64",
        index_url="https://cdimage.debian.org/debian-cd/current-live/amd64/iso-hybrid/",
        pattern=r"debian-live-[\d.]+-amd64-standard\.iso",
        firmware="uefi-x64",
        profile="debian-live",
        success=("Welcome to Debian",),
        # Its filesystem should come over NFS, so a download would mean the wrong recipe ran.
        must_not_fetch=("filesystem.squashfs",),
    ),
    Distro(
        name="clonezilla-x64",
        index_url="https://downloads.sourceforge.net/project/clonezilla/clonezilla_live_stable/3.3.1-35/clonezilla-live-3.3.1-35-amd64.iso",
        firmware="uefi-x64",
        profile="debian-live",
        success=("automatic login",),
        # Its filesystem should come over NFS, so a download would mean the wrong recipe ran.
        must_not_fetch=("filesystem.squashfs",),
    ),
    Distro(
        name="debian-netinst-x64",
        index_url="https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/",
        pattern=r"debian-[\d.]+-amd64-netinst\.iso",
        firmware="uefi-x64",
        profile="debian-installer",
        # The installer draws with ncurses, but the tab bar of its terminal session is plain text.
        success=("*installer)",),
        needs_internet=True,
        must_fetch=("/netboot/", "initrd.gz"),
    ),
    Distro(
        name="ubuntu-desktop-x64",
        index_url="https://releases.ubuntu.com/noble/",
        pattern=r"ubuntu-24\.04\.\d+-desktop-amd64\.iso",
        firmware="uefi-x64",
        profile="ubuntu-casper",
        success=("Welcome to Ubuntu",),
        memory=4096,
        must_not_fetch=("/raw/",),
    ),
    Distro(
        name="fedora-workstation-x64",
        index_url="https://download.fedoraproject.org/pub/fedora/linux/releases/44/Workstation/x86_64/iso/",
        pattern=r"Fedora-Workstation-Live-[\w.-]+\.iso",
        firmware="uefi-x64",
        profile="fedora-live",
        success=("Welcome to Fedora",),
        memory=5120,
        must_fetch=("LiveOS/squashfs.img",),
    ),
    Distro(
        name="ubuntu-server-arm64",
        index_url="https://cdimage.ubuntu.com/releases/noble/release/",
        pattern=r"ubuntu-24\.04\.\d+-live-server-arm64\.iso",
        firmware="uefi-arm64",
        profile="ubuntu-server",
        success=("Welcome to Ubuntu",),
        memory=3072,
        must_not_fetch=("/raw/",),
    ),
    Distro(
        name="ubuntu-server-ram-arm64",
        index_url="https://cdimage.ubuntu.com/releases/noble/release/",
        pattern=r"ubuntu-24\.04\.\d+-live-server-arm64\.iso",
        iso_from="ubuntu-server-arm64",
        firmware="uefi-arm64",
        profile="ubuntu-server",
        success=("Welcome to Ubuntu",),
        memory=8192,
        env={"ENABLE_NFS": "false"},
        must_fetch=("/raw/",),
    ),
    Distro(
        name="alma-boot-arm64",
        index_url="https://repo.almalinux.org/almalinux/9/isos/aarch64/AlmaLinux-9-latest-aarch64-boot.iso",
        firmware="uefi-arm64",
        profile="anaconda",
        # "anaconda" alone appears in the initramfs. This only exists after install.img has loaded.
        success=("anaconda-tmux", "Starting installer"),
        memory=4096,
        extra_args="inst.text",
        must_fetch=("images/install.img",),
    ),
    Distro(
        name="fedora-everything-x64",
        index_url="https://download.fedoraproject.org/pub/fedora/linux/releases/44/Everything/x86_64/iso/",
        pattern=r"Fedora-Everything-netinst-x86_64-[\d.-]+\.iso",
        firmware="uefi-x64",
        profile="anaconda",
        # "anaconda" alone appears in the initramfs. This only exists after install.img has loaded.
        success=("anaconda-tmux", "Starting installer"),
        memory=4096,
        extra_args="inst.text",
        must_fetch=("images/install.img",),
    ),
    Distro(
        name="opensuse-net-x64",
        index_url="https://download.opensuse.org/tumbleweed/iso/openSUSE-Tumbleweed-NET-x86_64-Current.iso",
        firmware="uefi-x64",
        profile="opensuse-net",
        success=("Starting YaST", "Preparing YaST", "YaST2"),
        memory=4096,
        needs_internet=True,
    ),
    Distro(
        name="opensuse-dvd-x64",
        index_url="https://download.opensuse.org/tumbleweed/iso/openSUSE-Tumbleweed-DVD-x86_64-Current.iso",
        firmware="uefi-x64",
        profile="opensuse",
        success=("Starting YaST", "Preparing YaST", "YaST2"),
        memory=4096,
        must_fetch=("boot/x86_64/root",),
    ),
)


def resolve_url(distro: Distro) -> str:
    if not distro.pattern:
        return distro.index_url
    with urllib.request.urlopen(distro.index_url, timeout=60) as response:
        listing = response.read().decode(errors="replace")
    matches = sorted(set(re.findall(distro.pattern, listing)))
    if not matches:
        raise RuntimeError(f"No ISO matching {distro.pattern} at {distro.index_url}")
    return distro.index_url + matches[-1]


def iso_name(distro: Distro) -> str:
    return distro.iso_from or distro.name


def download(distro: Distro, cache: Path) -> Path:
    """Each ISO gets its own folder, so that each run mounts only the one under test."""
    folder = cache / iso_name(distro)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{iso_name(distro)}.iso"
    if target.exists():
        return folder
    url = resolve_url(distro)
    say(f"{distro.name}: downloading {url}")
    partial = target.with_suffix(".part")
    with urllib.request.urlopen(url, timeout=120) as response, open(partial, "wb") as out:
        shutil.copyfileobj(response, out, 1024 * 1024)
    partial.replace(target)
    return folder


def run(distro: Distro, index: int, image: str, client: str, cache: Path, logs: Path, timeout: int) -> tuple[str, bool, str]:
    prefix = f"iv-distro-{distro.name}"
    server, vm, network = f"{prefix}-server", f"{prefix}-client", prefix
    subnet = f"172.29.{index}"
    try:
        folder = download(distro, cache)
        create = ["network", "create", "--subnet", f"{subnet}.0/24", network]
        if not distro.needs_internet:
            create.insert(2, "--internal")
        docker(*create)
        arguments = f"{CONSOLE[distro.firmware]} {distro.extra_args}".strip()
        docker(
            "run", "-d", "--name", server, "--network", network, "--ip", f"{subnet}.10", "--privileged",
            "-e", "DHCP_MODE=server", "-e", "DHCP_FORCE=true", "-e", "STABLE_SECONDS=0", "-e", "ENABLE_SMB=false",
            "-e", "MENU_TIMEOUT=2", "-e", f"DEFAULT_ISO={iso_name(distro)}.iso", "-e", f"EXTRA_KERNEL_ARGS={arguments}",
            "-e", f"DHCP_GATEWAY={subnet}.1", "-e", "DHCP_DNS=1.1.1.1",
            *[item for key, value in distro.env.items() for item in ("-e", f"{key}={value}")],
            "-v", f"{folder}:/isos:ro", image,
        )  # fmt: skip

        profile = wait_for_profile(server, distro)
        if profile != distro.profile:
            return distro.name, False, f"recognised as {profile}, not {distro.profile}"

        docker(
            "run", "-d", "--name", vm, "--network", network, "--ip", f"{subnet}.50", "--privileged", "-t",
            "-e", f"CLIENT_MEMORY={distro.memory}", client, distro.firmware,
        )  # fmt: skip

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = docker("logs", vm, check=False)
            text = ANSI_ESCAPE.sub("", result.stdout + result.stderr)
            for marker in distro.success:
                if marker in text:
                    problem = check_server_log(server, distro)
                    if problem:
                        return distro.name, False, f'reached "{marker}", but {problem}'
                    return distro.name, True, f'reached "{marker}"'
            if docker("inspect", "-f", "{{.State.Running}}", vm, check=False).stdout.strip() != "true":
                return distro.name, False, "the VM stopped early"
            time.sleep(5)
        return distro.name, False, f"timed out after {timeout}s"
    except Exception as error:  # A problem with one ISO, such as a moved URL, shouldn't stop the rest.
        return distro.name, False, str(error)
    finally:
        for role, container in (("server", server), ("serial", vm)):
            result = docker("logs", container, check=False)
            if result.returncode == 0:
                (logs / f"{distro.name}.{role}.log").write_text(result.stdout + result.stderr, errors="replace")
        docker("rm", "-f", server, vm, check=False)
        docker("network", "rm", network, check=False)


def check_server_log(server: str, distro: Distro) -> str:
    result = docker("logs", server, check=False)
    served = [line for line in (result.stdout + result.stderr).splitlines() if re.search(r'" (200|206) \d+$', line)]
    for path in distro.must_fetch:
        if not any(path in line for line in served):
            return f"the server never served {path}"
    for path in distro.must_not_fetch:
        if any(path in line for line in served):
            return f"the server served {path}, so the wrong recipe ran"
    return ""


def wait_for_profile(server: str, distro: Distro) -> str:
    pattern = re.compile(rf"{re.escape(iso_name(distro))}\.iso: ([\w-]+),")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        result = docker("logs", server, check=False)
        match = pattern.search(result.stdout + result.stderr)
        if match:
            return match.group(1)
        time.sleep(2)
    raise RuntimeError("the ISO never became ready")


def main() -> int:
    names = [distro.name for distro in DISTROS]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="iso-vault:dev")
    parser.add_argument("--only", default="", help=f"Comma-separated, from: {', '.join(names)}")
    parser.add_argument("--cache", type=Path, default=HERE / ".cache" / "distros")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=2400)
    arguments = parser.parse_args()

    chosen = [d for d in DISTROS if not arguments.only or d.name in arguments.only.split(",")]
    client = "iso-vault-e2e-client:dev"
    subprocess.run(
        ["docker", "build", "-q", "-t", client, "-f", str(HERE / "Dockerfile.client"), str(HERE)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    logs = HERE / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    with ThreadPoolExecutor(max_workers=arguments.jobs) as pool:
        futures = [
            pool.submit(run, d, 10 + i, arguments.image, client, arguments.cache.resolve(), logs, arguments.timeout)
            for i, d in enumerate(chosen)
        ]
        results = [future.result() for future in futures]

    say("")
    failures = 0
    for name, passed, detail in results:
        failures += not passed
        say(f"{'PASS' if passed else 'FAIL'}  {name}: {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
