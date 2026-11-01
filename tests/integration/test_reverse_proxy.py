"""Puts the real image behind a real Caddy, and checks what the plan promises about proxies."""

import json
import sys
import time
from pathlib import Path

import pytest

import harness
from harness import MAC, docker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "installer"))

HOST = "vault.test"
COOKIE_JAR = "/tmp/cookies.txt"


@pytest.fixture
def proxied(stack):
    stack.start(ADMIN_PASSWORD="test-admin-password", UI_PUBLIC_URL=f"https://{HOST}")
    caddy = f"iv-it-caddy-{stack.id}"
    stack.extra.append(caddy)
    caddyfile = f"{HOST} {{\n    tls internal\n    reverse_proxy {stack.server_ip}:8081\n}}\n"
    docker(
        "run", "-d", "--name", caddy, "--network", stack.network, "--ip", f"{stack.subnet}.30",
        "--entrypoint", "sh", "caddy:2-alpine", "-c",
        f"printf '%s' '{caddyfile}' > /etc/caddy/Caddyfile; exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile",
    )  # fmt: skip
    stack.caddy_ip = f"{stack.subnet}.30"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if https(stack, "/healthz", check=False).stdout.endswith("200"):
            break
        time.sleep(0.5)
    else:
        raise AssertionError(docker("logs", caddy, check=False).stderr)
    return stack


def https(stack, path, *flags, check=True, origin=None):
    arguments = [
        "-k", "--resolve", f"{HOST}:443:{stack.caddy_ip}", "-b", COOKIE_JAR, "-c", COOKIE_JAR,
        "-w", "\n%{http_code}",
    ]  # fmt: skip
    if origin:
        arguments += ["-H", f"Origin: {origin}"]
    return stack.curl(*arguments, *flags, f"https://{HOST}{path}", check=check)


def log_in(stack):
    return https(
        stack, "/api/login", "-X", "POST", "-H", "Content-Type: application/json",
        "-d", json.dumps({"password": "test-admin-password"}), "-i", origin=f"https://{HOST}",
    )  # fmt: skip


def test_login_through_caddy_sets_a_secure_cookie(proxied):
    result = log_in(proxied)
    assert result.stdout.endswith("200")
    cookie = next(l for l in result.stdout.splitlines() if l.lower().startswith("set-cookie"))
    assert "secure" in cookie.lower()
    assert "httponly" in cookie.lower()


def test_pinning_through_caddy_passes_the_origin_check(proxied):
    isos = proxied.wait_for_all_isos()
    log_in(proxied)
    body = json.dumps({"iso_id": isos["alpine-test.iso"]["id"], "mode": "once"})
    result = https(
        proxied, f"/api/pins/{MAC}", "-X", "PUT", "-H", "Content-Type: application/json", "-d", body,
        origin=f"https://{HOST}",
    )  # fmt: skip
    assert result.stdout.endswith("200"), result.stdout
    assert proxied.api("/api/clients")[1]["pins"][MAC]["mode"] == "once"
    assert "pinned to alpine-test.iso" in proxied.fetch_menu()


def test_another_sites_request_is_refused_even_with_a_session(proxied):
    isos = proxied.wait_for_all_isos()
    log_in(proxied)
    body = json.dumps({"iso_id": isos["alpine-test.iso"]["id"], "mode": "once"})
    result = https(
        proxied, f"/api/pins/{MAC}", "-X", "PUT", "-H", "Content-Type: application/json", "-d", body,
        origin="https://evil.example",
    )  # fmt: skip
    assert result.stdout.endswith("403")
    assert proxied.api("/api/clients")[1]["pins"] == {}


def test_logged_out_visitors_are_sent_to_the_login_page_on_the_public_address(proxied):
    result = https(proxied, "/", "-o", "/dev/null", "-w", "%{redirect_url}")
    assert result.stdout.startswith(f"https://{HOST}/login")


def test_dashboard_loads_through_caddy(proxied):
    log_in(proxied)
    result = https(proxied, "/api/dashboard")
    assert result.stdout.endswith("200")
    assert '"dhcp_mode"' in result.stdout


