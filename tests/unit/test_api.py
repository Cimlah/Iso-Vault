import json
import socket
import threading
from http.client import HTTPConnection

import pytest

from conftest import ROOT, make_tree
from isovault import api, ipxe
from isovault.config import Config, Secrets
from isovault.netboot import NetbootError
from isovault.network import NetInfo
from isovault.profiles import ARM64, X86_64, Tree, load_profiles
from isovault.scanner import READY, Entry
from isovault.state import State

MAC = "52:54:00:aa:bb:cc"
UEFI = ipxe.Client(mac=MAC, arch=X86_64, platform="efi")
ARM = ipxe.Client(mac=MAC, arch=ARM64, platform="efi")
NET = NetInfo("eth0", "10.0.0.5", 24, "10.0.0.0", "255.255.255.0", "10.0.0.1", "02:00:00:00:00:01")


class FakeScanner:
    def __init__(self, entries):
        self.entries = entries

    def snapshot(self):
        return list(self.entries)

    def get(self, iso_id):
        return next((e for e in self.entries if e.id == iso_id), None)

    def find_by_relpath(self, relpath):
        return next((e for e in self.entries if e.relpath == relpath), None)


class FakeNetboot:
    def __init__(self, error=False):
        self.error = error
        self.calls = []

    def ensure(self, suite, arch):
        self.calls.append((suite, arch))
        if self.error:
            raise NetbootError("offline")
        return f"{suite}/amd64"


@pytest.fixture(scope="module")
def all_profiles():
    return {p.name: p for p in load_profiles(ROOT / "isovault" / "profiles")}


def entry(all_profiles, tmp_path, iso_id, profile, files, archs=(X86_64,), group="", status=READY):
    root = make_tree(tmp_path / iso_id, files)
    return Entry(
        id=iso_id,
        relpath=f"{group}/{iso_id}.iso" if group else f"{iso_id}.iso",
        name=f"{iso_id}.iso",
        group=group,
        size=2**31,
        mtime=1.0,
        status=status,
        root=str(root),
        method="mount",
        profile=all_profiles[profile],
        archs=archs,
    )


def make_runtime(tmp_path, entries, features=frozenset({"nfs", "smb"}), netboot=None, **config):
    settings = Config(app_dir=ROOT, run_dir=tmp_path / "run", **config)
    # The repo keeps templates at its root, which is where the image puts them under app_dir.
    return api.Runtime(
        config=settings,
        net=NET,
        secrets=Secrets("admin", "session", "abc123"),
        state=State(tmp_path / "state.json"),
        scanner=FakeScanner(entries),
        netboot=netboot or FakeNetboot(),
        effective_mode="proxy",
        features=features,
    )


ALPINE_FILES = [".alpine-release", "boot/vmlinuz-virt", "boot/initramfs-virt", "boot/modloop-virt"]
WINDOWS_FILES = ["Boot/BCD", "Boot/boot.sdi", "Sources/boot.wim", "setup.exe"]
UBUNTU_FILES = ["casper/vmlinuz", "casper/initrd"]


@pytest.fixture
def alpine(all_profiles, tmp_path):
    return entry(all_profiles, tmp_path, "alpine-1", "alpine", ALPINE_FILES)


def test_menu_hides_isos_for_other_architectures(all_profiles, tmp_path, alpine):
    arm_only = entry(all_profiles, tmp_path, "arm-2", "alpine", ALPINE_FILES, archs=(ARM64,))
    runtime = make_runtime(tmp_path, [alpine, arm_only])
    script = api.menu_script(runtime, UEFI, "10.0.0.9", False)
    assert "alpine-1.iso" in script
    assert "arm-2.iso" not in script
    assert "1 ISO is hidden" in script
    assert "arm-2.iso" in api.menu_script(runtime, ARM, "10.0.0.9", False)


def test_an_iso_of_unknown_architecture_shows_everywhere(all_profiles, tmp_path):
    unknown = entry(all_profiles, tmp_path, "tool-1", "generic", ["a"], archs=())
    runtime = make_runtime(tmp_path, [unknown])
    assert "tool-1.iso" in api.menu_script(runtime, UEFI, "", False)
    assert "tool-1.iso" in api.menu_script(runtime, ARM, "", False)


