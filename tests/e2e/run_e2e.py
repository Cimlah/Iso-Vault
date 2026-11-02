#!/usr/bin/env python3
"""Boots real VMs from Iso-Vault over a private Docker network, and checks their serial logs.

Each case is one firmware type against one DHCP mode. A pass means the VM went from power-on to
Alpine's login screen, with every file coming from Iso-Vault.
"""

from __future__ import annotations

import argparse
import itertools
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODES = ("server", "proxy", "upstream")
CLIENT_MAC = "52:54:00:12:34:56"
# Runs inside the server, because the core's API is only on its socket. Pins the ISO for this VM.
PIN_SCRIPT = """
import http.client, json, socket, sys
class C(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect("/run/isovault/core.sock")
def call(method, path, body=None):
    c = C("x")
    c.request(method, path, json.dumps(body) if body else None)
    response = c.getresponse()
    return response.status, json.loads(response.read())
action, mac, relpath = sys.argv[1:4]
if action == "pin":
    isos = call("GET", "/api/isos")[1]
    iso = next(i for i in isos if i["relpath"] == relpath)
    status, _ = call("PUT", "/api/pins/" + mac, {"iso_id": iso["id"], "mode": "once"})
    sys.exit(0 if status == 200 else 1)
print(json.dumps(call("GET", "/api/clients")[1]["pins"]))
"""
FIRMWARES = ("bios-x64", "uefi-x64", "uefi-arm64")
CONSOLE = {"bios-x64": "console=ttyS0", "uefi-x64": "console=ttyS0", "uefi-arm64": "console=ttyAMA0"}
DEFAULT_ISO = {
    "bios-x64": "alpine-virt-x86_64.iso",
    "uefi-x64": "alpine-virt-x86_64.iso",
    "uefi-arm64": "alpine-virt-aarch64.iso",
}
EXPECTED_ARCH = {"bios-x64": "platform=pcbios", "uefi-x64": "platform=efi", "uefi-arm64": "arch=arm64"}
# Printed just before the login prompt. The prompt itself ends without a newline, so Docker's log
# buffer holds it back, but this banner proves the same thing: init finished and getty is running.
SUCCESS = "Welcome to Alpine"
# In order: each proves the boot got further than the one before.
IPXE_MILESTONES = ("Iso-Vault: configuring network", "Iso-Vault: boot server is", "Iso-Vault: booting")
LINUX_MILESTONES = ("Linux version", SUCCESS)
# BIOS iPXE only writes to the VGA screen, so these clients are checked through the server's log.
MILESTONES = {
    "bios-x64": LINUX_MILESTONES,
    "uefi-x64": IPXE_MILESTONES + LINUX_MILESTONES,
    "uefi-arm64": IPXE_MILESTONES + LINUX_MILESTONES,
}
print_lock = threading.Lock()
subnet_counter = itertools.count(40)


def say(message: str) -> None:
    with print_lock:
        print(message, flush=True)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)}\n{result.stderr.strip()}")
    return result


@dataclass
class Case:
    mode: str
    firmware: str
    index: int
    image: str
    client_image: str
    iso_dir: Path
    log_dir: Path
    timeout: int
    pinned: bool = False

    @property
    def name(self) -> str:
        return f"{'pinned-' if self.pinned else ''}{self.mode}-{self.firmware}"

    @property
    def prefix(self) -> str:
        return f"iv-e2e-{self.name}"

    @property
    def subnet(self) -> str:
        return f"172.30.{self.index}"


def start_router(case: Case, network: str) -> None:
    """A stand-in for the LAN's DHCP server, using the dnsmasq already in the image."""
    subnet = case.subnet
    arguments = [
        "--keep-in-foreground",
        "--log-facility=-",
        "--log-dhcp",
        "--port=0",
        "--conf-file=/dev/null",
        f"--dhcp-range={subnet}.100,{subnet}.200,255.255.255.0,10m",
        "--dhcp-leasefile=/tmp/leases",
    ]
    if case.mode == "upstream":
        server = f"{subnet}.10"
        arguments += [
            "--dhcp-match=set:bios,option:client-arch,0",
            "--dhcp-match=set:x64,option:client-arch,7",
            "--dhcp-match=set:x64,option:client-arch,9",
            "--dhcp-match=set:arm64,option:client-arch,11",
            f"--dhcp-boot=tag:bios,undionly.kpxe,,{server}",
            f"--dhcp-boot=tag:x64,snponly-x64.efi,,{server}",
            f"--dhcp-boot=tag:arm64,snponly-arm64.efi,,{server}",
        ]
    docker(
        "run", "-d", "--name", f"{case.prefix}-router", "--network", network, "--ip", f"{subnet}.2",
        "--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW", "--entrypoint", "dnsmasq", case.image, *arguments,
    )


