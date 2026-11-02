"""Entry point: `python -m isovault [run|probe-dhcp|healthcheck]`."""

from __future__ import annotations

import argparse
import grp
import json
import logging
import os
import signal
import sys
import threading
import urllib.request
from pathlib import Path

from . import config as config_module
from . import dhcp_probe, mounts, network, services
from .api import Runtime, UnixServer
from .config import Config, ConfigError
from .netboot import DebianNetboot
from .profiles import load_profiles
from .scanner import READY, Scanner
from .state import State

log = logging.getLogger("isovault")

NFS_PORTS = ((111, "tcp"), (111, "udp"), (2049, "tcp"), (2049, "udp"))
SMB_PORTS = ((445, "tcp"),)
SHARED_GROUP = "isovault"


def resolve_dhcp_mode(config: Config, net: network.NetInfo) -> tuple[str, list[str]]:
    """The rogue DHCP guard: server mode won't hand out leases next to another DHCP server."""
    if config.dhcp_mode != "server" or config.dhcp_force:
        return config.dhcp_mode, []
    try:
        offers = dhcp_probe.probe(net.iface, net.mac, ignore=(net.ip,))
    except OSError as error:
        message = f"Couldn't check for other DHCP servers ({error}), so server mode is off. Set DHCP_FORCE=true to override."
        log.error(message)
        return "proxy", [message]
    if not offers:
        return "server", []
    servers = ", ".join(sorted(o.server for o in offers))
    message = (
        f"Another DHCP server is active on this network ({servers}), so Iso-Vault isn't handing out "
        "addresses and is running in proxy mode instead. Set DHCP_FORCE=true to override."
    )
    log.error(message)
    return "proxy", [message]


def check_required_ports(config: Config, mode: str) -> None:
    needed = [(config.boot_port, "tcp", "BOOT_PORT"), (69, "udp", "TFTP")]
    if mode in ("server", "proxy"):
        needed.append((67, "udp", "DHCP"))
    if mode == "proxy":
        needed.append((4011, "udp", "ProxyDHCP"))
    for port, protocol, label in needed:
        if not network.port_free(port, protocol):
            raise ConfigError(f"Port {port}/{protocol} ({label}) is already in use on this host.")


def decide_feature(setting: str, name: str, ports: tuple, warnings: list[str]) -> bool:
    if setting == "false":
        return False
    busy = [f"{port}/{protocol}" for port, protocol in ports if not network.port_free(port, protocol)]
    if not busy:
        return True
    message = f"{name} is off because {', '.join(busy)} is already in use on this host."
    if setting == "true":
        raise ConfigError(message)
    log.warning(message)
    warnings.append(message)
    return False


def shared_gid() -> int | None:
    try:
        return grp.getgrnam(SHARED_GROUP).gr_gid
    except KeyError:
        return None


