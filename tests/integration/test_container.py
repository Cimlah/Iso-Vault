"""Runs the real image, and checks it from a separate device on the same network."""

import time

import pytest

import harness
from harness import MAC, docker, on_volume


@pytest.mark.parametrize("mode", ["server", "proxy", "upstream"])
def test_each_dhcp_mode_starts_with_a_valid_config(stack, mode):
    stack.start(DHCP_MODE=mode)
    assert stack.status()["dhcp_mode"] == mode
    assert stack.exec_server("dnsmasq", "--test", "--conf-file=/run/isovault/dnsmasq.conf", check=False).returncode == 0
    services = stack.status()["services"]
    assert services["dnsmasq"]["running"] and services["nginx"]["running"]
    assert stack.exec_server("python3", "-m", "isovault", "healthcheck", check=False).returncode == 0


def test_invalid_settings_stop_the_container_with_a_clear_message(stack):
    stack.start(DHCP_MODE="sometimes", wait=False)
    time.sleep(2)
    assert docker("inspect", "-f", "{{.State.Running}}", stack.server).stdout.strip() == "false"
    assert "DHCP_MODE" in stack.logs()


def test_rogue_dhcp_guard_blocks_server_mode(stack):
    router = f"iv-it-router-{stack.id}"
    stack.extra.append(router)
    docker(
        "run", "-d", "--name", router, "--network", stack.network, "--ip", f"{stack.subnet}.2",
        "--cap-add", "NET_ADMIN", "--entrypoint", "dnsmasq", harness.IMAGE,
        "--keep-in-foreground", "--port=0", "--conf-file=/dev/null",
        f"--dhcp-range={stack.subnet}.100,{stack.subnet}.200,255.255.255.0,10m",
    )  # fmt: skip
    time.sleep(1)
    stack.start(DHCP_MODE="server")
    status = stack.status()
    assert status["configured_dhcp_mode"] == "server"
    assert status["dhcp_mode"] == "proxy"
    assert any(f"{stack.subnet}.2" in warning for warning in status["warnings"])
    config = stack.exec_server("cat", "/run/isovault/dnsmasq.conf").stdout
    assert ",proxy," in config
    assert f"{stack.subnet}.100" not in config


def test_dhcp_force_overrides_the_guard(stack):
    router = f"iv-it-router-{stack.id}"
    stack.extra.append(router)
    docker(
        "run", "-d", "--name", router, "--network", stack.network, "--ip", f"{stack.subnet}.2",
        "--cap-add", "NET_ADMIN", "--entrypoint", "dnsmasq", harness.IMAGE,
        "--keep-in-foreground", "--port=0", "--conf-file=/dev/null",
        f"--dhcp-range={stack.subnet}.100,{stack.subnet}.200,255.255.255.0,10m",
    )  # fmt: skip
    time.sleep(1)
    stack.start(DHCP_MODE="server", DHCP_FORCE="true")
    assert stack.status()["dhcp_mode"] == "server"


def test_server_mode_starts_when_the_network_is_quiet(stack):
    stack.start(DHCP_MODE="server")
    status = stack.status()
    assert status["dhcp_mode"] == "server"
    assert status["warnings"] == []


def test_iso_families_and_architectures_are_identified(stack):
    stack.start()
    isos = stack.wait_for_all_isos()
    assert isos["alpine-test.iso"]["profile"] == "alpine"
    assert isos["alpine-test.iso"]["archs"] == ["x86_64"]
    assert isos["alpine-test.iso"]["method"] == "mount"
    assert isos["ubuntu-desktop.iso"]["profile"] == "ubuntu-casper"
    assert isos["windows-test.iso"]["profile"] == "windows"
    assert isos["memtest-tool.iso"]["profile"] == "generic"
    assert isos["memtest-tool.iso"]["warnings"]
    assert isos["arm/alpine-arm.iso"]["archs"] == ["arm64"]
    assert isos["arm/alpine-arm.iso"]["group"] == "arm"


