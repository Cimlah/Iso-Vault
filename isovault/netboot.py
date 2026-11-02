"""Fetches Debian's netboot kernel and initrd.

The kernel on Debian's installer ISOs only looks for a CD, so these files boot it instead.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

from .profiles import DEBIAN_ARCH, Tree

log = logging.getLogger("isovault.netboot")

MIRROR = "https://deb.debian.org/debian"
ROLLING_NAMES = {"stable", "testing", "unstable", "oldstable", "oldoldstable", "sid"}
SUITE_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
DOWNLOAD_TIMEOUT = 120


class NetbootError(Exception):
    """The netboot files couldn't be fetched. The message is shown on the device."""


def find_suite(tree: Tree) -> str | None:
    """The release codename, from the ISO's dists/ folder."""
    names = [path.rsplit("/", 1)[-1] for path in tree.find_all("dists/*/")]
    for name in names:
        if name.lower() not in ROLLING_NAMES and SUITE_NAME.match(name):
            return name
    return None


class DebianNetboot:
    def __init__(self, directory: Path, mirror: str = MIRROR) -> None:
        self.directory = directory
        self.mirror = mirror
        self._lock = threading.Lock()

    def ensure(self, suite: str, arch: str) -> str:
        """Download the files if needed, and return their path relative to the netboot folder."""
        debian_arch = DEBIAN_ARCH[arch]
        relative = f"{suite}/{debian_arch}"
        destination = self.directory / relative
        with self._lock:
            if all((destination / name).exists() for name in ("linux", "initrd.gz")):
                return relative
            destination.mkdir(parents=True, exist_ok=True)
            base = (
                f"{self.mirror}/dists/{suite}/main/installer-{debian_arch}/current/"
                f"images/netboot/debian-installer/{debian_arch}"
            )
            for name in ("linux", "initrd.gz"):
                self._download(f"{base}/{name}", destination / name)
        return relative

    def _download(self, url: str, target: Path) -> None:
        log.info("Downloading %s", url)
        partial = target.with_suffix(target.suffix + ".part")
        try:
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response, open(partial, "wb") as out:
                shutil.copyfileobj(response, out)
        except (urllib.error.URLError, OSError) as error:
            partial.unlink(missing_ok=True)
            raise NetbootError(f"Could not download Debian netboot files: {error}") from error
        partial.replace(target)
        target.chmod(0o644)