def test_menu_only_lists_ready_isos(all_profiles, tmp_path):
    waiting = entry(all_profiles, tmp_path, "new-1", "alpine", ALPINE_FILES, status="waiting")
    runtime = make_runtime(tmp_path, [waiting])
    assert "new-1.iso" not in api.menu_script(runtime, UEFI, "", False)


def test_menu_records_the_device(tmp_path, alpine):
    runtime = make_runtime(tmp_path, [alpine])
    api.menu_script(runtime, UEFI, "10.0.0.9", False)
    assert runtime.state.snapshot()["clients"][MAC]["ip"] == "10.0.0.9"


def test_a_pinned_device_skips_the_menu(tmp_path, alpine):
    runtime = make_runtime(tmp_path, [alpine])
    runtime.state.set_pin(MAC, "alpine-1", "once")
    script = api.menu_script(runtime, UEFI, "", False)
    assert "pinned to alpine-1.iso" in script
    assert "pin=1" in script
    assert "choose" not in script


def test_nopin_shows_the_menu_and_keeps_the_pin(tmp_path, alpine):
    runtime = make_runtime(tmp_path, [alpine])
    runtime.state.set_pin(MAC, "alpine-1", "once")
    assert "choose" in api.menu_script(runtime, UEFI, "", True)
    assert runtime.state.get_pin(MAC) is not None


def test_a_pin_to_a_missing_iso_falls_back_to_the_menu(tmp_path, alpine):
    runtime = make_runtime(tmp_path, [alpine])
    runtime.state.set_pin(MAC, "gone-9", "always")
    assert "choose" in api.menu_script(runtime, UEFI, "", False)


def test_a_pin_to_an_incompatible_iso_falls_back_to_the_menu(all_profiles, tmp_path):
    arm_only = entry(all_profiles, tmp_path, "arm-2", "alpine", ALPINE_FILES, archs=(ARM64,))
    runtime = make_runtime(tmp_path, [arm_only])
    runtime.state.set_pin(MAC, "arm-2", "always")
    assert "choose" in api.menu_script(runtime, UEFI, "", False)


def test_a_once_pin_clears_only_when_the_boot_script_is_fetched(tmp_path, alpine):
    runtime = make_runtime(tmp_path, [alpine])
    runtime.state.set_pin(MAC, "alpine-1", "once")
    api.menu_script(runtime, UEFI, "", False)
    assert runtime.state.get_pin(MAC) is not None
    script = api.boot_script(runtime, UEFI, "alpine-1", "", True)
    assert "kernel " in script
    assert runtime.state.get_pin(MAC) is None


def test_choosing_from_the_menu_doesnt_clear_a_pin(tmp_path, alpine):
    runtime = make_runtime(tmp_path, [alpine])
    runtime.state.set_pin(MAC, "alpine-1", "once")
    api.boot_script(runtime, UEFI, "alpine-1", "", False)
    assert runtime.state.get_pin(MAC) is not None


def test_a_failed_pinned_boot_keeps_the_pin(tmp_path, alpine):
    runtime = make_runtime(tmp_path, [alpine])
    runtime.state.set_pin(MAC, "other-2", "once")
    script = api.boot_script(runtime, UEFI, "other-2", "", True)
    assert "no longer available" in script
    assert runtime.state.get_pin(MAC) is not None


def test_default_iso_is_preselected(tmp_path, alpine):
    runtime = make_runtime(tmp_path, [alpine], default_iso="alpine-1.iso", menu_timeout=3)
    script = api.menu_script(runtime, UEFI, "", False)
    assert "--timeout 3000 --default iso0" in script


def test_boot_script_for_alpine(tmp_path, alpine):
    runtime = make_runtime(tmp_path, [alpine], extra_kernel_args="console=ttyS0")
    script = api.boot_script(runtime, UEFI, "alpine-1", "", False)
    assert "kernel http://10.0.0.5:8080/files/alpine-1/boot/vmlinuz-virt initrd=initrd.magic" in script
    assert "modloop=http://10.0.0.5:8080/files/alpine-1/boot/modloop-virt" in script
    assert "console=ttyS0" in script


def test_boot_refuses_the_wrong_architecture(tmp_path, alpine):
    runtime = make_runtime(tmp_path, [alpine])
    assert "cannot boot on this architecture" in api.boot_script(runtime, ARM, "alpine-1", "", False)


