"""The core's HTTP API, served on a Unix socket.

nginx only forwards /ipxe/ and /healthz from the network. The /api/ routes can only be reached
through the socket, which is how Next.js talks to the core after checking the user's session.
"""

from __future__ import annotations

import json
import logging
import os
import socketserver
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__, ipxe
from .config import Config, Secrets
from .netboot import DebianNetboot, NetbootError, find_suite
from .network import NetInfo
from .profiles import BootContext, Tree, build_linux_plan, ram_gib
from .scanner import READY, Entry, Scanner
from .state import State, StateError, normalize_mac

log = logging.getLogger("isovault.api")

KNOWN_PLATFORMS = ("efi", "pcbios")
MAX_BODY = 64 * 1024


@dataclass
class Runtime:
    """Everything request handlers need, so they can be tested without real services."""

    config: Config
    net: NetInfo
    secrets: Secrets
    state: State
    scanner: Scanner
    netboot: DebianNetboot
    effective_mode: str
    features: frozenset[str] = frozenset()
    warnings: list[str] = field(default_factory=list)
    service_status: Callable[[], dict] = dict

    @property
    def base_url(self) -> str:
        return f"http://{self.net.ip}:{self.config.boot_port}"


def is_compatible(entry: Entry, client: ipxe.Client) -> bool:
    return not entry.archs or client.arch in entry.archs


def unavailable_reason(entry: Entry, features: frozenset[str]) -> str | None:
    if entry.profile is None:
        return "No boot profile matches this ISO."
    missing = set(entry.profile.requires) - features
    if "smb" in missing:
        return "Needs the Samba share, which is not running. Is port 445 in use on the server?"
    if missing:
        return f"Needs {', '.join(sorted(missing))}, which is not running."
    return None


def visible_entries(runtime: Runtime, client: ipxe.Client) -> tuple[list[Entry], int]:
    """ISOs this device can boot, and how many others were left out because of its architecture."""
    visible: list[Entry] = []
    hidden = 0
    for entry in runtime.scanner.snapshot():
        if entry.status != READY or unavailable_reason(entry, runtime.features):
            continue
        if is_compatible(entry, client):
            visible.append(entry)
        else:
            hidden += 1
    return visible, hidden


def parse_client(query: dict[str, list[str]]) -> ipxe.Client:
    first = lambda name: query.get(name, [""])[0]
    platform = first("platform")
    return ipxe.Client(
        mac=normalize_mac(first("mac")) or "",
        arch=ipxe.client_arch(first("arch")),
        platform=platform if platform in KNOWN_PLATFORMS else "efi",
    )


def menu_script(runtime: Runtime, client: ipxe.Client, ip: str, skip_pin: bool) -> str:
    if client.mac:
        runtime.state.record_client(client.mac, client.arch, client.platform, ip)

    visible, hidden = visible_entries(runtime, client)
    if client.mac and not skip_pin:
        pin = runtime.state.get_pin(client.mac)
        if pin:
            pinned = next((e for e in visible if e.id == pin["iso_id"]), None)
            if pinned:
                return ipxe.render_pinned(runtime.base_url, client, pinned.id, pinned.name)
            log.warning("%s is pinned to %s, which isn't available. Showing the menu.", client.mac, pin["iso_id"])

    default = runtime.scanner.find_by_relpath(runtime.config.default_iso)
    default_id = default.id if default and any(e.id == default.id for e in visible) else None
    items = [
        ipxe.MenuItem(id=e.id, name=e.name, group=e.group, note=e.profile.menu_note if e.profile else "")
        for e in visible
    ]
    return ipxe.render_menu(
        runtime.base_url,
        client,
        items,
        hidden_count=hidden,
        timeout_seconds=runtime.config.menu_timeout,
        default_id=default_id,
    )


def boot_script(runtime: Runtime, client: ipxe.Client, iso_id: str, ip: str, from_pin: bool) -> str:
    entry = runtime.scanner.get(iso_id)
    if entry is None or entry.status != READY or entry.profile is None:
        return ipxe.error_script("That ISO is no longer available.")
    if not is_compatible(entry, client):
        return ipxe.error_script("That ISO cannot boot on this architecture.")
    reason = unavailable_reason(entry, runtime.features)
    if reason:
        return ipxe.error_script(reason)

    script = _render_boot(runtime, client, entry)

    if client.mac:
        runtime.state.record_client(client.mac, client.arch, client.platform, ip, iso_id=entry.id)
        if from_pin:
            runtime.state.consume_pin(client.mac, entry.id)
    return script


