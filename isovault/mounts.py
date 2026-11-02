"""Makes an ISO's files available as a directory, by loop mount or by extraction."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("isovault.mounts")

LOOP_MAJOR = 7
# Hosts with many snaps can already be using dozens of loop devices.
LOOP_NODES = 256
# Few files means bsdtar read only the ISO9660 stub of a UDF disc, as on Windows ISOs.
UDF_STUB_MAX_FILES = 3


class AttachError(Exception):
    """The ISO couldn't be mounted or extracted. The message is shown in the UI."""


@dataclass(frozen=True)
class Attachment:
    root: Path
    method: str  # "mount" or "extract"


def ensure_loop_nodes(count: int = LOOP_NODES) -> None:
    """Containers only get the loop devices that existed at startup, so make some more."""
    for number in range(count):
        path = f"/dev/loop{number}"
        if os.path.exists(path):
            continue
        try:
            os.mknod(path, 0o660 | stat.S_IFBLK, os.makedev(LOOP_MAJOR, number))
        except OSError:
            return


def _run(argv: list[str], timeout: float | None = 120) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _count_files(root: Path, stop_after: int) -> int:
    count = 0
    for _, _, files in os.walk(root):
        count += len(files)
        if count > stop_after:
            break
    return count


class Attacher:
    def __init__(self, mnt_dir: Path, cache_dir: Path, access: str = "auto") -> None:
        self.mnt_dir = mnt_dir
        self.cache_dir = cache_dir
        self.access = access

    def attach(self, iso_id: str, iso_path: Path, fingerprint: str) -> Attachment:
        errors: list[str] = []
        if self.access in ("auto", "mount"):
            try:
                return self._mount(iso_id, iso_path)
            except AttachError as error:
                errors.append(str(error))
                if self.access == "mount":
                    raise
                log.info("%s: can't mount (%s), extracting instead", iso_path.name, error)
        try:
            return self._extract(iso_id, iso_path, fingerprint)
        except AttachError as error:
            errors.append(str(error))
        raise AttachError("; ".join(errors))

    def detach(self, iso_id: str, keep_cache: bool = False) -> None:
        target = self.mnt_dir / iso_id
        if target.is_symlink():
            target.unlink(missing_ok=True)
        elif target.is_dir():
            _run(["umount", "-l", str(target)])
            try:
                target.rmdir()
            except OSError as error:
                log.warning("couldn't remove %s: %s", target, error)
        if not keep_cache:
            shutil.rmtree(self.cache_dir / iso_id, ignore_errors=True)

    def _mount(self, iso_id: str, iso_path: Path) -> Attachment:
        target = self.mnt_dir / iso_id
        if target.is_symlink():
            target.unlink()
        target.mkdir(parents=True, exist_ok=True)
        # UDF first: Windows ISOs also contain an ISO9660 stub holding only a README.
        # Forcing the permissions matters because nginx and Samba don't run as root, and an ISO
        # built from a private folder can carry modes like 0700 that would lock them out.
        attempts = (
            ("udf", "ro,loop,mode=0444,dmode=0555"),
            ("iso9660", "ro,loop,mode=0444,dmode=0555,overriderockperm"),
        )
        detail = ""
        for filesystem, options in attempts:
            result = _run(["mount", "-t", filesystem, "-o", options, str(iso_path), str(target)])
            if result.returncode == 0:
                return Attachment(target, "mount")
            detail = result.stderr.strip() or f"mount exited with {result.returncode}"
        target.rmdir()
        raise AttachError(detail)

    def _extract(self, iso_id: str, iso_path: Path, fingerprint: str) -> Attachment:
        destination = self.cache_dir / iso_id
        marker = destination / ".iso-vault-complete"
        if not (marker.exists() and marker.read_text() == fingerprint):
            shutil.rmtree(destination, ignore_errors=True)
            destination.mkdir(parents=True)
            self._unpack(iso_path, destination)
            marker.write_text(fingerprint)
            _make_readable(destination)

        link = self.mnt_dir / iso_id
        if link.is_symlink():
            link.unlink()
        elif link.is_dir():
            link.rmdir()
        self.mnt_dir.mkdir(parents=True, exist_ok=True)
        link.symlink_to(destination)
        return Attachment(destination, "extract")

    def _unpack(self, iso_path: Path, destination: Path) -> None:
        result = _run(["bsdtar", "-xf", str(iso_path), "-C", str(destination)], timeout=None)
        if result.returncode == 0 and _count_files(destination, UDF_STUB_MAX_FILES) > UDF_STUB_MAX_FILES:
            return

        shutil.rmtree(destination, ignore_errors=True)
        destination.mkdir(parents=True)
        result = _run(["7z", "x", "-y", f"-o{destination}", str(iso_path)], timeout=None)
        if result.returncode != 0:
            shutil.rmtree(destination, ignore_errors=True)
            lines = (result.stderr or result.stdout).strip().splitlines()
            raise AttachError(f"couldn't extract the ISO: {lines[-1] if lines else 'unknown error'}")


def _make_readable(root: Path) -> None:
    """Extracted permissions can be restrictive, and nginx doesn't run as root."""
    root.chmod(0o755)
    for directory, subdirs, files in os.walk(root):
        for name in subdirs:
            os.chmod(os.path.join(directory, name), 0o755)
        for name in files:
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                os.chmod(path, 0o644)