def test_menu_filters_by_architecture_and_groups_subfolders(stack):
    stack.start()
    stack.wait_for_all_isos()
    x64 = stack.fetch_menu(arch="x86_64")
    arm = stack.fetch_menu(arch="arm64")
    assert "alpine-test.iso" in x64 and "alpine-arm.iso" not in x64
    assert "alpine-arm.iso" in arm and "alpine-test.iso" not in arm
    assert "item --gap -- arm/" in arm
    # The generic ISO has no known architecture, so every client sees it.
    assert "memtest-tool.iso (may not finish booting)" in x64 and "memtest-tool.iso" in arm
    assert "hidden because" in x64


def test_a_bios_client_is_treated_as_x64(stack):
    stack.start()
    stack.wait_for_all_isos()
    assert "alpine-test.iso" in stack.fetch_menu(arch="i386", platform="pcbios")


def test_boot_scripts_point_at_files_that_really_exist(stack):
    stack.start()
    isos = stack.wait_for_all_isos()
    script = stack.fetch_boot(isos["alpine-test.iso"]["id"])
    urls = [word for word in script.split() if word.startswith("http")]
    assert len(urls) == 2
    for url in urls:
        assert stack.curl("-o", "/dev/null", "-w", "%{http_code}", url).stdout == "200"
    modloop = next(word for word in script.split() if word.startswith("modloop="))
    assert stack.curl("-o", "/dev/null", "-w", "%{http_code}", modloop[len("modloop="):]).stdout == "200"


def test_loaders_download_over_tftp_and_http(stack):
    stack.start()
    boot = stack.curl(f"tftp://{stack.server_ip}/boot.ipxe").stdout
    assert boot.startswith("#!ipxe")
    assert f"http://{stack.server_ip}:8080/ipxe/menu" in boot
    assert "@@" not in boot
    for loader in ("undionly.kpxe", "snponly-x64.efi", "snponly-arm64.efi", "ipxe-x64.efi", "ipxe-arm64.efi", "wimboot", "wimboot.arm64"):
        size = stack.curl("-o", "/dev/null", "-w", "%{size_download}", stack.boot_url(f"/loaders/{loader}")).stdout
        assert int(size) > 10000, loader
    tftp = stack.exec_helper("sh", "-c", f"curl -s tftp://{stack.server_ip}/snponly-arm64.efi | sha256sum").stdout
    http = stack.exec_helper("sh", "-c", f"curl -s {stack.boot_url('/loaders/snponly-arm64.efi')} | sha256sum").stdout
    assert tftp == http


def test_range_requests_work_on_extracted_and_raw_files(stack):
    stack.start()
    isos = stack.wait_for_all_isos()
    iso_id = isos["alpine-test.iso"]["id"]
    for path in (f"/raw/{iso_id}.iso", f"/files/{iso_id}/boot/vmlinuz-virt"):
        result = stack.curl("-r", "0-3", "-o", "/dev/null", "-w", "%{http_code} %{size_download}", stack.boot_url(path)).stdout
        assert result == "206 4", path


def test_core_api_and_secrets_cant_be_reached_from_the_network(stack):
    stack.start()
    stack.wait_for_all_isos()
    iso_id = stack.isos_by_name()["alpine-test.iso"]["id"]
    assert stack.http_status("/api/status") == "404"
    assert stack.http_status(f"/api/pins/{MAC}", "-X", "PUT", "-d", "{}") == "404"
    assert stack.http_status("/ipxe/../api/status") in ("400", "404")
    for path in (
        "/secrets.json",
        "/files/../secrets.json",
        "/files/../../../data/secrets.json",
        f"/files/{iso_id}/../../../../data/secrets.json",
        "/raw/../../../data/secrets.json",
        "/loaders/../core.sock",
        "/loaders../core.sock",
        "/netboot/../../data/secrets.json",
    ):
        status = stack.http_status(path)
        assert status in ("400", "403", "404"), f"{path} returned {status}"