def test_ubuntu_uses_ram_when_nfs_is_off(all_profiles, tmp_path):
    ubuntu = entry(all_profiles, tmp_path, "ubuntu-1", "ubuntu-casper", UBUNTU_FILES)
    script = api.boot_script(make_runtime(tmp_path, [ubuntu], features=frozenset()), UEFI, "ubuntu-1", "", False)
    assert "url=http://10.0.0.5:8080/raw/ubuntu-1.iso" in script


def test_generic_uses_sanboot(all_profiles, tmp_path):
    tool = entry(all_profiles, tmp_path, "tool-1", "generic", ["a"], archs=())
    script = api.boot_script(make_runtime(tmp_path, [tool]), UEFI, "tool-1", "", False)
    assert "sanboot --no-describe http://10.0.0.5:8080/raw/tool-1.iso" in script


def test_windows_is_hidden_without_samba(all_profiles, tmp_path):
    windows = entry(all_profiles, tmp_path, "win-1", "windows", WINDOWS_FILES)
    runtime = make_runtime(tmp_path, [windows], features=frozenset({"nfs"}))
    assert "win-1.iso" not in api.menu_script(runtime, UEFI, "", False)
    assert "Samba" in api.boot_script(runtime, UEFI, "win-1", "", False)
    described = api.describe_entry(runtime, windows)
    assert described["bootable"] is False
    assert any("Samba" in warning for warning in described["warnings"])


def test_windows_boot_uses_real_paths(all_profiles, tmp_path):
    windows = entry(all_profiles, tmp_path, "win-1", "windows", WINDOWS_FILES)
    script = api.boot_script(make_runtime(tmp_path, [windows]), UEFI, "win-1", "", False)
    assert "/files/win-1/Boot/BCD BCD" in script
    assert "/files/win-1/Sources/boot.wim boot.wim" in script


def test_windows_startup_script_is_rendered_with_crlf(all_profiles, tmp_path):
    windows = entry(all_profiles, tmp_path, "win-1", "windows", WINDOWS_FILES)
    runtime = make_runtime(tmp_path, [windows])
    text = api.render_windows_file(runtime, "win-1", "startup.bat")
    assert "\\\\10.0.0.5\\isovault\\win-1 abc123 /user:10.0.0.5\\isovault" in text
    assert "@@" not in text
    assert text.count("\n") == text.count("\r\n")


@pytest.mark.parametrize("name", ["../../secrets.json", "other.txt", "startup.bat.bak"])
def test_windows_files_only_serve_the_two_templates(all_profiles, tmp_path, name):
    windows = entry(all_profiles, tmp_path, "win-1", "windows", WINDOWS_FILES)
    assert api.render_windows_file(make_runtime(tmp_path, [windows]), "win-1", name) is None


def test_windows_files_arent_served_for_other_families(tmp_path, alpine):
    assert api.render_windows_file(make_runtime(tmp_path, [alpine]), "alpine-1", "startup.bat") is None


DEBIAN_FILES = [
    (".disk/info", "Debian GNU/Linux 13"),
    "install.amd/vmlinuz",
    "dists/trixie/Release",
    "dists/stable/Release",
]


def test_debian_installer_uses_netboot_files(all_profiles, tmp_path):
    debian = entry(all_profiles, tmp_path, "debian-1", "debian-installer", DEBIAN_FILES)
    netboot = FakeNetboot()
    script = api.boot_script(make_runtime(tmp_path, [debian], netboot=netboot), UEFI, "debian-1", "", False)
    assert netboot.calls == [("trixie", X86_64)]
    assert "kernel http://10.0.0.5:8080/netboot/trixie/amd64/linux initrd=initrd.magic" in script
    assert "initrd http://10.0.0.5:8080/netboot/trixie/amd64/initrd.gz" in script


def test_debian_installer_explains_a_download_failure(all_profiles, tmp_path):
    debian = entry(all_profiles, tmp_path, "debian-1", "debian-installer", DEBIAN_FILES)
    runtime = make_runtime(tmp_path, [debian], netboot=FakeNetboot(error=True))
    assert "internet access" in api.boot_script(runtime, UEFI, "debian-1", "", False)


def test_status_includes_upstream_values(tmp_path, alpine):
    payload = api.status_payload(make_runtime(tmp_path, [alpine]))
    assert payload["upstream"]["next_server"] == "10.0.0.5"
    assert "snponly-arm64.efi" in payload["upstream"]["filenames"].values()


