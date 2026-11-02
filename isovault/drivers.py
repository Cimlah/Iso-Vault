"""Network drivers for Windows PE, which often has none for newer network cards.

wimboot injects these files, and its limits shape this module. Everything lands flat in
X:\\Windows\\System32, names over 31 characters get cut short, and too many files stop the boot
altogether. So a driver is only included whole, and only if it fits, and anything left out is
reported instead of failing later on the device.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .profiles import ARM64, X86_64

SUBFOLDERS = {X86_64: "x64", ARM64: "arm64"}
# wimboot allows 63 files in all, and the ISO's boot files, fonts and loaders need some of those.
MAX_FILES = 40
# This also keeps names safe to put in iPXE scripts and batch files.
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,30}$")
# Driver downloads often come with these. wimboot wouldn't inject the last four anyway.
IGNORED_SUFFIXES = (
    ".exe", ".msi", ".bat", ".cmd", ".ps1",
    ".txt", ".pdf", ".htm", ".html", ".rtf", ".doc", ".docx", ".chm", ".url", ".log",
    ".zip", ".cab", ".png", ".jpg", ".ico", ".ini",
    ".wim", ".sdi", ".efi", ".ttf",
)  # fmt: skip
# autorun.inf isn't a driver. The rest would clash with files a Windows boot depends on.
IGNORED_NAMES = ("autorun.inf", "bcd", "bootmgr", "boot.stl", "wimboot")


@dataclass(frozen=True)
class DriverSet:
    files: dict[str, Path]  # Flat name, as Windows PE will see it, to its path on this server
    infs: tuple[str, ...]
    problems: tuple[str, ...]


def _is_driver_file(directory: str, name: str) -> bool:
    lowered = name.lower()
    if name.startswith(".") or lowered in IGNORED_NAMES or lowered.endswith(IGNORED_SUFFIXES):
        return False
    path = os.path.join(directory, name)
    # A link could point anywhere on the server, and these files are served without a login.
    return os.path.isfile(path) and not os.path.islink(path)


def find_drivers(root: Path, arch: str) -> DriverSet:
    """Each folder holding an .inf file counts as one driver, with the other files beside it."""
    base = root / SUBFOLDERS[arch]
    files: dict[str, Path] = {}
    infs: list[str] = []
    problems: list[str] = []
    taken: set[str] = set()

    for directory, subdirs, names in os.walk(base):
        subdirs[:] = sorted(d for d in subdirs if not d.startswith("."))
        package = sorted(n for n in names if _is_driver_file(directory, n))
        package_infs = [n for n in package if n.lower().endswith(".inf")]
        if not package_infs:
            continue

        label = os.path.relpath(directory, root).replace(os.sep, "/")
        unsafe = next((n for n in package if not SAFE_NAME.match(n)), None)
        clash = next((n for n in package if n.lower() in taken), None)
        if unsafe:
            problems.append(
                f"Skipped the driver in {label}, because of the file {unsafe!r}. Names can only use "
                "letters, digits, dots, dashes and underscores, and be up to 31 characters long."
            )
        elif clash:
            problems.append(f"Skipped the driver in {label}, because another driver also has a file called {clash}.")
        elif len(files) + len(package) > MAX_FILES:
            problems.append(
                f"Skipped the driver in {label}, because it would go over the limit of {MAX_FILES} driver "
                "files. Keep only the drivers you need in this folder."
            )
        else:
            files.update((name, Path(directory) / name) for name in package)
            taken.update(name.lower() for name in package)
            infs.extend(package_infs)

    return DriverSet(files, tuple(infs), tuple(problems))


def driver_commands(infs: Iterable[str]) -> str:
    """Batch commands to load drivers once Windows PE has started."""
    infs = list(infs)
    if not infs:
        return "rem The server has no extra network drivers for this architecture."
    lines = ["echo Loading network drivers..."]
    lines += [f"drvload X:\\Windows\\System32\\{inf}" for inf in infs]
    lines.append("wpeutil InitializeNetwork")
    return "\n".join(lines)
