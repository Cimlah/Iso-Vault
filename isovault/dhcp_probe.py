"""Looks for other DHCP servers by sending a DISCOVER.

This only ever sends DISCOVER and reads the OFFERs. It never sends REQUEST, so no lease is taken.
"""

from __future__ import annotations

import os
import select
import socket
import struct
import time
from dataclasses import dataclass

MAGIC_COOKIE = b"\x63\x82\x53\x63"
DHCPDISCOVER = 1
DHCPOFFER = 2
BOOTP_HEADER = 236
SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)
# Many DHCP servers, including dnsmasq on home routers, ping an address for a few seconds before
# offering it. Giving up too soon would miss them, and start a second DHCP server on a live LAN.
DEFAULT_TIMEOUT = 8.0
SETTLE_SECONDS = 0.5


@dataclass(frozen=True)
class Offer:
    server: str
    offered_ip: str


def build_discover(mac: str, xid: int) -> bytes:
    hardware = bytes.fromhex(mac.replace(":", ""))
    header = struct.pack(
        "!BBBBIHH4s4s4s4s16s64s128s",
        1,  # BOOTREQUEST
        1,  # Ethernet
        6,
        0,
        xid,
        0,
        0x8000,  # ask for a broadcast reply, since we have no address to reply to
        b"\0" * 4,
        b"\0" * 4,
        b"\0" * 4,
        b"\0" * 4,
        hardware.ljust(16, b"\0"),
        b"\0" * 64,
        b"\0" * 128,
    )
    options = MAGIC_COOKIE + bytes([53, 1, DHCPDISCOVER, 55, 3, 1, 3, 54, 255])
    return (header + options).ljust(300, b"\0")


def parse_offer(packet: bytes, xid: int) -> Offer | None:
    """Return the offer if this is a reply to us from a server that hands out addresses."""
    if len(packet) < BOOTP_HEADER + 4 or packet[0] != 2:
        return None
    if struct.unpack("!I", packet[4:8])[0] != xid:
        return None
    if packet[BOOTP_HEADER : BOOTP_HEADER + 4] != MAGIC_COOKIE:
        return None

    options: dict[int, bytes] = {}
    position = BOOTP_HEADER + 4
    while position < len(packet):
        code = packet[position]
        if code == 255:
            break
        if code == 0:
            position += 1
            continue
        if position + 1 >= len(packet):
            break
        length = packet[position + 1]
        options[code] = packet[position + 2 : position + 2 + length]
        position += 2 + length

    if options.get(53) != bytes([DHCPOFFER]):
        return None
    offered = socket.inet_ntoa(packet[16:20])
    # ProxyDHCP servers offer no address, so they can't conflict with server mode.
    if offered == "0.0.0.0":
        return None
    server_id = options.get(54, packet[20:24])
    server = socket.inet_ntoa(server_id) if len(server_id) == 4 else "unknown"
    return Offer(server=server, offered_ip=offered)


def probe(iface: str, mac: str, timeout: float = DEFAULT_TIMEOUT, ignore: tuple[str, ...] = ()) -> list[Offer]:
    """Broadcast a DISCOVER on `iface`, and collect offers. Stops shortly after the first one."""
    xid = struct.unpack("!I", os.urandom(4))[0]
    packet = build_discover(mac, xid)
    offers: dict[str, Offer] = {}

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, iface.encode() + b"\0")
        sock.bind(("0.0.0.0", 68))

        deadline = time.monotonic() + timeout
        next_send = 0.0
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            if now >= next_send:
                sock.sendto(packet, ("255.255.255.255", 67))
                next_send = now + 1.0
            ready, _, _ = select.select([sock], [], [], min(deadline, next_send) - now)
            if ready:
                data, _ = sock.recvfrom(4096)
                offer = parse_offer(data, xid)
                if offer and offer.server not in ignore:
                    # One is enough to decide, so only wait a moment for any others.
                    if not offers:
                        deadline = min(deadline, time.monotonic() + SETTLE_SECONDS)
                    offers[offer.server] = offer

    return list(offers.values())
