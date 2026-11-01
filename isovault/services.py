"""Renders service configs, and keeps the services running."""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .config import Config
from .network import NetInfo, default_range

log = logging.getLogger("isovault.services")

PLACEHOLDER = re.compile(r"@@([A-Z_]+)@@")
LOADERS = (
    "undionly.kpxe",
    "snponly-x64.efi",
    "snponly-arm64.efi",
    "ipxe-x64.efi",
    "ipxe-arm64.efi",
    "wimboot",
    "wimboot.arm64",
)
MAX_BACKOFF = 30.0


def render(template: str, values: dict[str, str]) -> str:
    """Fill in @@NAME@@ placeholders. nginx and iPXE use $, so that syntax has to be left alone."""

    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in values:
            raise KeyError(f"Template needs {name}, which wasn't provided")
        return values[name]

    return PLACEHOLDER.sub(replace, template)


def dnsmasq_config(config: Config, net: NetInfo, mode: str) -> str:
    templates = config.templates_dir / "dnsmasq"
    values = {
        "IFACE": net.iface,
        "TFTP_ROOT": str(config.tftp_root),
        "NETWORK": net.network,
        "NETMASK": net.netmask,
        "SERVER_IP": net.ip,
        "BOOT_PORT": str(config.boot_port),
    }
    if mode == "server":
        start, end = config.dhcp_range_start, config.dhcp_range_end
        if not (start and end):
            start, end = default_range(net.ip, net.prefix)
        gateway = config.dhcp_gateway or net.gateway
        dns = config.dhcp_dns or gateway
        values.update(
            RANGE_START=start,
            RANGE_END=end,
            LEASE_TIME=config.dhcp_lease_time,
            LEASE_FILE=str(config.run_dir / "dnsmasq.leases"),
            ROUTER_LINE=f"dhcp-option=option:router,{gateway}" if gateway else "",
            DNS_LINE=f"dhcp-option=option:dns-server,{dns}" if dns else "",
        )
    parts = [(templates / "common.conf").read_text(), (templates / f"{mode}.conf").read_text()]
    return "\n".join(render(part, values) for part in parts)