def test_web_ui_needs_a_login_and_core_socket_is_private(stack):
    stack.start()
    ui = f"http://{stack.server_ip}:8081"
    assert stack.curl("-o", "/dev/null", "-w", "%{http_code}", f"{ui}/api/dashboard").stdout == "401"
    assert stack.curl("-o", "/dev/null", "-w", "%{http_code}", f"{ui}/healthz").stdout == "200"
    assert stack.curl("-o", "/dev/null", "-w", "%{redirect_url}", f"{ui}/").stdout.endswith("/login")
    mode = stack.exec_server("stat", "-c", "%a", "/run/isovault/core.sock").stdout.strip()
    assert mode == "660"


def test_services_dont_inherit_secrets(stack):
    stack.start(ADMIN_PASSWORD="env-admin-password")
    script = "for name in dnsmasq nginx smbd unfsd rpcbind; do for p in $(pidof $name); do cat /proc/$p/environ | tr '\\0' '\\n' | grep -E '^(ADMIN_PASSWORD|SESSION_SECRET|SMB_PASSWORD)=' && echo LEAK $name; done; done; true"
    assert "LEAK" not in stack.exec_server("sh", "-c", script).stdout
    assert "env-admin-password" not in stack.logs()
    # The web process needs the login secrets, but it has no use for the Samba password.
    web = stack.exec_server("sh", "-c", "cat /proc/$(pidof node)/environ | tr '\\0' '\\n'").stdout
    assert "ADMIN_PASSWORD=env-admin-password" in web
    assert "SMB_PASSWORD" not in web


def test_web_and_nginx_workers_dont_run_as_root(stack):
    stack.start()
    workers = stack.exec_server("sh", "-c", "ps -o user,args | grep -E 'nginx: worker|next-server' | grep -v grep").stdout
    assert "nginx: worker" in workers and "next-server" in workers
    for line in workers.strip().splitlines():
        assert not line.split()[0].startswith("root"), line
    # Dropping the user isn't enough if the process keeps root's group.
    status = stack.exec_server("sh", "-c", "cat /proc/$(pidof node)/status").stdout
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    assert "0" not in fields["Uid"].split()
    assert "0" not in fields["Gid"].split()
    assert "0" not in fields["Groups"].split()


def test_pins_survive_a_restart(stack):
    stack.start()
    isos = stack.wait_for_all_isos()
    iso_id = isos["alpine-test.iso"]["id"]
    assert stack.api(f"/api/pins/{MAC}", "PUT", {"iso_id": iso_id, "mode": "always"})[0] == 200
    docker("restart", stack.server, timeout=60)
    stack.wait_for_core()
    stack.wait_for_all_isos()
    assert stack.api("/api/clients")[1]["pins"][MAC]["iso_id"] == iso_id
    assert "pinned to alpine-test.iso" in stack.fetch_menu()


def test_passwords_stay_the_same_after_a_restart(stack):
    stack.start()
    first = stack.exec_server("cat", "/data/secrets.json").stdout
    docker("restart", stack.server, timeout=60)
    stack.wait_for_core()
    assert stack.exec_server("cat", "/data/secrets.json").stdout == first
    assert stack.exec_server("stat", "-c", "%a", "/data/secrets.json").stdout.strip() == "600"


def test_a_copy_in_progress_is_ignored_until_it_finishes(private_stack, private_isos):
    harness.build_iso_volume(private_isos, ["alpine-test.iso"])
    private_stack.start(STABLE_SECONDS="8")
    private_stack.wait_for_iso("alpine-test.iso")

    # A real ISO arriving in pieces, as a slow copy would write it.
    on_volume(
        private_isos,
        "cp alpine-test.iso /tmp/whole.iso; head -c 100000 /tmp/whole.iso > incoming.iso",
    )
    private_stack.wait_for_iso("incoming.iso", "waiting")
    assert "incoming.iso" not in private_stack.fetch_menu()

    on_volume(private_isos, "cp alpine-test.iso incoming.iso")
    time.sleep(1)
    assert private_stack.isos_by_name()["incoming.iso"]["status"] == "waiting"

    entry = private_stack.wait_for_iso("incoming.iso")
    assert entry["profile"] == "alpine"
    assert "incoming.iso" in private_stack.fetch_menu()