def _render_boot(runtime: Runtime, client: ipxe.Client, entry: Entry) -> str:
    profile = entry.profile
    assert profile is not None
    base = runtime.base_url
    file_base = f"{base}/files/{entry.id}"
    tree = Tree(Path(entry.root))

    if profile.method == "sanboot":
        return ipxe.render_sanboot(entry.name, f"{base}/raw/{entry.id}.iso")

    if profile.method == "windows":
        bcd = tree.find("boot/bcd")
        sdi = tree.find("boot/boot.sdi")
        wim = tree.find("sources/boot.wim")
        if not (bcd and sdi and wim):
            return ipxe.error_script("This Windows ISO is missing its boot files.")
        return ipxe.render_windows(client, base, entry.id, entry.name, file_base, bcd, sdi, wim)

    if profile.method == "debian-netboot":
        suite = find_suite(tree)
        if not suite:
            return ipxe.error_script("Could not find the Debian release on this ISO.")
        try:
            relative = runtime.netboot.ensure(suite, client.arch)
        except NetbootError as error:
            log.error("%s", error)
            return ipxe.error_script("Could not download Debian's netboot files. Does the server have internet access?")
        return ipxe.render_debian_netboot(
            client, entry.name, f"{base}/netboot/{relative}", runtime.config.extra_kernel_args
        )

    context = BootContext(
        arch=client.arch,
        platform=client.platform,
        server=runtime.net.ip,
        http_base=file_base,
        iso_url=f"{base}/raw/{entry.id}.iso",
        nfs_path=entry.root,
        iso_size=entry.size,
        mac=client.mac,
        features=runtime.features,
        extra_args=runtime.config.extra_kernel_args,
    )
    plan = build_linux_plan(profile, tree, context)
    if plan.error:
        log.error("%s: %s", entry.relpath, plan.error)
        return ipxe.error_script(plan.error)
    return ipxe.render_linux(client, entry.name, file_base, plan)


def render_windows_file(runtime: Runtime, iso_id: str, name: str) -> str | None:
    entry = runtime.scanner.get(iso_id)
    if entry is None or entry.status != READY or name not in ("winpeshl.ini", "startup.bat"):
        return None
    if entry.profile is None or entry.profile.method != "windows":
        return None
    text = (runtime.config.templates_dir / "windows" / name).read_text()
    text = (
        text.replace("@@SERVER_IP@@", runtime.net.ip)
        .replace("@@ISO_ID@@", entry.id)
        .replace("@@SMB_PASSWORD@@", runtime.secrets.smb_password)
    )
    # Windows batch files need CRLF line endings.
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def describe_entry(runtime: Runtime, entry: Entry) -> dict:
    warnings = list(entry.warnings)
    reason = unavailable_reason(entry, runtime.features) if entry.status == READY else None
    if reason:
        warnings.append(reason)
    if entry.status == READY and entry.profile is not None and entry.profile.method == "linux":
        warnings.extend(_variant_warnings(runtime, entry))
    return {
        "id": entry.id,
        "name": entry.name,
        "relpath": entry.relpath,
        "group": entry.group,
        "size": entry.size,
        "status": entry.status,
        "error": entry.error,
        "method": entry.method,
        "profile": entry.profile.name if entry.profile else None,
        "profile_label": entry.profile.label if entry.profile else None,
        "archs": list(entry.archs),
        "bootable": entry.status == READY and reason is None,
        "warnings": warnings,
    }


def _variant_warnings(runtime: Runtime, entry: Entry) -> list[str]:
    assert entry.profile is not None
    variant = next((v for v in entry.profile.variants if set(v.requires) <= runtime.features), None)
    if variant is None or not variant.warning:
        return []
    text = variant.warning.replace("{iso_gib}", f"{entry.size / 2**30:.1f}")
    return [text.replace("{ram_gib}", ram_gib(entry.size))]


