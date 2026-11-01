"""Runs install.sh against stubbed system commands, so nothing on the machine is touched."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# A fake docker that records its calls. `FAKE_*` variables set what it reports.
DOCKER_STUB = r"""#!/bin/sh
echo "docker $*" >> "$CALL_LOG"
case "$1 $2" in
  "info "*) [ -n "$FAKE_DOCKER_DOWN" ] && exit 1; echo "${FAKE_DOCKER_OS:-Debian GNU/Linux}"; exit 0 ;;
  "compose version") exit 0 ;;
  "compose pull") [ -n "$FAKE_PULL_FAILS" ] && exit 1; exit 0 ;;
  "compose build") exit 0 ;;
  "compose up") exit 0 ;;
  "ps "*) [ -n "$FAKE_PROXY_CONTAINER" ] && echo "$FAKE_PROXY_CONTAINER"; exit 0 ;;
  "inspect "*) echo "${FAKE_HEALTH:-healthy}"; exit 0 ;;
  "logs "*) exit 0 ;;
  "exec "*) echo "from-secrets-file"; exit 0 ;;
esac
case "$*" in
  *probe-dhcp*) [ -n "$FAKE_PROBE_FAILS" ] && exit 1; echo "{\"interface\": \"eth0\", \"ip\": \"192.168.1.10\", \"servers\": [${FAKE_DHCP_SERVERS:-}]}" ;;
esac
exit 0
"""

IP_STUB = r"""#!/bin/sh
case "$*" in
  *"route show default"*) echo "default via 192.168.1.1 dev eth0 proto dhcp" ;;
  *"addr show dev eth0"*) echo "2: eth0    inet 192.168.1.10/24 brd 192.168.1.255 scope global eth0" ;;
  *"addr show dev "*) ;;
  *"addr show scope global"*)
    echo "2: eth0    inet 192.168.1.10/24 brd 192.168.1.255 scope global eth0"
    echo "3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0"
    ;;
esac
"""

SS_STUB = r"""#!/bin/sh
# FAKE_LISTENERS holds lines of "port:process".
for entry in $FAKE_LISTENERS; do
  port=${entry%%:*}
  name=${entry#*:}
  case "$*" in
    *":$port"*) echo "LISTEN 0 4096 *:$port *:* users:((\"$name\",pid=1,fd=3))" ;;
  esac
done
"""

SYSTEMCTL_STUB = r"""#!/bin/sh
echo "systemctl $*" >> "$CALL_LOG"
case "$1" in
  is-active) for unit in $FAKE_ACTIVE_UNITS; do [ "$unit" = "$3" ] && exit 0; done; exit 3 ;;
  reload) [ -n "$FAKE_RELOAD_FAILS" ] && exit 1; exit 0 ;;
esac
"""

CADDY_STUB = r"""#!/bin/sh
echo "caddy $*" >> "$CALL_LOG"
case "$1" in
  validate) [ -n "$FAKE_VALIDATE_FAILS" ] && exit 1; exit 0 ;;
