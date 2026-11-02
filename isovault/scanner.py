"""Watches the ISO folder, and keeps each ISO attached and identified."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from .mounts import Attacher, AttachError
from .profiles import Profile, Tree, detect_archs, identify

log = logging.getLogger("isovault.scanner")

WAITING = "waiting"
PREPARING = "preparing"
READY = "ready"
ERROR = "error"


@dataclass
class Entry:
    id: str
    relpath: str
    name: str
    group: str
    size: int
    mtime: float
    status: str = WAITING
    error: str = ""
    root: str = ""
    method: str = ""
    profile: Profile | None = None
    archs: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)
    stable_since: float = 0.0
    generation: int = 0


def make_id(relpath: str) -> str:
    """Stable, URL-safe and independent of the filename's characters."""
    stem = relpath.rsplit("/", 1)[-1]
    if stem.lower().endswith(".iso"):
        stem = stem[:-4]
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:40].strip("-") or "iso"
    return f"{slug}-{hashlib.sha1(relpath.encode('utf-8', 'surrogateescape')).hexdigest()[:8]}"


def walk_isos(root: Path) -> dict[str, tuple[int, float]]:
    """Every ISO under root, by relative path. Hidden files and folders are skipped."""
    found: dict[str, tuple[int, float]] = {}
    real_root = os.path.realpath(root)
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = sorted(d for d in subdirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith(".") or not name.lower().endswith(".iso"):
                continue
            path = os.path.join(directory, name)
            # A symlink out of the folder wouldn't resolve in the container's other services.
            if os.path.commonpath([real_root, os.path.realpath(path)]) != real_root:
                continue
            try:
                info = os.stat(path)
            except OSError:
                continue
            if os.path.isfile(path):
                found[os.path.relpath(path, root).replace(os.sep, "/")] = (info.st_size, info.st_mtime)
    return found


class Scanner:
    def __init__(
        self,
        iso_dir: Path,
        raw_dir: Path,
        attacher: Attacher,
        profiles: list[Profile],
        stable_seconds: float = 10.0,
        on_change: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        workers: int = 2,
    ) -> None:
        self.iso_dir = iso_dir
        self.raw_dir = raw_dir
        self.attacher = attacher
        self.profiles = profiles
        self.stable_seconds = stable_seconds
        self.on_change = on_change
        self.clock = clock
        self.wall_clock = wall_clock
        self._entries: dict[str, Entry] = {}
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="attach")

    def snapshot(self) -> list[Entry]:
        with self._lock:
            return sorted((replace(e) for e in self._entries.values()), key=lambda e: e.relpath.lower())

    def get(self, iso_id: str) -> Entry | None:
        with self._lock:
            for entry in self._entries.values():
                if entry.id == iso_id:
                    return replace(entry)
        return None

    def find_by_relpath(self, relpath: str) -> Entry | None:
        with self._lock:
            entry = self._entries.get(relpath)
            return replace(entry) if entry else None

    def scan(self) -> None:
        found = walk_isos(self.iso_dir)
        now = self.clock()
        to_prepare: list[Entry] = []
        changed = False
        with self._lock:
            for relpath in list(self._entries):
                if relpath not in found:
                    self._forget(relpath)
                    changed = True

            for relpath, (size, mtime) in found.items():
                entry = self._entries.get(relpath)
                if entry is None:
                    entry = self._add(relpath, size, mtime, now)
                    changed = True
                elif (entry.size, entry.mtime) != (size, mtime):
                    self._restart(entry, size, mtime, now)
                    changed = True
                if entry.status == WAITING and now - entry.stable_since >= self.stable_seconds:
                    entry.status = PREPARING
                    to_prepare.append(replace(entry))

        for entry in to_prepare:
            self._pool.submit(self._prepare, entry)
        if changed:
            self.on_change()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            for relpath in list(self._entries):
                self.attacher.detach(self._entries[relpath].id, keep_cache=True)

    def _add(self, relpath: str, size: int, mtime: float, now: float) -> Entry:
        parts = relpath.rsplit("/", 1)
        group, name = (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])
        entry = Entry(id=make_id(relpath), relpath=relpath, name=name, group=group, size=size, mtime=mtime)
        # A file that's been idle for a while is already complete, so don't delay it after a restart.
        settled = self.wall_clock() - mtime >= self.stable_seconds
        entry.stable_since = now - self.stable_seconds if settled else now
        self._entries[relpath] = entry
        return entry

    def _restart(self, entry: Entry, size: int, mtime: float, now: float) -> None:
        """The file changed, so whatever we knew about it is out of date."""
        self._release(entry)
        entry.size, entry.mtime = size, mtime
        entry.status, entry.error, entry.root, entry.method = WAITING, "", "", ""
        entry.profile, entry.archs, entry.warnings = None, (), []
        entry.stable_since = now
        entry.generation += 1

    def _forget(self, relpath: str) -> None:
        entry = self._entries.pop(relpath)
        self._release(entry)
        log.info("Removed %s", relpath)

    def _release(self, entry: Entry) -> None:
        if entry.root:
            self.attacher.detach(entry.id)
        (self.raw_dir / f"{entry.id}.iso").unlink(missing_ok=True)

    def _prepare(self, snapshot: Entry) -> None:
        relpath = snapshot.relpath
        path = self.iso_dir / relpath
        fingerprint = f"{snapshot.size}:{snapshot.mtime}"
        try:
            attachment = self.attacher.attach(snapshot.id, path, fingerprint)
        except (AttachError, OSError) as error:
            log.error("%s: %s", relpath, error)
            self._finish(snapshot, status=ERROR, error=f"Couldn't read this ISO: {error}")
            return

        tree = Tree(attachment.root)
        profile = identify(tree, self.profiles)
        archs = detect_archs(tree, snapshot.name)
        warnings: list[str] = []
        if profile and profile.warning:
            warnings.append(profile.warning)
        try:
            readable = os.stat(path).st_mode & 0o004
        except OSError:
            readable = 0
        if not readable:
            warnings.append(
                "The ISO file isn't world-readable, so booting that needs the whole ISO will fail. "
                "Run chmod a+r on it."
            )
        raw = self.raw_dir / f"{snapshot.id}.iso"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.unlink(missing_ok=True)
        raw.symlink_to(path)

        log.info(
            "%s: %s, %s, via %s",
            relpath,
            profile.name if profile else "unrecognised",
            "/".join(archs) or "unknown architecture",
            attachment.method,
        )
        self._finish(
            snapshot,
            status=READY,
            root=str(attachment.root),
            method=attachment.method,
            profile=profile,
            archs=archs,
            warnings=warnings,
        )

    def _finish(self, snapshot: Entry, **updates) -> None:
        with self._lock:
            entry = self._entries.get(snapshot.relpath)
            if entry is None or entry.generation != snapshot.generation:
                # It was removed or changed while we were working, so this result is stale.
                # Leave the files alone if a newer attempt has already taken over this ID.
                if entry is None or entry.status == WAITING:
                    if updates.get("root"):
                        self.attacher.detach(snapshot.id)
                    (self.raw_dir / f"{snapshot.id}.iso").unlink(missing_ok=True)
                return
            for key, value in updates.items():
                setattr(entry, key, value)
        self.on_change()