def status_payload(runtime: Runtime) -> dict:
    config = runtime.config
    return {
        "version": __version__,
        "dhcp_mode": runtime.effective_mode,
        "configured_dhcp_mode": config.dhcp_mode,
        "server_ip": runtime.net.ip,
        "interface": runtime.net.iface,
        "boot_port": config.boot_port,
        "ui_port": config.ui_port,
        "features": sorted(runtime.features),
        "services": runtime.service_status(),
        "warnings": list(runtime.warnings),
        "upstream": {
            "next_server": runtime.net.ip,
            "filenames": {
                "BIOS (arch 0)": "undionly.kpxe",
                "x64 UEFI (arch 7 and 9)": "snponly-x64.efi",
                "ARM64 UEFI (arch 11)": "snponly-arm64.efi",
            },
        },
    }


class Handler(BaseHTTPRequestHandler):
    runtime: Runtime
    protocol_version = "HTTP/1.1"
    server_version = "IsoVault"

    # Unix socket peers have no address, so the default logging and formatting would fail.
    def address_string(self) -> str:
        return self.headers.get("X-Real-IP", "local") if self.headers else "local"

    def log_message(self, format: str, *args) -> None:
        log.debug("%s %s", self.address_string(), format % args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _text(self, text: str, status: int = 200) -> None:
        self._send(status, text.encode(), "text/plain; charset=utf-8")

    def _json(self, value: object, status: int = 200) -> None:
        self._send(status, json.dumps(value).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if not 0 < length <= MAX_BODY:
            return None
        try:
            value = json.loads(self.rfile.read(length))
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        parts = [unquote(p) for p in url.path.split("/") if p]
        query = parse_qs(url.query)
        try:
            if parts[:1] == ["ipxe"] and method == "GET":
                self._handle_ipxe(parts[1:], query)
            elif parts[:1] == ["api"]:
                self._handle_api(method, parts[1:])
            elif parts == ["healthz"] and method == "GET":
                self._json({"status": "ok"})
            else:
                self._error(404, "Not found.")
        except BrokenPipeError:
            pass
        except Exception:
            log.exception("Request failed: %s %s", method, url.path)
            self._error(500, "Internal error.")

    def _handle_ipxe(self, parts: list[str], query: dict[str, list[str]]) -> None:
        client = parse_client(query)
        ip = self.headers.get("X-Real-IP", "")
        if parts == ["menu"]:
            skip_pin = "nopin" in query
            self._text(menu_script(self.runtime, client, ip, skip_pin))
        elif len(parts) == 2 and parts[0] == "boot":
            self._text(boot_script(self.runtime, client, parts[1], ip, "pin" in query))
        elif len(parts) == 3 and parts[0] == "windows":
            content = render_windows_file(self.runtime, parts[1], parts[2])
            if content is None:
                self._error(404, "Not found.")
            else:
                self._text(content)
        else:
            self._error(404, "Not found.")

    def _handle_api(self, method: str, parts: list[str]) -> None:
        runtime = self.runtime
        if parts == ["status"] and method == "GET":
            self._json(status_payload(runtime))
        elif parts == ["isos"] and method == "GET":
            self._json([describe_entry(runtime, e) for e in runtime.scanner.snapshot()])
        elif parts == ["clients"] and method == "GET":
            self._json(runtime.state.snapshot())
        elif len(parts) == 2 and parts[0] == "pins":
            self._handle_pin(method, parts[1])
        else:
            self._error(404, "Not found.")

    def _handle_pin(self, method: str, mac: str) -> None:
        runtime = self.runtime
        try:
            if method == "PUT":
                body = self._read_json()
                if body is None or not isinstance(body.get("iso_id"), str):
                    self._error(400, "Send a JSON body with an iso_id.")
                    return
                if runtime.scanner.get(body["iso_id"]) is None:
                    self._error(404, "That ISO doesn't exist.")
                    return
                mode = body.get("mode", "once")
                self._json(runtime.state.set_pin(mac, body["iso_id"], mode if isinstance(mode, str) else ""))
            elif method == "DELETE":
                self._json({"removed": runtime.state.clear_pin(mac)})
            else:
                self._error(405, "Method not allowed.")
        except StateError as error:
            self._error(400, str(error))


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, runtime: Runtime, group_gid: int | None = None) -> None:
        handler = type("BoundHandler", (Handler,), {"runtime": runtime})
        path.unlink(missing_ok=True)
        super().__init__(str(path), handler)
        os.chmod(path, 0o660)
        if group_gid is not None:
            os.chown(path, 0, group_gid)

    def get_request(self):
        # http.server expects a (host, port) peer address, which Unix sockets don't have.
        connection, _ = super().get_request()
        return connection, ("local", 0)
