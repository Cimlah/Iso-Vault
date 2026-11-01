#!/usr/bin/env python3
"""Downloads Alpine's small virt ISOs for the E2E tests, and checks their published hashes."""

from __future__ import annotations

import hashlib
import re
import sys
import urllib.request
from pathlib import Path

MIRROR = "https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases"
ARCHES = ("x86_64", "aarch64")


def latest_virt(arch: str) -> tuple[str, str]:
    """Reads the release index by hand, because the tests shouldn't need a YAML library."""
    with urllib.request.urlopen(f"{MIRROR}/{arch}/latest-releases.yaml", timeout=60) as response:
        text = response.read().decode()
    for block in text.split("\n-\n"):
        if re.search(r"^\s*flavor: alpine-virt\s*$", block, re.M):
            name = re.search(r"^\s*iso: (\S+)", block, re.M).group(1)
            digest = re.search(r"^\s*sha256: (\S+)", block, re.M).group(1)
            return name, digest
    raise SystemExit(f"No virt ISO listed for {arch}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    destination = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / ".cache" / "isos")
    destination.mkdir(parents=True, exist_ok=True)
    for arch in ARCHES:
        name, expected = latest_virt(arch)
        target = destination / f"alpine-virt-{arch}.iso"
        if target.exists() and sha256(target) == expected:
            print(f"{target.name}: already downloaded ({name})")
            continue
        print(f"{target.name}: downloading {name}")
        partial = target.with_suffix(".part")
        urllib.request.urlretrieve(f"{MIRROR}/{arch}/{name}", partial)
        actual = sha256(partial)
        if actual != expected:
            partial.unlink()
            raise SystemExit(f"{name}: expected SHA-256 {expected}, got {actual}")
        partial.replace(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