@pytest.fixture
def server(tmp_path, alpine):
    (tmp_path / "run").mkdir()
    runtime = make_runtime(tmp_path, [alpine])
    socket_path = tmp_path / "core.sock"
    instance = api.UnixServer(socket_path, runtime)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield socket_path, runtime
    instance.shutdown()
    instance.server_close()


class UnixConnection(HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost")
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(str(self.path))


def call(path, method, url, body=None, headers=None):
    connection = UnixConnection(path)
    payload = json.dumps(body) if body is not None else None
    connection.request(method, url, payload, headers or {})
    response = connection.getresponse()
    text = response.read().decode()
    connection.close()
    return response.status, text


def test_http_menu_and_client_tracking(server):
    path, runtime = server
    status, text = call(path, "GET", f"/ipxe/menu?mac=52-54-00-AA-BB-CC&arch=x86_64&platform=efi", headers={"X-Real-IP": "10.0.0.9"})
    assert status == 200 and text.startswith("#!ipxe")
    assert runtime.state.snapshot()["clients"][MAC]["ip"] == "10.0.0.9"


@pytest.mark.parametrize(
    "query",
    ["mac=$(reboot)&arch=${x}&platform=evil", "mac=52:54:00:aa:bb:cc%0Ashell&arch=arm", ""],
)
def test_http_menu_never_echoes_bad_parameters(server, query):
    path, runtime = server
    status, text = call(path, "GET", f"/ipxe/menu?{query}")
    assert status == 200
    assert "$(reboot)" not in text and "evil" not in text and "%0A" not in text
    assert "mac=" not in text
    assert runtime.state.snapshot()["clients"] == {}


def test_http_pin_lifecycle(server):
    path, runtime = server
    status, _ = call(path, "PUT", f"/api/pins/{MAC}", {"iso_id": "alpine-1", "mode": "always"})
    assert status == 200
    assert runtime.state.get_pin(MAC)["mode"] == "always"
    status, text = call(path, "DELETE", f"/api/pins/{MAC}")
    assert status == 200 and json.loads(text) == {"removed": True}


def test_http_pin_accepts_an_encoded_mac(server):
    """The web layer URL-encodes the colons, as any correct HTTP client would."""
    path, runtime = server
    status, _ = call(path, "PUT", "/api/pins/52%3A54%3A00%3Aaa%3Abb%3Acc", {"iso_id": "alpine-1"})
    assert status == 200
    assert runtime.state.get_pin(MAC)["iso_id"] == "alpine-1"
    status, _ = call(path, "DELETE", "/api/pins/52%3A54%3A00%3Aaa%3Abb%3Acc")
    assert status == 200
    assert runtime.state.get_pin(MAC) is None


@pytest.mark.parametrize(
    "mac, body, expected",
    [
        (MAC, {"iso_id": "missing"}, 404),
        (MAC, {"mode": "once"}, 400),
        (MAC, {"iso_id": "alpine-1", "mode": "forever"}, 400),
        (MAC, {"iso_id": "alpine-1", "mode": 7}, 400),
        ("not-a-mac", {"iso_id": "alpine-1"}, 400),
    ],
)
def test_http_pin_rejects_bad_requests(server, mac, body, expected):
    path, runtime = server
    status, _ = call(path, "PUT", f"/api/pins/{mac}", body)
    assert status == expected
    assert runtime.state.snapshot()["pins"] == {}


def test_http_pin_rejects_a_malformed_body(server):
    path, _ = server
    connection = UnixConnection(path)
    connection.request("PUT", f"/api/pins/{MAC}", "{nope", {})
    assert connection.getresponse().status == 400


def test_http_unknown_paths_are_404(server):
    path, _ = server
    for url in ("/", "/api/nope", "/ipxe/nope", "/ipxe/windows/alpine-1/startup.bat"):
        assert call(path, "GET", url)[0] == 404


def test_http_lists_isos_and_status(server):
    path, _ = server
    status, text = call(path, "GET", "/api/isos")
    assert status == 200 and json.loads(text)[0]["profile"] == "alpine"
    status, text = call(path, "GET", "/api/status")
    assert status == 200 and json.loads(text)["server_ip"] == "10.0.0.5"