def run_case(case: Case) -> tuple[str, bool, str]:
    network = case.prefix
    containers = [f"{case.prefix}-{role}" for role in ("router", "server", "client")]
    log_path = case.log_dir / f"{case.name}.serial.log"
    try:
        docker("network", "create", "--internal", "--subnet", f"{case.subnet}.0/24", network)
        if case.mode in ("proxy", "upstream"):
            start_router(case, network)
        # A pinned VM gets a menu that waits forever, so it can only boot if the pin works.
        menu = ["-e", "MENU_TIMEOUT=0"] if case.pinned else [
            "-e", "MENU_TIMEOUT=2", "-e", f"DEFAULT_ISO={DEFAULT_ISO[case.firmware]}",
        ]  # fmt: skip
        docker(
            "run", "-d", "--name", f"{case.prefix}-server", "--network", network, "--ip", f"{case.subnet}.10",
            "--privileged",
            "-e", f"DHCP_MODE={case.mode}",
            "-e", "STABLE_SECONDS=0",
            *menu,
            "-e", f"EXTRA_KERNEL_ARGS={CONSOLE[case.firmware]}",
            "-e", "ENABLE_SMB=false",
            "-v", f"{case.iso_dir}:/isos:ro",
            case.image,
        )  # fmt: skip
        if not wait_for_isos(case):
            return case.name, False, "the ISOs never became ready"
        if case.pinned:
            docker(
                "exec", f"{case.prefix}-server", "python3", "-c", PIN_SCRIPT,
                "pin", CLIENT_MAC, DEFAULT_ISO[case.firmware],
            )  # fmt: skip

        docker(
            "run", "-d", "--name", f"{case.prefix}-client", "--network", network, "--ip", f"{case.subnet}.50",
            "--privileged", "-t", case.client_image, case.firmware,
        )  # fmt: skip
        passed, detail = watch_client(case, log_path)
        return case.name, passed, detail
    except RuntimeError as error:
        return case.name, False, str(error)
    finally:
        save_logs(case, containers)
        for container in containers:
            docker("rm", "-f", container, check=False)
        docker("network", "rm", network, check=False)


def wait_for_isos(case: Case) -> bool:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        logs = docker("logs", f"{case.prefix}-server", check=False)
        text = logs.stdout + logs.stderr
        if DEFAULT_ISO[case.firmware] in text and "alpine" in text:
            return True
        time.sleep(1)
    return False


def watch_client(case: Case, log_path: Path) -> tuple[bool, str]:
    deadline = time.monotonic() + case.timeout
    milestones = MILESTONES[case.firmware]
    reached = 0
    text = ""
    while time.monotonic() < deadline:
        result = docker("logs", f"{case.prefix}-client", check=False)
        text = result.stdout + result.stderr
        while reached < len(milestones) and milestones[reached] in text:
            reached += 1
            say(f"  {case.name}: reached \"{milestones[reached - 1]}\"")
        if reached == len(milestones):
            log_path.write_text(text, errors="replace")
            return verify_server_saw_client(case)
        running = docker("inspect", "-f", "{{.State.Running}}", f"{case.prefix}-client", check=False)
        if running.stdout.strip() != "true":
            log_path.write_text(text, errors="replace")
            return False, f"the VM stopped early, after \"{milestones[reached - 1] if reached else 'power-on'}\""
        time.sleep(3)
    log_path.write_text(text, errors="replace")
    stuck = milestones[reached - 1] if reached else "power-on"
    return False, f"timed out after {case.timeout}s; last milestone was \"{stuck}\""


def verify_server_saw_client(case: Case) -> tuple[bool, str]:
    """The serial log could be fooled by a stale script, so check the server's side too."""
    logs = docker("logs", f"{case.prefix}-server", check=False)
    text = logs.stdout + logs.stderr
    for needle in ("/ipxe/menu", "/ipxe/boot/alpine", EXPECTED_ARCH[case.firmware], "modloop-virt"):
        if needle not in text:
            return False, f"the server's log never mentioned {needle}"
    if case.pinned:
        pins = docker(
            "exec", f"{case.prefix}-server", "python3", "-c", PIN_SCRIPT, "list", CLIENT_MAC, "",
        ).stdout.strip()  # fmt: skip
        if pins != "{}":
            return False, f"booted, but the once pin wasn't used up: {pins}"
        return True, "booted to the login screen from its pin, and the pin was used up"
    return True, "booted to the login screen"


def save_logs(case: Case, containers: list[str]) -> None:
    for container in containers:
        result = docker("logs", container, check=False)
        if result.returncode == 0:
            role = container.rsplit("-", 1)[-1]
            (case.log_dir / f"{case.name}.{role}.log").write_text(result.stdout + result.stderr, errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="iso-vault:dev")
    parser.add_argument("--iso-dir", type=Path, default=HERE / ".cache" / "isos")
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--firmwares", default=",".join(FIRMWARES))
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=420)
    parser.add_argument("--log-dir", type=Path, default=HERE / "logs")
    parser.add_argument("--no-pinned", action="store_true", help="Skip the pinned boot cases")
    arguments = parser.parse_args()

    client_image = "iso-vault-e2e-client:dev"
    say("Building the client image")
    subprocess.run(
        ["docker", "build", "-q", "-t", client_image, "-f", str(HERE / "Dockerfile.client"), str(HERE)],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    log_dir = arguments.log_dir.resolve()
    shutil.rmtree(log_dir, ignore_errors=True)
    log_dir.mkdir(parents=True)

    cases = [
        Case(mode, firmware, next(subnet_counter), arguments.image, client_image,
             arguments.iso_dir.resolve(), log_dir, arguments.timeout)
        for mode in filter(None, arguments.modes.split(","))
        for firmware in arguments.firmwares.split(",")
    ]  # fmt: skip
    if not arguments.no_pinned:
        # One per firmware is enough, because the DHCP mode doesn't affect how pins work.
        cases += [
            Case(MODES[0], firmware, next(subnet_counter), arguments.image, client_image,
                 arguments.iso_dir.resolve(), log_dir, arguments.timeout, pinned=True)
            for firmware in arguments.firmwares.split(",")
        ]  # fmt: skip

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=arguments.jobs) as pool:
        results = list(pool.map(run_case, cases))

    say("")
    failures = 0
    for name, passed, detail in sorted(results):
        failures += not passed
        say(f"{'PASS' if passed else 'FAIL'}  {name}: {detail}")
    say(f"\n{len(results) - failures} of {len(results)} passed in {time.monotonic() - started:.0f}s. Logs are in {log_dir}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