def run() -> int:
    config = config_module.load()
    net = network.detect(config)
    log.info("Serving on %s (%s), DHCP mode %s", net.ip, net.iface, config.dhcp_mode)

    secrets = config_module.load_secrets(config.data_dir)
    # Child services inherit this environment, and only the web UI gets what it needs back.
    for name in ("ADMIN_PASSWORD", "SESSION_SECRET", "SMB_PASSWORD"):
        os.environ.pop(name, None)
    # nginx needs to reach extracted ISOs. secrets.json and state.json have their own permissions.
    config.data_dir.chmod(config.data_dir.stat().st_mode | 0o011)
    config.cache_dir.mkdir(parents=True, exist_ok=True)
    config.cache_dir.chmod(0o755)

    mode, warnings = resolve_dhcp_mode(config, net)
    check_required_ports(config, mode)
    use_nfs = decide_feature(config.enable_nfs, "The NFS share", NFS_PORTS, warnings)
    use_smb = decide_feature(config.enable_smb, "The Samba share", SMB_PORTS, warnings)

    services.write_runtime_files(config, net, mode)
    mounts.ensure_loop_nodes()

    if use_smb and not services.setup_samba_user(config, secrets.smb_password):
        warnings.append("The Samba share is off because its login couldn't be created.")
        use_smb = False

    features = frozenset(name for name, enabled in (("nfs", use_nfs), ("smb", use_smb)) if enabled)
    supervisor = services.Supervisor()

    exports_lock = threading.Lock()
    scanner: Scanner | None = None

    def refresh_exports() -> None:
        if not use_nfs or scanner is None:
            return
        with exports_lock:
            roots = [e.root for e in scanner.snapshot() if e.status == READY and e.root]
            services.write_exports(config.run_dir / "exports", roots)
            supervisor.signal("unfsd", signal.SIGHUP)

    attacher = mounts.Attacher(config.mnt_dir, config.cache_dir, config.iso_access)
    profiles = load_profiles(Path(__file__).parent / "profiles", config.data_dir / "profiles.d")
    scanner = Scanner(
        config.iso_dir,
        config.raw_dir,
        attacher,
        profiles,
        stable_seconds=config.stable_seconds,
        on_change=refresh_exports,
    )
    state = State(config.data_dir / "state.json")
    runtime = Runtime(
        config=config,
        net=net,
        secrets=secrets,
        state=state,
        scanner=scanner,
        netboot=DebianNetboot(config.netboot_dir),
        effective_mode=mode,
        features=features,
        warnings=warnings,
        service_status=supervisor.status,
    )

    server = UnixServer(config.core_socket, runtime, shared_gid())
    threading.Thread(target=server.serve_forever, name="api", daemon=True).start()

    supervisor.add(
        services.Service("dnsmasq", ["dnsmasq", "--keep-in-foreground", f"--conf-file={config.run_dir / 'dnsmasq.conf'}"])
    )
    supervisor.add(services.Service("nginx", ["nginx", "-c", str(config.run_dir / "nginx.conf")]))
    if use_nfs:
        supervisor.add(services.Service("rpcbind", ["rpcbind", "-f", "-w"]))
        supervisor.add(services.Service("unfsd", ["unfsd", "-d", "-e", str(config.run_dir / "exports")]))
    if use_smb:
        supervisor.add(
            services.Service(
                "smbd",
                ["smbd", "--foreground", "--no-process-group", "--debug-stdout", "-s", str(config.run_dir / "smb.conf")],
            )
        )

    start_web_ui(config, secrets, supervisor, warnings)
    supervisor.start()

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    log.info("Ready. Put ISOs in the ISO folder, and boot a device from the network.")
    while not stop.is_set():
        try:
            scanner.scan()
            state.flush()
        except Exception:
            log.exception("Scan failed")
        stop.wait(config.scan_interval)

    log.info("Shutting down")
    state.flush()
    supervisor.stop()
    server.shutdown()
    scanner.shutdown()
    return 0


def start_web_ui(
    config: Config, secrets: config_module.Secrets, supervisor: services.Supervisor, warnings: list[str]
) -> None:
    server_js = config.web_dir / "server.js"
    if not server_js.exists():
        warnings.append("The web UI isn't included in this image.")
        return
    bind_host = "" if config.ui_bind == "0.0.0.0" else config.ui_bind
    if not network.port_free(config.ui_port, "tcp", bind_host):
        # A UI conflict shouldn't stop devices from booting.
        message = f"The web UI is off because port {config.ui_port} is already in use. Change UI_PORT in .env."
        log.error(message)
        warnings.append(message)
        return
    supervisor.add(
        services.Service(
            "web",
            ["node", "server.js"],
            cwd=str(config.web_dir),
            user=SHARED_GROUP,
            group=SHARED_GROUP,
            env={
                "NODE_ENV": "production",
                "NEXT_TELEMETRY_DISABLED": "1",
                "HOSTNAME": config.ui_bind,
                "PORT": str(config.ui_port),
                "CORE_SOCKET": str(config.core_socket),
                "ADMIN_PASSWORD": secrets.admin_password,
                "SESSION_SECRET": secrets.session_secret,
                "UI_PUBLIC_URL": config.ui_public_url,
            },
        )
    )


def probe_command(arguments: argparse.Namespace) -> int:
    """Used by install.sh to pick a DHCP mode. Prints JSON, and always takes no lease."""
    config = config_module.load()
    if arguments.interface:
        config = Config(**{**config.__dict__, "interface": arguments.interface})
    net = network.detect(config)
    offers = dhcp_probe.probe(net.iface, net.mac, timeout=arguments.timeout, ignore=(net.ip,))
    print(
        json.dumps(
            {
                "interface": net.iface,
                "ip": net.ip,
                "servers": sorted(o.server for o in offers),
            }
        )
    )
    return 0


def healthcheck() -> int:
    port = os.environ.get("BOOT_PORT", "").strip() or "8080"
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="isovault")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("run")
    commands.add_parser("healthcheck")
    probe = commands.add_parser("probe-dhcp")
    probe.add_argument("--interface", default="")
    probe.add_argument("--timeout", type=float, default=dhcp_probe.DEFAULT_TIMEOUT)
    arguments = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        if arguments.command == "healthcheck":
            return healthcheck()
        if arguments.command == "probe-dhcp":
            return probe_command(arguments)
        return run()
    except ConfigError as error:
        log.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
