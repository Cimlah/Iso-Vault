"""Device pins and recently seen clients, saved in a JSON file."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("isovault.state")

MAC_PATTERN = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")
PIN_MODES = ("once", "always")
# The boot endpoints are open, so stop made-up MAC addresses growing the file forever.
MAX_CLIENTS = 256


class StateError(ValueError):
    """A request to change state was invalid. The message is returned to the client."""


def normalize_mac(value: str) -> str | None:
    mac = value.strip().lower().replace("-", ":")
    return mac if MAC_PATTERN.match(mac) else None


class State:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self._lock = threading.RLock()
        self._dirty = False
        self.pins: dict[str, dict] = {}
        self.clients: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            self.pins = dict(data.get("pins", {}))
            self.clients = dict(data.get("clients", {}))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, TypeError) as error:
            # Keep the damaged file for inspection instead of silently overwriting it.
            log.error("Couldn't read %s (%s). Starting with no pins.", self.path, error)
            try:
                os.replace(self.path, self.path.with_suffix(".corrupt"))
            except OSError:
                pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        with open(temp, "w") as handle:
            json.dump({"version": 1, "pins": self.pins, "clients": self.clients}, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)
        self._dirty = False

    def flush(self) -> None:
        """Saves client sightings, which are batched so boot requests don't each write to disk."""
        with self._lock:
            if self._dirty:
                self._save()

    def record_client(self, mac: str, arch: str, platform: str, ip: str, iso_id: str | None = None) -> None:
        now = self.clock()
        with self._lock:
            client = self.clients.setdefault(mac, {"first_seen": now, "last_iso": None})
            client.update(arch=arch, platform=platform, ip=ip, last_seen=now)
            if iso_id:
                client["last_iso"] = iso_id
            if len(self.clients) > MAX_CLIENTS:
                # Evict the stalest device that isn't pinned; pinned devices stay listed.
                candidates = [m for m in self.clients if m not in self.pins] or list(self.clients)
                oldest = min(candidates, key=lambda m: self.clients[m].get("last_seen", 0))
                del self.clients[oldest]
            self._dirty = True

    def set_pin(self, mac: str, iso_id: str, mode: str = "once") -> dict:
        normalized = normalize_mac(mac)
        if not normalized:
            raise StateError("That isn't a valid MAC address.")
        if mode not in PIN_MODES:
            raise StateError(f"The pin type must be one of: {', '.join(PIN_MODES)}.")
        pin = {"iso_id": iso_id, "mode": mode, "created": self.clock()}
        with self._lock:
            self.pins[normalized] = pin
            self._save()
        return pin

    def clear_pin(self, mac: str) -> bool:
        normalized = normalize_mac(mac)
        if not normalized:
            raise StateError("That isn't a valid MAC address.")
        with self._lock:
            removed = self.pins.pop(normalized, None) is not None
            if removed:
                self._save()
        return removed

    def get_pin(self, mac: str) -> dict | None:
        with self._lock:
            pin = self.pins.get(mac)
            return dict(pin) if pin else None

    def consume_pin(self, mac: str, iso_id: str) -> None:
        """A `once` pin is used up when the device actually starts booting its ISO."""
        with self._lock:
            pin = self.pins.get(mac)
            if pin and pin["mode"] == "once" and pin["iso_id"] == iso_id:
                del self.pins[mac]
                self._save()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "pins": {mac: dict(pin) for mac, pin in self.pins.items()},
                "clients": {mac: dict(client) for mac, client in self.clients.items()},
            }