esac
"""

FAIL_STUB = '#!/bin/sh\necho "$0 $*" >> "$CALL_LOG"\nexit 1\n'


def write_stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class Installer:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "install"
        self.home.mkdir()
        # A checkout-like folder, so the installer uses the local templates and compose file.
        (self.home / "compose.yml").write_text("services: {}\n")
        (self.home / "Dockerfile").write_text("FROM scratch\n")
        templates = self.home / "templates" / "reverse-proxy"
        templates.mkdir(parents=True)
        for source in (ROOT / "templates" / "reverse-proxy").iterdir():
            (templates / source.name).write_text(source.read_text())

        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.log = tmp_path / "calls.log"
        self.log.touch()
        self.caddyfile = tmp_path / "Caddyfile"
        for name, body in {
            "docker": DOCKER_STUB,
            "ip": IP_STUB,
            "ss": SS_STUB,
            "systemctl": SYSTEMCTL_STUB,
            "curl": FAIL_STUB,
            "ufw": FAIL_STUB,
            "firewall-cmd": FAIL_STUB,
            "sudo": '#!/bin/sh\necho "sudo $*" >> "$CALL_LOG"\nexec "$@"\n',
        }.items():
            write_stub(self.bin, name, body)

    def add_caddy(self) -> None:
        write_stub(self.bin, "caddy", CADDY_STUB)
        self.caddyfile.write_text("example.com {\n    respond \"hello\"\n}\n")

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        environment = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "CALL_LOG": str(self.log),
            "ISO_VAULT_ALLOW_ANY_OS": "true",
            "ISO_VAULT_CADDYFILE": str(self.caddyfile),
            "HOME": str(self.home),
            **env,
        }
        return subprocess.run(
            ["bash", str(ROOT / "install.sh"), *args],
            cwd=self.home,
            env=environment,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # no controlling terminal, as with cron or CI
            timeout=60,
        )

    def env(self) -> dict[str, str]:
        lines = (self.home / ".env").read_text().splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line)

    def calls(self) -> str:
        return self.log.read_text()


@pytest.fixture
def installer(tmp_path):
    return Installer(tmp_path)


DHCP_FOUND = {"FAKE_DHCP_SERVERS": '"192.168.1.1"'}


def test_finds_a_dhcp_server_and_picks_proxy_mode(installer):
    result = installer.run("--yes", **DHCP_FOUND)
    assert result.returncode == 0, result.stderr
    assert installer.env()["DHCP_MODE"] == "proxy"
    assert "192.168.1.1" in result.stdout


def test_unattended_install_never_turns_on_dhcp_by_itself(installer):
    result = installer.run("--yes")
    assert result.returncode == 0, result.stderr
    assert installer.env()["DHCP_MODE"] == "proxy"
    assert "Use --dhcp-mode server" in result.stderr


def test_no_terminal_never_turns_on_dhcp_by_itself(installer):
    result = installer.run()
    assert result.returncode == 0, result.stderr
    assert installer.env()["DHCP_MODE"] == "proxy"


def test_a_failed_probe_falls_back_to_proxy(installer):
    result = installer.run("--yes", FAKE_PROBE_FAILS="1")
    assert result.returncode == 0, result.stderr
    assert installer.env()["DHCP_MODE"] == "proxy"


@pytest.mark.parametrize("mode", ["server", "upstream", "proxy"])
def test_an_explicit_mode_is_respected_without_probing(installer, mode):
    result = installer.run("--yes", "--dhcp-mode", mode, **DHCP_FOUND)
    assert result.returncode == 0, result.stderr
    assert installer.env()["DHCP_MODE"] == mode
    assert "probe-dhcp" not in installer.calls()


def test_upstream_mode_prints_the_router_settings(installer):
    result = installer.run("--yes", "--dhcp-mode", "upstream")
    assert "192.168.1.10" in result.stdout
    for filename in ("undionly.kpxe", "snponly-x64.efi", "snponly-arm64.efi"):
        assert filename in result.stdout


def test_rejects_an_invalid_mode(installer):
    assert installer.run("--dhcp-mode", "maybe").returncode != 0


def test_rerunning_keeps_settings_and_passwords(installer):
    installer.run("--yes", "--dhcp-mode", "upstream")
    first = installer.env()
    result = installer.run("--yes", **DHCP_FOUND)
    assert result.returncode == 0, result.stderr
    second = installer.env()
    assert second["DHCP_MODE"] == "upstream"
    for name in ("ADMIN_PASSWORD", "SESSION_SECRET", "SMB_PASSWORD", "BOOT_PORT", "UI_PORT"):
        assert second[name] == first[name]


def test_env_file_is_private_and_smb_password_is_safe(installer):
    installer.run("--yes")
    env_path = installer.home / ".env"
    assert env_path.stat().st_mode & 0o077 == 0
    assert installer.env()["SMB_PASSWORD"].isalnum()


def test_summary_shows_how_to_reach_the_ui(installer):
    result = installer.run("--yes")
    assert "http://192.168.1.10:8081" in result.stdout
    assert installer.env()["ADMIN_PASSWORD"] in result.stdout


def test_builds_locally_when_the_image_isnt_published(installer):
    result = installer.run("--yes", FAKE_PULL_FAILS="1")
    assert result.returncode == 0, result.stderr
    assert "docker compose build" in installer.calls()


def test_stops_if_the_image_cant_be_pulled_or_built(installer):
    (installer.home / "Dockerfile").unlink()
    (installer.home / "compose.yml").write_text("services: {}\n")
    result = installer.run("--yes", "--dir", ".", FAKE_PULL_FAILS="1")
    assert result.returncode != 0
    assert "Couldn't download" in result.stderr


def test_reports_a_container_that_wont_start(installer):
    result = installer.run("--yes", FAKE_HEALTH="unhealthy")
    assert result.returncode != 0
    assert "didn't start" in result.stderr


def test_refuses_docker_desktop(installer):
    result = installer.run("--yes", ISO_VAULT_ALLOW_ANY_OS="false", FAKE_DOCKER_OS="Docker Desktop")
    assert result.returncode != 0


def test_picks_other_ports_when_the_defaults_are_taken(installer):
    result = installer.run("--yes", FAKE_LISTENERS="8080:apache2 8081:grafana 8082:other")
    assert result.returncode == 0, result.stderr
    env = installer.env()
    assert env["BOOT_PORT"] == "8083"
    assert env["UI_PORT"] == "8084"
    assert "http://192.168.1.10:8084" in result.stdout


def test_no_proxy_found_leaves_the_ui_direct(installer):
    result = installer.run("--yes")
    assert "UI_PUBLIC_URL" not in installer.env()
    assert not list((installer.home / "reverse-proxy").iterdir())
    assert "http://192.168.1.10:8081" in result.stdout


def test_a_hostname_with_shell_characters_is_refused(installer):
    assert installer.run("--yes", "--proxy-host", "a;rm -rf /").returncode != 0
    assert installer.run("--yes", "--proxy-host", "$(reboot)").returncode != 0


# --- Each row of the reverse proxy table ------------------------------------------------------


def test_caddy_on_host_is_applied_with_explicit_permission(installer):
    installer.add_caddy()
    original = installer.caddyfile.read_text()
    result = installer.run(
        "--yes", "--proxy-host", "vault.example.com", "--apply-proxy",
        FAKE_LISTENERS="443:caddy", FAKE_ACTIVE_UNITS="caddy",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    site = installer.caddyfile.parent / "iso-vault.caddy"
    assert "vault.example.com {" in site.read_text()
    assert "reverse_proxy 127.0.0.1:8081" in site.read_text()
    assert installer.caddyfile.read_text().startswith(original)
    assert f"import {site}" in installer.caddyfile.read_text()
    assert "systemctl reload caddy" in installer.calls()
    assert not (installer.caddyfile.parent / "Caddyfile.iso-vault-backup").exists()
    env = installer.env()
    assert env["UI_PUBLIC_URL"] == "https://vault.example.com"
    assert env["UI_BIND"] == "127.0.0.1"
    assert "https://vault.example.com" in result.stdout


def test_caddy_is_left_alone_without_permission(installer):
    installer.add_caddy()
    original = installer.caddyfile.read_text()
    result = installer.run("--yes", "--proxy-host", "vault.example.com", FAKE_LISTENERS="443:caddy")
    assert result.returncode == 0, result.stderr
    assert installer.caddyfile.read_text() == original
    assert "caddy validate" not in installer.calls()
    assert (installer.home / "reverse-proxy" / "Caddyfile").exists()
    assert installer.env().get("UI_BIND") != "127.0.0.1"
    assert "http://192.168.1.10:8081" in result.stdout


def test_a_failed_validation_restores_caddy_and_keeps_the_ui_reachable(installer):
    installer.add_caddy()
    original = installer.caddyfile.read_text()
    result = installer.run(
        "--yes", "--proxy-host", "vault.example.com", "--apply-proxy",
        FAKE_LISTENERS="443:caddy", FAKE_VALIDATE_FAILS="1",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert installer.caddyfile.read_text() == original
    assert not (installer.caddyfile.parent / "iso-vault.caddy").exists()
    assert not (installer.caddyfile.parent / "Caddyfile.iso-vault-backup").exists()
    assert "restored" in result.stderr
    assert installer.env().get("UI_BIND") != "127.0.0.1"
    assert "http://192.168.1.10:8081" in result.stdout


def test_a_failed_reload_restores_caddy(installer):
    installer.add_caddy()
    original = installer.caddyfile.read_text()
    result = installer.run(
        "--yes", "--proxy-host", "vault.example.com", "--apply-proxy",
        FAKE_LISTENERS="443:caddy", FAKE_ACTIVE_UNITS="caddy", FAKE_RELOAD_FAILS="1",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert installer.caddyfile.read_text() == original
    assert not (installer.caddyfile.parent / "iso-vault.caddy").exists()
    assert installer.env().get("UI_BIND") != "127.0.0.1"


def test_applying_twice_adds_one_import_line(installer):
    installer.add_caddy()
    arguments = ("--yes", "--proxy-host", "vault.example.com", "--apply-proxy")
    installer.run(*arguments, FAKE_LISTENERS="443:caddy")
    installer.run(*arguments, FAKE_LISTENERS="443:caddy")
    assert installer.caddyfile.read_text().count("import ") == 1


def test_local_names_get_an_internal_certificate(installer):
    installer.add_caddy()
    installer.run("--yes", "--proxy-host", "vault.home.arpa", FAKE_LISTENERS="443:caddy")
    text = (installer.home / "reverse-proxy" / "Caddyfile").read_text()
    assert "    tls internal\n    reverse_proxy 127.0.0.1:8081\n" in text


def test_public_names_dont_get_an_internal_certificate(installer):
    installer.add_caddy()
    installer.run("--yes", "--proxy-host", "vault.example.com", FAKE_LISTENERS="443:caddy")
    text = (installer.home / "reverse-proxy" / "Caddyfile").read_text()
    assert "tls internal" not in text
    assert "@@" not in text


def test_caddy_in_docker_gets_a_snippet_using_the_lan_ip(installer):
    result = installer.run(
        "--yes", "--proxy-host", "vault.example.com", FAKE_PROXY_CONTAINER="web-caddy caddy:2-alpine"
    )
    assert result.returncode == 0, result.stderr
    text = (installer.home / "reverse-proxy" / "Caddyfile").read_text()
    assert "reverse_proxy 192.168.1.10:8081" in text
    assert installer.env().get("UI_BIND") != "127.0.0.1"
    assert "caddy validate" not in installer.calls()
    assert "reverse-proxy/Caddyfile" in result.stdout


@pytest.mark.parametrize(
    "process, filename, needle",
    [
        ("nginx", "nginx.conf", "server_name vault.example.com;"),
        ("traefik", "traefik.yml", "Host(`vault.example.com`)"),
        ("apache2", "apache.conf", "ServerName vault.example.com"),
        ("httpd", "apache.conf", "ProxyPass / http://127.0.0.1:8081/"),
        ("haproxy", "haproxy.conf", "server iso_vault 127.0.0.1:8081 check"),
    ],
)
def test_other_proxies_get_a_ready_snippet(installer, process, filename, needle):
    result = installer.run("--yes", "--proxy-host", "vault.example.com", FAKE_LISTENERS=f"443:{process}")
    assert result.returncode == 0, result.stderr
    text = (installer.home / "reverse-proxy" / filename).read_text()
    assert needle in text
    assert "@@" not in text
    assert installer.env().get("UI_BIND") != "127.0.0.1"
    assert filename in result.stdout


def test_proxy_found_through_systemd_when_the_process_name_is_hidden(installer):
    result = installer.run("--yes", "--proxy-host", "vault.example.com", FAKE_ACTIVE_UNITS="nginx")
    assert result.returncode == 0, result.stderr
    assert (installer.home / "reverse-proxy" / "nginx.conf").exists()


def test_no_proxy_flag_skips_everything(installer):
    installer.add_caddy()
    result = installer.run("--yes", "--no-proxy", FAKE_LISTENERS="443:caddy")
    assert result.returncode == 0, result.stderr
    assert "UI_PUBLIC_URL" not in installer.env()
    assert not list((installer.home / "reverse-proxy").iterdir())


def test_without_a_hostname_a_found_proxy_is_left_alone(installer):
    installer.add_caddy()
    original = installer.caddyfile.read_text()
    result = installer.run("--yes", FAKE_LISTENERS="443:caddy")
    assert result.returncode == 0, result.stderr
    assert installer.caddyfile.read_text() == original
    assert "UI_PUBLIC_URL" not in installer.env()
