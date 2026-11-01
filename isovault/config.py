"""Settings, read once from the environment at startup."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

DHCP_MODES = ("proxy", "server", "upstream")
ISO_ACCESS_MODES = ("auto", "mount", "extract")
TOGGLES = ("auto", "true", "false")


class ConfigError(ValueError):
    """A setting has a value we can't use. The message is shown to the user."""


@dataclass(frozen=True)
class Config:
    dhcp_mode: str = "proxy"
    dhcp_force: bool = False
    dhcp_range_start: str = ""
    dhcp_range_end: str = ""
    dhcp_gateway: str = ""
    dhcp_dns: str = ""
    dhcp_lease_time: str = "12h"
    interface: str = "auto"
    server_ip: str = "auto"
    boot_port: int = 8080
    ui_port: int = 8081
    ui_bind: str = "0.0.0.0"
    ui_public_url: str = ""
    iso_access: str = "auto"
    enable_nfs: str = "auto"
    enable_smb: str = "auto"
    menu_timeout: int = 30
    default_iso: str = ""
    extra_kernel_args: str = ""
    scan_interval: float = 5.0
    stable_seconds: float = 10.0
    iso_dir: Path = Path("/isos")
    data_dir: Path = Path("/data")
    run_dir: Path = Path("/run/isovault")
    app_dir: Path = Path("/opt/isovault")

    @property
    def core_socket(self) -> Path:
        return self.run_dir / "core.sock"

    @property
    def tftp_root(self) -> Path:
        return self.run_dir / "tftp"

    @property
    def mnt_dir(self) -> Path:
        return self.run_dir / "mnt"

    @property
    def raw_dir(self) -> Path:
        return self.run_dir / "raw"

    @property
    def netboot_dir(self) -> Path:
        return self.run_dir / "netboot"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def templates_dir(self) -> Path:
        return self.app_dir / "templates"

    @property
    def loaders_dir(self) -> Path:
        return self.app_dir / "loaders"

    @property
    def web_dir(self) -> Path:
        return self.app_dir / "web"


def _choice(env: Mapping[str, str], name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = env.get(name, "").strip().lower() or default
    if value not in allowed:
        raise ConfigError(f"{name}={value!r} isn't valid. Use one of: {', '.join(allowed)}.")
    return value


def _flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _integer(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} isn't a whole number.") from None
    if not low <= value <= high:
        raise ConfigError(f"{name}={value} must be between {low} and {high}.")
    return value


def load(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    text = lambda name, default="": env.get(name, "").strip() or default
    config = Config(
        dhcp_mode=_choice(env, "DHCP_MODE", "proxy", DHCP_MODES),
        dhcp_force=_flag(env, "DHCP_FORCE"),
        dhcp_range_start=text("DHCP_RANGE_START"),
        dhcp_range_end=text("DHCP_RANGE_END"),
        dhcp_gateway=text("DHCP_GATEWAY"),
        dhcp_dns=text("DHCP_DNS"),
        dhcp_lease_time=text("DHCP_LEASE_TIME", "12h"),
        interface=text("INTERFACE", "auto"),
        server_ip=text("SERVER_IP", "auto"),
        boot_port=_integer(env, "BOOT_PORT", 8080, 1, 65535),
        ui_port=_integer(env, "UI_PORT", 8081, 1, 65535),
        ui_bind=text("UI_BIND", "0.0.0.0"),
        ui_public_url=text("UI_PUBLIC_URL").rstrip("/"),
        iso_access=_choice(env, "ISO_ACCESS", "auto", ISO_ACCESS_MODES),
        enable_nfs=_choice(env, "ENABLE_NFS", "auto", TOGGLES),
        enable_smb=_choice(env, "ENABLE_SMB", "auto", TOGGLES),
        menu_timeout=_integer(env, "MENU_TIMEOUT", 30, 0, 86400),
        default_iso=text("DEFAULT_ISO"),
        extra_kernel_args=text("EXTRA_KERNEL_ARGS"),
        stable_seconds=float(_integer(env, "STABLE_SECONDS", 10, 0, 3600)),
    )
    if config.boot_port == config.ui_port:
        raise ConfigError(f"BOOT_PORT and UI_PORT are both {config.boot_port}. They must differ.")
    return config


@dataclass(frozen=True)
class Secrets:
    admin_password: str
    session_secret: str
    smb_password: str


def load_secrets(data_dir: Path, env: Mapping[str, str] | None = None) -> Secrets:
    """Use values from the environment, then saved values, then generate new ones."""
    env = os.environ if env is None else env
    path = data_dir / "secrets.json"
    saved: dict[str, str] = {}
    if path.exists():
        try:
            saved = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            saved = {}

    generators = {
        "ADMIN_PASSWORD": lambda: secrets.token_urlsafe(18),
        "SESSION_SECRET": lambda: secrets.token_hex(32),
        # Hex only, because this ends up on a Windows batch file's command line.
        "SMB_PASSWORD": lambda: secrets.token_hex(16),
    }
    values: dict[str, str] = {}
    changed = False
    for name, generate in generators.items():
        value = env.get(name, "").strip() or saved.get(name, "")
        if not value:
            value = generate()
        if saved.get(name) != value:
            changed = True
        values[name] = value

    if not values["SMB_PASSWORD"].isalnum():
        raise ConfigError("SMB_PASSWORD may only contain letters and digits.")

    if changed:
        data_dir.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(values, handle, indent=2)
        os.replace(temp, path)

    return Secrets(values["ADMIN_PASSWORD"], values["SESSION_SECRET"], values["SMB_PASSWORD"])