def test_removing_an_iso_unmounts_it(private_stack, private_isos):
    harness.build_iso_volume(private_isos, ["alpine-test.iso", "ubuntu-desktop.iso"])
    private_stack.start()
    isos = private_stack.wait_for_all_isos()
    iso_id = isos["alpine-test.iso"]["id"]
    assert iso_id in private_stack.exec_server("mount").stdout
    assert iso_id in private_stack.exec_server("cat", "/run/isovault/exports").stdout

    on_volume(private_isos, "rm alpine-test.iso")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and "alpine-test.iso" in private_stack.isos_by_name():
        time.sleep(0.5)

    assert "alpine-test.iso" not in private_stack.isos_by_name()
    assert iso_id not in private_stack.exec_server("mount").stdout
    assert iso_id not in private_stack.exec_server("cat", "/run/isovault/exports").stdout
    assert private_stack.http_status(f"/files/{iso_id}/boot/vmlinuz-virt") == "404"
    assert "no longer available" in private_stack.fetch_boot(iso_id)
    assert private_stack.status()["services"]["unfsd"]["running"]


def test_a_garbage_iso_shows_an_error_and_stays_out_of_the_menu(private_stack, private_isos):
    on_volume(private_isos, "head -c 100000 /dev/urandom > garbage.iso; touch -d 2020-01-01 garbage.iso")
    private_stack.start()
    entry = private_stack.wait_for_iso("garbage.iso", "error")
    assert entry["error"]
    assert entry["bootable"] is False
    assert "garbage.iso" not in private_stack.fetch_menu()
    assert private_stack.status()["services"]["nginx"]["running"]


def test_nfs_export_really_mounts_read_only(stack):
    stack.start()
    isos = stack.wait_for_all_isos()
    iso_id = isos["ubuntu-desktop.iso"]["id"]
    assert "nfs" in stack.status()["features"]
    script = f"""
        set -e
        mkdir -p /mnt/nfs
        mount -t nfs -o ro,nolock,vers=3,tcp {stack.server_ip}:/run/isovault/mnt/{iso_id} /mnt/nfs
        cat /mnt/nfs/casper/filesystem.squashfs
        umount /mnt/nfs
    """
    assert stack.exec_helper("sh", "-c", script).stdout == "squashfs"


def test_ubuntu_boot_script_uses_the_nfs_path_that_mounts(stack):
    stack.start()
    isos = stack.wait_for_all_isos()
    iso_id = isos["ubuntu-desktop.iso"]["id"]
    script = stack.fetch_boot(iso_id)
    assert f"nfsroot={stack.server_ip}:/run/isovault/mnt/{iso_id}" in script
    assert "netboot=nfs" in script


def test_samba_share_serves_windows_files_read_only_with_a_login(stack):
    stack.start()
    isos = stack.wait_for_all_isos()
    iso_id = isos["windows-test.iso"]["id"]
    assert "smb" in stack.status()["features"]
    share = f"//{stack.server_ip}/isovault"
    login = ["-U", f"isovault%{stack.smb_password()}", "-m", "SMB3"]

    listing = stack.exec_helper("smbclient", share, *login, "-c", f"cd {iso_id}; ls").stdout
    assert "setup.exe" in listing
    # Without this, Windows says "Access is denied" when it starts Setup.
    stack.smb_open(f"{iso_id}\\setup.exe")
    assert stack.smb_open(f"{iso_id}\\setup.exe", harness.SMB_WRITE_ACCESS, check=False).returncode != 0

    stack.exec_helper("sh", "-c", "echo x > /tmp/upload.txt")
    write = stack.exec_helper("smbclient", share, *login, "-c", f"cd {iso_id}; put /tmp/upload.txt upload.txt", check=False)
    assert "NT_STATUS_ACCESS_DENIED" in write.stdout + write.stderr or "NT_STATUS_MEDIA_WRITE_PROTECTED" in write.stdout + write.stderr

    guest = stack.exec_helper("smbclient", share, "-N", "-c", "ls", check=False)
    assert guest.returncode != 0
    wrong = stack.exec_helper("smbclient", share, "-U", "isovault%wrong", "-c", "ls", check=False)
    assert wrong.returncode != 0