def test_direct_port_still_works_with_an_https_public_url(proxied):
    """If the proxy isn't set up yet, the direct address must not be a dead end."""
    direct = f"http://{proxied.server_ip}:8081"
    result = proxied.curl(
        "-i", "-X", "POST", "-H", "Content-Type: application/json", "-H", f"Origin: {direct}",
        "-d", json.dumps({"password": "test-admin-password"}), f"{direct}/api/login",
    )  # fmt: skip
    assert " 200" in result.stdout.splitlines()[0]
    cookie = next(l for l in result.stdout.splitlines() if l.lower().startswith("set-cookie"))
    assert "secure" not in cookie.lower(), "A browser would drop a Secure cookie sent over plain HTTP"
    redirect = proxied.curl("-o", "/dev/null", "-w", "%{redirect_url}", f"{direct}/").stdout
    assert redirect.startswith(direct), redirect


def ui_health(stack):
    return stack.curl(
        "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "3", f"http://{stack.server_ip}:8081/healthz",
        check=False,
    )  # fmt: skip


def test_booting_keeps_working_while_the_ui_is_down(proxied):
    isos = proxied.wait_for_all_isos()
    iso_id = isos["alpine-test.iso"]["id"]
    # Frozen rather than killed, because the supervisor would restart it straight away.
    proxied.exec_server("sh", "-c", "kill -STOP $(pidof node)")
    try:
        assert ui_health(proxied).stdout != "200", "The UI should be down for this test to mean anything"
        assert "alpine-test.iso" in proxied.fetch_menu()
        assert "kernel " in proxied.fetch_boot(iso_id)
        assert proxied.http_status(f"/files/{iso_id}/boot/vmlinuz-virt") == "200"
    finally:
        proxied.exec_server("sh", "-c", "kill -CONT $(pidof node)")
    assert ui_health(proxied).stdout == "200"


def test_a_crashed_ui_is_restarted(proxied):
    proxied.exec_server("sh", "-c", "kill -KILL $(pidof node)")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        web = proxied.status()["services"]["web"]
        if web["restarts"] >= 1 and web["running"] and ui_health(proxied).stdout == "200":
            return
        time.sleep(0.5)
    raise AssertionError(f"The UI never came back: {proxied.status()['services']['web']}")


def test_boot_traffic_bypasses_the_proxy(proxied):
    """Scripts must point at the LAN IP and boot port, whatever UI_PUBLIC_URL says."""
    isos = proxied.wait_for_all_isos()
    script = proxied.fetch_boot(isos["alpine-test.iso"]["id"])
    assert f"http://{proxied.server_ip}:8080/files/" in script
    assert HOST not in script
    boot = proxied.curl(f"tftp://{proxied.server_ip}/boot.ipxe").stdout
    assert f"http://{proxied.server_ip}:8080/ipxe/menu" in boot and HOST not in boot


def render_snippet(tmp_path, process, *extra):
    from test_install import Installer

    installer = Installer(tmp_path)
    result = installer.run("--yes", "--proxy-host", "vault.example.com", *extra, FAKE_LISTENERS=f"443:{process}")
    assert result.returncode == 0, result.stderr
    return installer.home / "reverse-proxy"


@pytest.mark.parametrize("hostname_flag", ["vault.example.com", "vault.home.arpa"])
def test_generated_caddy_snippet_passes_caddy_validate(tmp_path, hostname_flag):
    from test_install import Installer

    installer = Installer(tmp_path)
    result = installer.run("--yes", "--proxy-host", hostname_flag, FAKE_LISTENERS="443:caddy")
    assert result.returncode == 0, result.stderr
    text = (installer.home / "reverse-proxy" / "Caddyfile").read_text()
    check = docker(
        "run", "--rm", "-i", "--entrypoint", "sh", "caddy:2-alpine", "-c",
        "cat > /tmp/Caddyfile; caddy validate --config /tmp/Caddyfile --adapter caddyfile",
        input_text=text, check=False,
    )  # fmt: skip
    assert check.returncode == 0, check.stdout + check.stderr


def test_generated_nginx_snippet_passes_nginx_t(tmp_path):
    text = (render_snippet(tmp_path, "nginx") / "nginx.conf").read_text()
    wrapper = "events {}\nhttp {\n" + text + "\n}\n"
    check = docker(
        "run", "--rm", "-i", "--entrypoint", "sh", "nginx:alpine", "-c",
        "cat > /tmp/test.conf; nginx -t -c /tmp/test.conf",
        input_text=wrapper, check=False,
    )  # fmt: skip
    assert check.returncode == 0, check.stdout + check.stderr
