"""Helpers for running Iso-Vault and a fake LAN device in Docker.

Everything lives in named Docker volumes and networks, so these tests also work when run from
inside a container with the Docker socket mounted.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGE = os.environ.get("ISO_VAULT_IMAGE", "iso-vault:dev")
HELPER_IMAGE = "iso-vault-it-helper:dev"
MAC = "52:54:00:aa:bb:cc"
# FILE_READ_DATA, FILE_READ_ATTRIBUTES and FILE_EXECUTE, which Windows asks for to run a program.
SMB_RUN_ACCESS = 0x01 | 0x80 | 0x20
SMB_WRITE_ACCESS = 0x02

# Minimal fake ISOs: just enough for each family's detection and boot script.
ISO_TREES = {
    "alpine-test.iso": {
        ".alpine-release": "3.24.0\n",
        "boot/vmlinuz-virt": "kernel",
        "boot/initramfs-virt": "initramfs",
        "boot/modloop-virt": "modloop",
        "efi/boot/bootx64.efi": "efi",
        "apks/x86_64/APKINDEX.tar.gz": "index",
    },
    "ubuntu-desktop.iso": {
        "casper/vmlinuz": "kernel",
        "casper/initrd": "initrd",
        "casper/filesystem.squashfs": "squashfs",
        "EFI/BOOT/BOOTX64.EFI": "efi",
    },
    "windows-test.iso": {
        "bootmgr": "bootmgr",
        "Boot/BCD": "bcd",
        "Boot/boot.sdi": "sdi",
        "Sources/boot.wim": "wim",
        "setup.exe": "setup",
        "efi/boot/bootx64.efi": "efi",
    },
    "memtest-tool.iso": {"memtest.bin": "tool"},
    "arm/alpine-arm.iso": {
        ".alpine-release": "3.24.0\n",
        "boot/vmlinuz-virt": "kernel",
        "boot/initramfs-virt": "initramfs",
        "boot/modloop-virt": "modloop",
        "efi/boot/bootaa64.efi": "efi",
    },
}


def docker(*args: str, check: bool = True, timeout: float = 120, input_text: str | None = None):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, input=input_text
    )
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result


def build_helper() -> None:
    docker(
        "build", "-q", "-t", HELPER_IMAGE, "-f", str(ROOT / "tests/integration/Dockerfile.helper"),
        str(ROOT / "tests/integration"), timeout=300,
    )  # fmt: skip


def build_iso_volume(volume: str, names: list[str] | None = None) -> None:
    """Creates a Docker volume holding real ISO files, built with xorriso."""
    docker("volume", "create", volume)
    script = ["set -e", "cd /isos"]
    for iso, files in ISO_TREES.items():
        if names is not None and iso not in names:
            continue
        # mktemp makes a 0700 folder, which the ISO keeps. Iso-Vault has to serve these anyway.
        script.append("t=$(mktemp -d)")
        for path, content in files.items():
            script.append(f'mkdir -p "$t/$(dirname {path})"; printf %s {json.dumps(content)} > "$t/{path}"')
        script.append(f'mkdir -p "$(dirname {iso})"')
        script.append(f'xorriso -as mkisofs -quiet -R -J -V TEST -o {iso} "$t" 2>/dev/null')
        # Idle files are picked up immediately, which keeps the tests fast.
        script.append(f"touch -d '2020-01-01' {iso}; chmod a+r {iso}")
    docker("run", "--rm", "-v", f"{volume}:/isos", HELPER_IMAGE, "sh", "-c", "\n".join(script), timeout=180)


class Stack:
    """A private network, with an Iso-Vault server and a helper device on it."""

    counter = 60

    def __init__(self, isos: str) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.network = f"iv-it-{self.id}"
        self.server = f"iv-it-server-{self.id}"
        self.helper = f"iv-it-helper-{self.id}"
        self.data = f"iv-it-data-{self.id}"
        self.isos = isos
        self.extra: list[str] = []
        Stack.counter += 1
        self.subnet = f"172.31.{Stack.counter}"
        self.server_ip = f"{self.subnet}.10"
        self.helper_ip = f"{self.subnet}.20"
        docker("network", "create", "--subnet", f"{self.subnet}.0/24", self.network)
        docker("volume", "create", self.data)
        docker(
            "run", "-d", "--name", self.helper, "--network", self.network, "--ip", self.helper_ip,
            "--privileged", HELPER_IMAGE, "sleep", "infinity",
        )  # fmt: skip

    def start(self, *flags: str, privileged: bool = True, wait: bool = True, network: str | None = None, **env: str) -> None:
        settings = {"DHCP_MODE": "upstream", "STABLE_SECONDS": "1", "ENABLE_SMB": "auto", **env}
        arguments = [
            "run", "-d", "--name", self.server,
            "-v", f"{self.isos}:/isos:ro", "-v", f"{self.data}:/data",
        ]  # fmt: skip
        if network:
            arguments += ["--network", network]
        else:
            arguments += ["--network", self.network, "--ip", self.server_ip]
        if privileged:
            arguments.append("--privileged")
        for key, value in settings.items():
            arguments += ["-e", f"{key}={value}"]
        docker(*arguments, *flags, IMAGE)
        if wait:
            self.wait_for_core()

    def wait_for_core(self, timeout: float = 45) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.exec_server("python3", "-m", "isovault", "healthcheck", check=False).returncode == 0:
                return
            time.sleep(0.5)
        raise AssertionError(f"The server never became healthy:\n{self.logs()}")

    def logs(self) -> str:
        result = docker("logs", self.server, check=False)
        return result.stdout + result.stderr

    def exec_server(self, *command: str, check: bool = True, timeout: float = 60):
        return docker("exec", self.server, *command, check=check, timeout=timeout)

    def exec_helper(self, *command: str, check: bool = True, timeout: float = 60):
        return docker("exec", self.helper, *command, check=check, timeout=timeout)

    def api(self, path: str, method: str = "GET", body: dict | None = None):
        """Calls the core's socket API, which isn't reachable from the network."""
        script = (
            "import http.client,json,socket,sys\n"
            "class C(http.client.HTTPConnection):\n"
            "  def connect(self):\n"
            "    self.sock=socket.socket(socket.AF_UNIX); self.sock.connect('/run/isovault/core.sock')\n"
            "c=C('x'); b=sys.argv[3] or None\n"
            "c.request(sys.argv[1],sys.argv[2],b); r=c.getresponse()\n"
            "print(json.dumps({'status':r.status,'body':json.loads(r.read())}))\n"
        )
        payload = json.dumps(body) if body is not None else ""
        result = json.loads(self.exec_server("python3", "-c", script, method, path, payload).stdout)
        return result["status"], result["body"]

    def smb_password(self) -> str:
        return self.exec_server(
            "python3", "-c", "import json;print(json.load(open('/data/secrets.json'))['SMB_PASSWORD'])"
        ).stdout.strip()

    def smb_open(self, path: str, access: int = SMB_RUN_ACCESS, check: bool = True):
        """Opens a file on the share with exact access rights, which smbclient can't ask for."""
        script = (
            "import sys\n"
            "from impacket.smbconnection import SMBConnection\n"
            "server, password, path, access = sys.argv[1:]\n"
            "smb = SMBConnection(server, server)\n"
            "smb.login('isovault', password)\n"
            "tree = smb.connectTree('isovault')\n"
            "smb.closeFile(tree, smb.openFile(tree, path, desiredAccess=int(access)))\n"
        )
        return self.exec_helper(
            "python3", "-c", script, self.server_ip, self.smb_password(), path, str(access), check=check
        )

    def status(self) -> dict:
        return self.api("/api/status")[1]

    def isos_by_name(self) -> dict:
        return {iso["relpath"]: iso for iso in self.api("/api/isos")[1]}

    def wait_for_iso(self, relpath: str, status: str = "ready", timeout: float = 45) -> dict:
        deadline = time.monotonic() + timeout
        seen = None
        while time.monotonic() < deadline:
            seen = self.isos_by_name().get(relpath)
            if seen and seen["status"] == status:
                return seen
            time.sleep(0.5)
        raise AssertionError(f"{relpath} never reached {status}; last state: {seen}\n{self.logs()}")

    def wait_for_all_isos(self, timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        isos: dict = {}
        while time.monotonic() < deadline:
            isos = self.isos_by_name()
            if isos and all(iso["status"] in ("ready", "error") for iso in isos.values()):
                return isos
            time.sleep(0.5)
        raise AssertionError(f"ISOs never settled: {isos}\n{self.logs()}")

    def curl(self, *arguments: str, check: bool = True, target: str | None = None):
        return self.exec_helper("curl", "-sS", "--max-time", "30", *arguments, check=check)

    def boot_url(self, path: str) -> str:
        return f"http://{self.server_ip}:8080{path}"

    def http_status(self, path: str, *flags: str) -> str:
        return self.curl("--path-as-is", "-o", "/dev/null", "-w", "%{http_code}", *flags, self.boot_url(path)).stdout

    def client_query(self, arch: str = "x86_64", platform: str = "efi", mac: str = MAC) -> str:
        return f"mac={mac}&arch={arch}&platform={platform}"

    def fetch_menu(self, **kwargs: str) -> str:
        return self.curl(self.boot_url(f"/ipxe/menu?{self.client_query(**kwargs)}")).stdout

    def fetch_boot(self, iso_id: str, **kwargs: str) -> str:
        return self.curl(self.boot_url(f"/ipxe/boot/{iso_id}?{self.client_query(**kwargs)}")).stdout

    def stop_server(self) -> None:
        docker("rm", "-f", self.server, check=False)

    def close(self) -> None:
        for name in [self.server, self.helper, *self.extra]:
            docker("rm", "-f", name, check=False)
        docker("volume", "rm", "-f", self.data, check=False)
        docker("network", "rm", self.network, check=False)


def remove_volume(volume: str) -> None:
    docker("volume", "rm", "-f", volume, check=False)


def on_volume(volume: str, script: str, timeout: float = 120) -> None:
    docker("run", "--rm", "-w", "/isos", "-v", f"{volume}:/isos", HELPER_IMAGE, "sh", "-c", script, timeout=timeout)