def test_windows_boot_script_and_startup_files(stack):
    stack.start()
    isos = stack.wait_for_all_isos()
    iso_id = isos["windows-test.iso"]["id"]
    script = stack.fetch_boot(iso_id)
    assert stack.boot_url("/loaders/wimboot") in script
    assert stack.boot_url(f"/files/{iso_id}/Boot/BCD") in script
    assert stack.boot_url(f"/files/{iso_id}/Sources/boot.wim") in script
    for word in script.split():
        if word.startswith("http"):
            assert stack.curl("-o", "/dev/null", "-w", "%{http_code}", word).stdout == "200", word
    startup = stack.curl(stack.boot_url(f"/ipxe/windows/{iso_id}/startup.bat")).stdout
    assert f"\\\\{stack.server_ip}\\isovault\\{iso_id}" in startup
    assert "@@" not in startup
    assert "setup.exe" in startup
    # Checked on the wire, because reading it back here would normalize the line endings.
    url = stack.boot_url(f"/ipxe/windows/{iso_id}/startup.bat")
    carriage_returns = stack.exec_helper("sh", "-c", f"curl -s {url} | tr -cd '\\r' | wc -c").stdout
    assert int(carriage_returns) > 5


def test_windows_drivers_reach_the_boot_and_startup_scripts(driver_volume, stack):
    stack.start("-v", f"{driver_volume}:/drivers:ro")
    isos = stack.wait_for_all_isos()
    windows = isos["windows-test.iso"]
    assert windows["drivers"] == ["rt25.inf"]

    script = stack.fetch_boot(windows["id"])
    for name in ("rt25.inf", "rt25.sys", "rt25.cat"):
        assert f"initrd --name {name} {stack.boot_url(f'/ipxe/drivers/x64/{name}')} {name}" in script
    assert "README" not in script and "setup.exe" not in script and "ok.inf" not in script
    # Everything the device is told to fetch has to really be there.
    for word in script.split():
        if word.startswith("http"):
            assert stack.curl("-o", "/dev/null", "-w", "%{http_code}", word).stdout == "200", word

    startup = stack.curl(stack.boot_url(f"/ipxe/windows/{windows['id']}/startup.bat?arch=x86_64")).stdout
    assert "drvload X:\\Windows\\System32\\rt25.inf" in startup
    assert "wpeutil InitializeNetwork" in startup
    arm = stack.curl(stack.boot_url(f"/ipxe/windows/{windows['id']}/startup.bat?arch=arm64")).stdout
    assert "qc.inf" in arm and "rt25.inf" not in arm


def test_windows_driver_files_are_served_exactly(driver_volume, stack):
    stack.start("-v", f"{driver_volume}:/drivers:ro")
    # This file is private to root, which nginx's unprivileged workers couldn't have read.
    assert stack.curl(stack.boot_url("/ipxe/drivers/x64/rt25.sys")).stdout == "sys-bytes"
    assert stack.curl(stack.boot_url("/ipxe/drivers/arm64/qc.inf")).stdout == "arm-inf"
    for path in (
        "/ipxe/drivers/x64/qc.inf",
        "/ipxe/drivers/x64/setup.exe",
        "/ipxe/drivers/nope/rt25.sys",
        "/ipxe/drivers/x64/..%2f..%2fsecrets.json",
        "/ipxe/drivers/x64/..%2fmessy%2fok.inf",
        "/ipxe/drivers/x64/../../../data/secrets.json",
    ):
        assert stack.http_status(path) in ("400", "404"), path