def write_runtime_files(config: Config, net: NetInfo, mode: str) -> None:
    """Everything derived from this host's network goes in the run directory at startup."""
    run = config.run_dir
    for directory in (
        run,
        config.tftp_root,
        config.mnt_dir,
        config.raw_dir,
        config.netboot_dir,
        run / "nginx",
        run / "samba" / "private",
        run / "samba" / "lock",
        run / "samba" / "state",
        run / "samba" / "cache",
        run / "samba" / "ncalrpc",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    # nginx workers don't run as root, so they need to be able to reach these.
    for directory in (run, config.tftp_root, config.mnt_dir, config.raw_dir, config.netboot_dir):
        directory.chmod(0o755)

    for name in LOADERS:
        source = config.loaders_dir / name
        if source.exists():
            shutil.copyfile(source, config.tftp_root / name)
            (config.tftp_root / name).chmod(0o644)

    values = {
        "SERVER_IP": net.ip,
        "BOOT_PORT": str(config.boot_port),
        "RUN_DIR": str(run),
        "CORE_SOCKET": str(config.core_socket),
        "TFTP_ROOT": str(config.tftp_root),
        "IFACE": net.iface,
    }
    templates = config.templates_dir
    (config.tftp_root / "boot.ipxe").write_text(render((templates / "boot.ipxe").read_text(), values))
    (config.tftp_root / "boot.ipxe").chmod(0o644)
    (run / "nginx.conf").write_text(render((templates / "nginx.conf").read_text(), values))
    (run / "smb.conf").write_text(render((templates / "smb.conf").read_text(), values))
    (run / "dnsmasq.conf").write_text(dnsmasq_config(config, net, mode))
    (run / "exports").write_text("")


def write_exports(path: Path, roots: Sequence[str]) -> None:
    """One read-only export per ISO, because unfs3 doesn't cross into mounts below an export."""
    lines = [f"{root} (ro,no_root_squash,insecure)\n" for root in sorted(roots)]
    temp = path.with_suffix(".tmp")
    temp.write_text("".join(lines))
    os.replace(temp, path)


def setup_samba_user(config: Config, password: str) -> bool:
    """Create the share's login. This needs a matching system user, which the image provides."""
    try:
        result = subprocess.run(
            ["smbpasswd", "-c", str(config.run_dir / "smb.conf"), "-a", "-s", "isovault"],
            input=f"{password}\n{password}\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        log.error("Couldn't set the Samba password: %s", error)
        return False
    if result.returncode != 0:
        log.error("Couldn't set the Samba password: %s", result.stderr.strip())
        return False
    return True


@dataclass
class Service:
    name: str
    argv: Sequence[str]
    env: dict[str, str] | None = None
    cwd: str | None = None
    # Popen's user= alone keeps root's groups, so a service that drops privileges must set this too.
    user: str | None = None
    group: str | None = None
    process: subprocess.Popen | None = None
    restarts: int = 0
    next_start: float = 0.0
    backoff: float = 1.0
    started_at: float = 0.0
    last_exit: int | None = None


class Supervisor:
    """Starts services, and restarts any that exit, backing off if one keeps failing."""

    def __init__(self) -> None:
        self.services: dict[str, Service] = {}
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def add(self, service: Service) -> None:
        with self._lock:
            self.services[service.name] = service
            self._start(service)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._watch, name="supervisor", daemon=True)
        self._thread.start()

    def _start(self, service: Service) -> None:
        log.info("Starting %s", service.name)
        env = {**os.environ, **(service.env or {})}
        try:
            privileges = {"user": service.user, "group": service.group, "extra_groups": []} if service.user else {}
            service.process = subprocess.Popen(
                list(service.argv), env=env, cwd=service.cwd, stdin=subprocess.DEVNULL, **privileges
            )
            service.started_at = time.monotonic()
        except OSError as error:
            log.error("Couldn't start %s: %s", service.name, error)
            service.process = None

    def _watch(self) -> None:
        while not self._stopping.wait(1.0):
            now = time.monotonic()
            with self._lock:
                for service in self.services.values():
                    self._check(service, now)

    def _check(self, service: Service, now: float) -> None:
        process = service.process
        if process is not None:
            code = process.poll()
            if code is None:
                # Once a service has stayed up for a while, forgive its earlier failures.
                if now - service.started_at > 60:
                    service.backoff = 1.0
                return
            service.last_exit = code
            service.process = None
            service.next_start = now + service.backoff
            log.error("%s exited with code %s. Restarting in %.0fs.", service.name, code, service.backoff)
            service.backoff = min(service.backoff * 2, MAX_BACKOFF)
            service.restarts += 1
        elif now >= service.next_start:
            self._start(service)
            if service.process is None:
                service.next_start = now + service.backoff
                service.backoff = min(service.backoff * 2, MAX_BACKOFF)

    def signal(self, name: str, number: int) -> None:
        with self._lock:
            service = self.services.get(name)
            if service and service.process and service.process.poll() is None:
                service.process.send_signal(number)

    def status(self) -> dict:
        with self._lock:
            return {
                name: {
                    "running": bool(s.process and s.process.poll() is None),
                    "restarts": s.restarts,
                    "last_exit": s.last_exit,
                }
                for name, s in self.services.items()
            }

    def stop(self, timeout: float = 10.0) -> None:
        self._stopping.set()
        with self._lock:
            running = [s for s in self.services.values() if s.process and s.process.poll() is None]
            for service in running:
                service.process.terminate()
            deadline = time.monotonic() + timeout
            for service in running:
                try:
                    service.process.wait(max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    service.process.send_signal(signal.SIGKILL)
