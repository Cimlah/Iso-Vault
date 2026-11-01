import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from conftest import make_tree
from isovault.mounts import AttachError, Attachment
from isovault.scanner import ERROR, PREPARING, READY, WAITING, Scanner, make_id, walk_isos


class FakeAttacher:
    """Pretends to mount, by serving a prepared directory for each ISO's filename."""

    def __init__(self, trees: dict[str, Path], gate: threading.Event | None = None) -> None:
        self.trees = trees
        self.gate = gate
        self.attached: list[str] = []
        self.detached: list[str] = []

    def attach(self, iso_id, iso_path, fingerprint):
        if self.gate:
            self.gate.wait(5)
        tree = self.trees.get(iso_path.name)
        if tree is None:
            raise AttachError("not a disc image")
        self.attached.append(iso_id)
        return Attachment(tree, "mount")

    def detach(self, iso_id, keep_cache=False):
        self.detached.append(iso_id)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def world(tmp_path, profiles):
    isos = tmp_path / "isos"
    raw = tmp_path / "raw"
    isos.mkdir()
    alpine = make_tree(tmp_path / "alpine", [".alpine-release", "boot/vmlinuz-virt", "efi/boot/bootx64.efi"])
    attacher = FakeAttacher({"alpine.iso": alpine, "second.iso": alpine})
    clock = Clock()
    changes = []
    scanner = Scanner(
        isos,
        raw,
        attacher,
        profiles,
        stable_seconds=10,
        on_change=lambda: changes.append(1),
        clock=clock,
        wall_clock=lambda: time.time(),
    )
    return scanner, attacher, clock, isos, raw, changes


def settle(scanner):
    scanner._pool.shutdown(wait=True)
    scanner._pool = type(scanner._pool)(max_workers=2)


def add_iso(isos: Path, name: str = "alpine.iso", content: str = "data") -> Path:
    path = isos / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_a_new_file_waits_until_it_stops_changing(world):
    scanner, attacher, clock, isos, *_ = world
    add_iso(isos)
    scanner.scan()
    assert scanner.snapshot()[0].status == WAITING
    assert attacher.attached == []

    clock.now += 11
    scanner.scan()
    settle(scanner)
    entry = scanner.snapshot()[0]
    assert entry.status == READY
    assert entry.profile.name == "alpine"
    assert entry.archs == ("x86_64",)


def test_a_file_still_being_copied_is_left_alone(world):
    scanner, attacher, clock, isos, *_ = world
    path = add_iso(isos)
    scanner.scan()
    for size in range(5):
        clock.now += 8
        path.write_text("x" * (size + 10))
        scanner.scan()
        assert scanner.snapshot()[0].status == WAITING
    assert attacher.attached == []


def test_an_old_file_is_ready_at_once_after_a_restart(world):
    scanner, attacher, _, isos, *_ = world
    path = add_iso(isos)
    old = time.time() - 3600
    os.utime(path, (old, old))
    scanner.scan()
    settle(scanner)
    assert scanner.snapshot()[0].status == READY


def test_a_replaced_file_is_detached_and_prepared_again(world):
    scanner, attacher, clock, isos, *_ = world
    path = add_iso(isos)
    scanner.scan()
    clock.now += 11
    scanner.scan()
    settle(scanner)

    path.write_text("a longer replacement")
    clock.now += 1
    scanner.scan()
    entry = scanner.snapshot()[0]
    assert entry.status == WAITING
    assert entry.profile is None
    assert attacher.detached == [entry.id]

    clock.now += 11
    scanner.scan()
    settle(scanner)
    assert scanner.snapshot()[0].status == READY


def test_a_deleted_file_is_detached_and_forgotten(world):
    scanner, attacher, clock, isos, raw, _ = world
    path = add_iso(isos)
    scanner.scan()
    clock.now += 11
    scanner.scan()
    settle(scanner)
    iso_id = scanner.snapshot()[0].id
    assert (raw / f"{iso_id}.iso").is_symlink()

    path.unlink()
    scanner.scan()
    assert scanner.snapshot() == []
    assert attacher.detached == [iso_id]
    assert not (raw / f"{iso_id}.iso").exists()


def test_an_unreadable_iso_shows_an_error(world):
    scanner, _, clock, isos, *_ = world
    add_iso(isos, "garbage.iso")
    scanner.scan()
    clock.now += 11
    scanner.scan()
    settle(scanner)
    entry = scanner.snapshot()[0]
    assert entry.status == ERROR
    assert "not a disc image" in entry.error


def test_a_file_removed_while_preparing_doesnt_leave_a_mount(tmp_path, profiles):
    isos = tmp_path / "isos"
    isos.mkdir()
    tree = make_tree(tmp_path / "t", [".alpine-release"])
    gate = threading.Event()
    attacher = FakeAttacher({"alpine.iso": tree}, gate)
    clock = Clock()
    scanner = Scanner(isos, tmp_path / "raw", attacher, profiles, stable_seconds=0, clock=clock)
    path = add_iso(isos)
    scanner.scan()
    assert scanner.snapshot()[0].status == PREPARING

    path.unlink()
    scanner.scan()
    gate.set()
    settle(scanner)
    assert scanner.snapshot() == []
    assert attacher.attached == attacher.detached


def test_subfolders_become_groups(world):
    scanner, _, _, isos, *_ = world
    add_iso(isos, "linux/server/alpine.iso")
    scanner.scan()
    entry = scanner.snapshot()[0]
    assert entry.group == "linux/server"
    assert entry.name == "alpine.iso"


def test_walk_skips_hidden_and_other_files(tmp_path):
    (tmp_path / ".hidden").mkdir()
    for name in ("a.iso", "B.ISO", ".partial.iso", "._a.iso", "notes.txt", ".hidden/c.iso"):
        (tmp_path / name).write_text("x")
    assert sorted(walk_isos(tmp_path)) == ["B.ISO", "a.iso"]


def test_walk_skips_symlinks_that_leave_the_folder(tmp_path):
    outside = tmp_path / "outside.iso"
    outside.write_text("x")
    root = tmp_path / "isos"
    root.mkdir()
    (root / "escape.iso").symlink_to(outside)
    (root / "real.iso").write_text("y")
    (root / "alias.iso").symlink_to(root / "real.iso")
    assert sorted(walk_isos(root)) == ["alias.iso", "real.iso"]


@pytest.mark.parametrize(
    "relpath",
    ["ubuntu 24.04.iso", "a/b/ç$(reboot).iso", "../../x.iso", "${shell}.iso", "日本語.iso", "\udcff.iso"],
)
def test_ids_are_always_url_safe(relpath):
    iso_id = make_id(relpath)
    assert iso_id.replace("-", "").isalnum() and iso_id.isascii() and iso_id == iso_id.lower()


def test_ids_are_stable_and_unique_per_path():
    assert make_id("a/x.iso") == make_id("a/x.iso")
    assert make_id("a/x.iso") != make_id("b/x.iso")