def test_a_skipped_driver_is_explained_in_the_status(driver_volume, stack):
    stack.start("-v", f"{driver_volume}:/drivers:ro")
    warnings = stack.status()["warnings"]
    assert any("x64/messy" in warning and "not ok.sys" in warning for warning in warnings)
    assert not any("x64/realtek" in warning for warning in warnings)


def test_ports_in_use_turn_off_only_the_shares(stack):
    # Runs in the helper's network namespace, with something already listening on the share ports.
    script = """
import socket, time
held = []
for port, kind in ((445, socket.SOCK_STREAM), (2049, socket.SOCK_STREAM)):
    s = socket.socket(socket.AF_INET, kind)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    s.bind(("", port))
    s.listen()
    held.append(s)
time.sleep(600)
"""
    stack.exec_helper("sh", "-c", f"nohup python3 -c '{script}' >/dev/null 2>&1 &")
    time.sleep(1)
    stack.start(network=f"container:{stack.helper}")
    status = stack.status()
    assert status["features"] == []
    assert any("445" in warning for warning in status["warnings"])
    assert any("2049" in warning for warning in status["warnings"])
    assert status["services"]["nginx"]["running"] and status["services"]["dnsmasq"]["running"]
    assert "rpcbind" not in status["services"] and "smbd" not in status["services"]

    isos = stack.wait_for_all_isos()
    menu = stack.exec_helper("curl", "-s", f"http://127.0.0.1:8080/ipxe/menu?{stack.client_query()}").stdout
    assert "alpine-test.iso" in menu
    assert "windows-test.iso" not in menu
    assert isos["windows-test.iso"]["bootable"] is False
    assert any("Samba" in warning for warning in isos["windows-test.iso"]["warnings"])
    ubuntu = isos["ubuntu-desktop.iso"]
    assert any("RAM" in warning for warning in ubuntu["warnings"])
    boot = stack.exec_helper(
        "curl", "-s", f"http://127.0.0.1:8080/ipxe/boot/{ubuntu['id']}?{stack.client_query()}"
    ).stdout
    assert "url=" in boot and "nfsroot" not in boot


def test_shares_can_be_turned_off_by_setting(stack):
    stack.start(ENABLE_NFS="false", ENABLE_SMB="false")
    assert stack.status()["features"] == []
    assert "smbd" not in stack.status()["services"]


def test_a_required_share_with_a_busy_port_stops_startup(stack):
    script = "import socket,time;s=socket.socket();s.bind(('',445));s.listen();time.sleep(600)"
    stack.exec_helper("sh", "-c", f"nohup python3 -c \"{script}\" >/dev/null 2>&1 &")
    time.sleep(1)
    stack.start(network=f"container:{stack.helper}", wait=False, ENABLE_SMB="true")
    time.sleep(3)
    assert docker("inspect", "-f", "{{.State.Running}}", stack.server).stdout.strip() == "false"
    assert "445" in stack.logs()


def test_a_busy_boot_port_is_a_fatal_error(stack):
    script = "import socket,time;s=socket.socket();s.bind(('',8080));s.listen();time.sleep(600)"
    stack.exec_helper("sh", "-c", f"nohup python3 -c \"{script}\" >/dev/null 2>&1 &")
    time.sleep(1)
    stack.start(network=f"container:{stack.helper}", wait=False)
    time.sleep(3)
    assert docker("inspect", "-f", "{{.State.Running}}", stack.server).stdout.strip() == "false"
    assert "8080" in stack.logs()


