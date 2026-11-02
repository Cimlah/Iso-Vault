"""Identifies an ISO's OS family, and works out how to boot it over the network."""

from __future__ import annotations

import configparser
import fnmatch
import logging
import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("isovault.profiles")

X86_64 = "x86_64"
ARM64 = "arm64"
KERNEL_ARCH = {X86_64: "x86_64", ARM64: "aarch64"}
DEBIAN_ARCH = {X86_64: "amd64", ARM64: "arm64"}

ARCH_MARKERS = (
    ("efi/boot/bootx64.efi", X86_64),
    ("efi/boot/bootaa64.efi", ARM64),
    ("install.amd/", X86_64),
    ("install.a64/", ARM64),
    ("arch/boot/x86_64/", X86_64),
    ("arch/boot/aarch64/", ARM64),
    ("boot/x86_64/", X86_64),
    ("boot/aarch64/", ARM64),
)
ARCH_ALIASES = {"x86_64": X86_64, "amd64": X86_64, "x64": X86_64, "aarch64": ARM64, "arm64": ARM64}
FILENAME_ARCH = re.compile(r"(?<![a-z0-9])(x86[_-]64|amd64|x64|aarch64|arm64)(?![a-z0-9])", re.I)
GLOB_VARIABLE = re.compile(r"\{glob:([^}]+)\}")


class ProfileError(ValueError):
    """A profile file is invalid."""


class Tree:
    """Case-insensitive, glob-aware file lookup, because ISOs vary (Boot/BCD vs boot/bcd)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._listings: dict[str, list[tuple[str, bool]]] = {}

    def _list(self, relative: str) -> list[tuple[str, bool]]:
        cached = self._listings.get(relative)
        if cached is not None:
            return cached
        entries: list[tuple[str, bool]] = []
        try:
            with os.scandir(self.root / relative) as iterator:
                entries = sorted((e.name, e.is_dir()) for e in iterator)
        except OSError:
            pass
        self._listings[relative] = entries
        return entries

    def find_all(self, pattern: str) -> list[str]:
        """Real paths matching the pattern. A trailing slash only matches directories."""
        want_dir = pattern.endswith("/")
        parts = [p for p in pattern.lower().split("/") if p]
        current = [""]
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            matches: list[str] = []
            for parent in current:
                for name, is_dir in self._list(parent):
                    if not fnmatch.fnmatchcase(name.lower(), part):
                        continue
                    if (not last or want_dir) and not is_dir:
                        continue
                    matches.append(f"{parent}/{name}" if parent else name)
            current = matches
            if not current:
                break
        return current if parts else []

    def find(self, pattern: str) -> str | None:
        found = self.find_all(pattern)
        return found[0] if found else None

    def first(self, patterns: list[str]) -> str | None:
        for pattern in patterns:
            found = self.find(pattern)
            if found:
                return found
        return None

    def read_text(self, relative: str, limit: int = 65536) -> str:
        try:
            with open(self.root / relative, errors="replace") as handle:
                return handle.read(limit)
        except OSError:
            return ""


@dataclass(frozen=True)
class Variant:
    args: str
    requires: tuple[str, ...] = ()
    warning: str = ""


@dataclass(frozen=True)
class Profile:
    name: str
    label: str
    priority: int
    method: str  # linux, windows, debian-netboot or sanboot
    menu_note: str = ""
    requires: tuple[str, ...] = ()
    detect_all: tuple[str, ...] = ()
    detect_any: tuple[str, ...] = ()
    detect_none: tuple[str, ...] = ()
    detect_contains: tuple[tuple[str, str], ...] = ()
    kernel: tuple[str, ...] = ()
    initrd: tuple[str, ...] = ()
    extra_initrd: tuple[str, ...] = ()
    variants: tuple[Variant, ...] = ()
    warning: str = ""

    def matches(self, tree: Tree) -> bool:
        if not all(tree.find(p) for p in self.detect_all):
            return False
        if self.detect_any and not any(tree.find(p) for p in self.detect_any):
            return False
        if any(tree.find(p) for p in self.detect_none):
            return False
        for pattern, needle in self.detect_contains:
            found = tree.find(pattern)
            if not found or needle.lower() not in tree.read_text(found).lower():
                return False
        return True


def parse_profile(data: dict, source: str) -> Profile:
    try:
        name = data["name"]
        boot = data.get("boot", {})
        method = boot.get("method", "linux")
    except (KeyError, AttributeError) as error:
        raise ProfileError(f"{source}: missing setting {error}") from None
    if method not in ("linux", "windows", "debian-netboot", "sanboot"):
        raise ProfileError(f"{source}: unknown boot method {method!r}")

    detect = data.get("detect", {})
    variants = tuple(
        Variant(
            args=v.get("args", ""),
            requires=tuple(v.get("requires", ())),
            warning=v.get("warning", ""),
        )
        for v in boot.get("variant", ())
    )
    if method == "linux" and (not boot.get("kernel") or not boot.get("initrd") or not variants):
        raise ProfileError(f"{source}: linux profiles need kernel, initrd and at least one variant")

    return Profile(
        name=name,
        label=data.get("label", name),
        priority=int(data.get("priority", 50)),
        method=method,
        menu_note=data.get("menu_note", ""),
        requires=tuple(boot.get("requires", ())),
        detect_all=tuple(detect.get("all", ())),
        detect_any=tuple(detect.get("any", ())),
        detect_none=tuple(detect.get("none", ())),
        detect_contains=tuple(detect.get("contains", {}).items()),
        kernel=tuple(boot.get("kernel", ())),
        initrd=tuple(boot.get("initrd", ())),
        extra_initrd=tuple(boot.get("extra_initrd", ())),
        variants=variants,
        warning=data.get("warning", ""),
    )


def load_profiles(*directories: Path) -> list[Profile]:
    """Load profiles from each directory. A later directory replaces profiles of the same name."""
    profiles: dict[str, Profile] = {}
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.toml")):
            try:
                with open(path, "rb") as handle:
                    profile = parse_profile(tomllib.load(handle), str(path))
            except (OSError, tomllib.TOMLDecodeError, ProfileError, TypeError, ValueError) as error:
                log.error("Skipping profile %s: %s", path, error)
                continue
            profiles[profile.name] = profile
    return sorted(profiles.values(), key=lambda p: (-p.priority, p.name))


def identify(tree: Tree, profiles: list[Profile]) -> Profile | None:
    for profile in profiles:
        if profile.matches(tree):
            return profile
    return None


def detect_archs(tree: Tree, filename: str) -> tuple[str, ...]:
    """Architectures an ISO can boot on. Empty means we couldn't tell."""
    found: set[str] = set()
    treeinfo = tree.find(".treeinfo")
    if treeinfo:
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        try:
            parser.read_string(tree.read_text(treeinfo))
            for section in ("general", "tree"):
                arch = ARCH_ALIASES.get(parser.get(section, "arch", fallback="").lower())
                if arch:
                    found.add(arch)
        except configparser.Error:
            pass
    if not found:
        found = {arch for marker, arch in ARCH_MARKERS if tree.find(marker)}
    if not found:
        for match in FILENAME_ARCH.finditer(filename):
            arch = ARCH_ALIASES.get(match.group(1).lower().replace("-", "_"))
            if arch:
                found.add(arch)
    return tuple(sorted(found))


