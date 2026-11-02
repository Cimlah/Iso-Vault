"""Finds the LAN interface, and checks whether the ports we need are free."""

from __future__ import annotations

import ipaddress
import json
import socket
import subprocess
from dataclasses import dataclass

from .config import Config, ConfigError

# Bridges and tunnels that are never the LAN we should serve.
IGNORED_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "tun", "tap", "wg", "cni", "flannel")


@dataclass(frozen=True)
class NetInfo:
    iface: str
    ip: str
    prefix: int
    network: str
    netmask: str
    gateway: str
    mac: str


def _ip_json(*args: str) -> list[dict]:
    output = subprocess.run(["ip", "-j", *args], capture_output=True, text=True, check=True).stdout
    return json.loads(output or "[]")


def choose_interface(routes: list[dict], links: list[dict]) -> str | None:
    """Prefer the default route's interface, then the first usable interface with an address."""
    for route in routes:
        if route.get("dst") == "default" and route.get("dev"):
            return route["dev"]
    for link in links:
        name = link.get("ifname", "")
        has_address = any(a.get("family") == "inet" for a in link.get("addr_info", []))
        if has_address and not name.startswith(IGNORED_PREFIXES):
            return name
    return None


def describe(link: dict, gateway: str, wanted_ip: str = "auto") -> NetInfo:
    addresses = [a for a in link.get("addr_info", []) if a.get("family") == "inet"]
    if wanted_ip != "auto":
        addresses = [a for a in addresses if a.get("local") == wanted_ip]
    name = link.get("ifname", "?")
    if not addresses:
        detail = f"the address {wanted_ip}" if wanted_ip != "auto" else "an IPv4 address"
        raise ConfigError(f"Interface {name} doesn't have {detail}. Check INTERFACE and SERVER_IP.")
    address = addresses[0]
    network = ipaddress.ip_interface(f"{address['local']}/{address['prefixlen']}").network
    return NetInfo(
        iface=name,
        ip=address["local"],
        prefix=address["prefixlen"],
        network=str(network.network_address),
        netmask=str(network.netmask),
        gateway=gateway,
        mac=link.get("address", ""),
    )


def detect(config: Config) -> NetInfo:
    routes = _ip_json("-4", "route")
    links = _ip_json("-4", "addr")
    name = None if config.interface == "auto" else config.interface
    if name is None:
        name = choose_interface(routes, links)
    if name is None:
        raise ConfigError("Couldn't find a network interface. Set INTERFACE in .env.")

    for link in links:
        if link.get("ifname") == name:
            break
    else:
        raise ConfigError(f"Interface {name} doesn't exist or has no IPv4 address.")

    gateway = next(
        (r.get("gateway", "") for r in routes if r.get("dst") == "default" and r.get("dev") == name),
        "",
    )
    # `ip -4 addr` omits the MAC address, so it needs a second lookup.
    details = _ip_json("link", "show", "dev", name)
    if details:
        link = {**link, "address": details[0].get("address", "")}
    return describe(link, gateway, config.server_ip)


def default_range(ip: str, prefix: int) -> tuple[str, str]:
    """A pool away from the addresses people usually assign statically."""
    network = ipaddress.ip_interface(f"{ip}/{prefix}").network
    if network.num_addresses >= 256:
        base = ipaddress.ip_interface(f"{ip}/24").network.network_address
        return str(base + 100), str(base + 200)
    if network.num_addresses < 8:
        raise ConfigError(
            f"The network {network} is too small for a DHCP pool. Set DHCP_RANGE_START and DHCP_RANGE_END."
        )
    hosts = list(network.hosts())
    third = len(hosts) // 3
    return str(hosts[third]), str(hosts[-1])


def port_free(port: int, protocol: str = "tcp", host: str = "") -> bool:
    kind = socket.SOCK_STREAM if protocol == "tcp" else socket.SOCK_DGRAM
    with socket.socket(socket.AF_INET, kind) as probe:
        # dnsmasq shares UDP ports the same way, so a daemon that also allows it isn't a conflict.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True