def test_a_busy_ui_port_only_disables_the_ui(stack):
    script = "import socket,time;s=socket.socket();s.bind(('',8081));s.listen();time.sleep(600)"
    stack.exec_helper("sh", "-c", f"nohup python3 -c \"{script}\" >/dev/null 2>&1 &")
    time.sleep(1)
    stack.start(network=f"container:{stack.helper}")
    status = stack.status()
    assert "web" not in status["services"]
    assert any("8081" in warning for warning in status["warnings"])
    assert status["services"]["nginx"]["running"]
    assert stack.exec_helper("curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "http://127.0.0.1:8080/healthz").stdout == "200"


def test_without_privileges_isos_are_extracted_instead(stack):
    stack.start("--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW", privileged=False)
    isos = stack.wait_for_all_isos()
    entry = isos["alpine-test.iso"]
    assert entry["status"] == "ready"
    assert entry["method"] == "extract"
    script = stack.fetch_boot(entry["id"])
    url = next(word for word in script.split() if word.startswith("http"))
    assert stack.curl(url).stdout == "kernel"
    # A second start reuses the extraction, because the ISO hasn't changed.
    marker = stack.exec_server("stat", "-c", "%Y", f"/data/cache/{entry['id']}/.iso-vault-complete").stdout
    docker("restart", stack.server, timeout=60)
    stack.wait_for_core()
    stack.wait_for_all_isos()
    assert stack.exec_server("stat", "-c", "%Y", f"/data/cache/{entry['id']}/.iso-vault-complete").stdout == marker


def test_extracted_isos_also_work_over_nfs_and_samba(stack):
    stack.start(ISO_ACCESS="extract")
    isos = stack.wait_for_all_isos()
    ubuntu = isos["ubuntu-desktop.iso"]
    assert ubuntu["method"] == "extract"
    assert f"nfsroot={stack.server_ip}:/data/cache/{ubuntu['id']}" in stack.fetch_boot(ubuntu["id"])
    script = f"""
        set -e
        mkdir -p /mnt/nfs
        mount -t nfs -o ro,nolock,vers=3,tcp {stack.server_ip}:/data/cache/{ubuntu['id']} /mnt/nfs
        cat /mnt/nfs/casper/filesystem.squashfs
        umount /mnt/nfs
    """
    assert stack.exec_helper("sh", "-c", script).stdout == "squashfs"
    windows = isos["windows-test.iso"]["id"]
    listing = stack.exec_helper(
        "smbclient", f"//{stack.server_ip}/isovault", "-U", f"isovault%{stack.smb_password()}", "-m", "SMB3", "-c", f"cd {windows}; ls"
    ).stdout
    assert "setup.exe" in listing
    stack.smb_open(f"{windows}\\setup.exe")


def test_custom_profile_from_the_data_folder_is_used(private_stack, private_isos):
    harness.build_iso_volume(private_isos, ["memtest-tool.iso"])
    profile = """
name = "memtest"
label = "Memtest"
priority = 90
[detect]
all = ["memtest.bin"]
[boot]
method = "sanboot"
"""
    docker(
        "run", "--rm", "-i", "-v", f"{private_stack.data}:/data", harness.HELPER_IMAGE, "sh", "-c",
        "mkdir -p /data/profiles.d; cat > /data/profiles.d/memtest.toml",
        input_text=profile,
    )  # fmt: skip
    private_stack.start()
    entry = private_stack.wait_for_iso("memtest-tool.iso")
    assert entry["profile"] == "memtest"
    assert entry["profile_label"] == "Memtest"


def test_default_iso_and_timeout_reach_the_menu(stack):
    stack.start(DEFAULT_ISO="alpine-test.iso", MENU_TIMEOUT="7")
    stack.wait_for_all_isos()
    menu = stack.fetch_menu()
    line = next(l for l in menu.splitlines() if l.startswith("choose"))
    assert "--timeout 7000" in line
    assert "--default local" not in line


def test_stopping_is_clean(stack):
    stack.start()
    stack.wait_for_all_isos()
    started = time.monotonic()
    docker("stop", "-t", "20", stack.server, timeout=30)
    assert time.monotonic() - started < 15
    assert docker("inspect", "-f", "{{.State.ExitCode}}", stack.server).stdout.strip() == "0"
    assert "Shutting down" in stack.logs()