@dataclass(frozen=True)
class BootContext:
    arch: str  # X86_64 or ARM64
    platform: str  # efi or pcbios
    server: str
    http_base: str  # URL of the ISO's extracted files, with no trailing slash
    iso_url: str
    nfs_path: str
    iso_size: int
    mac: str
    features: frozenset[str] = frozenset()
    extra_args: str = ""


@dataclass
class BootPlan:
    kernel: str = ""
    initrds: list[str] = field(default_factory=list)
    args: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str = ""


def ram_gib(iso_size: int) -> str:
    """RAM to load an ISO into memory. A tmpfs is limited to half of RAM by default."""
    return str(math.ceil(iso_size * 2 / 2**30))


def _expand(text: str, tree: Tree, context: BootContext) -> str:
    def glob_match(match: re.Match) -> str:
        return tree.find(_expand(match.group(1), tree, context)) or match.group(1)

    text = GLOB_VARIABLE.sub(glob_match, text)
    values = {
        "{server}": context.server,
        "{http}": context.http_base,
        "{iso_url}": context.iso_url,
        "{nfs_path}": context.nfs_path,
        "{karch}": KERNEL_ARCH[context.arch],
        "{debarch}": DEBIAN_ARCH[context.arch],
        "{mac_hyphen}": context.mac.replace(":", "-"),
        "{iso_gib}": f"{context.iso_size / 2**30:.1f}",
        "{ram_gib}": ram_gib(context.iso_size),
    }
    for key, value in values.items():
        text = text.replace(key, value)
    return text


def build_linux_plan(profile: Profile, tree: Tree, context: BootContext) -> BootPlan:
    plan = BootPlan()
    expanded = lambda patterns: [_expand(p, tree, context) for p in patterns]

    kernel = tree.first(expanded(profile.kernel))
    initrd = tree.first(expanded(profile.initrd))
    if not kernel or not initrd:
        missing = "kernel" if not kernel else "initrd"
        plan.error = f"The {missing} was not where the {profile.name} profile expected it."
        return plan

    variant = next((v for v in profile.variants if set(v.requires) <= context.features), None)
    if variant is None:
        plan.error = f"The {profile.name} profile needs a share that is not available on this server."
        return plan

    plan.kernel = kernel
    # Microcode must come before the main initrd, or the kernel ignores it.
    plan.initrds = [f for p in expanded(profile.extra_initrd) for f in tree.find_all(p)] + [initrd]
    plan.args = " ".join(a for a in (_expand(variant.args, tree, context), context.extra_args) if a)
    if variant.warning:
        plan.warnings.append(_expand(variant.warning, tree, context))
    return plan
