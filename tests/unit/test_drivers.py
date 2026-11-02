import re

import pytest

from isovault import ipxe
from isovault.drivers import MAX_FILES, driver_commands, find_drivers
from isovault.profiles import ARM64, X86_64

from conftest import ROOT, make_tree

BASE = "http://10.0.0.5:8080"
REALTEK = ["x64/realtek/rt25.inf", "x64/realtek/rt25.sys", "x64/realtek/rt25.cat"]


def test_finds_a_driver_and_its_files(tmp_path):
    make_tree(tmp_path, REALTEK)
    found = find_drivers(tmp_path, X86_64)
    assert found.infs == ("rt25.inf",)
    assert sorted(found.files) == ["rt25.cat", "rt25.inf", "rt25.sys"]
    assert found.files["rt25.sys"] == tmp_path / "x64/realtek/rt25.sys"
    assert found.problems == ()


def test_each_architecture_only_gets_its_own_folder(tmp_path):
    make_tree(tmp_path, REALTEK + ["arm64/qualcomm/qc.inf"])
    assert find_drivers(tmp_path, X86_64).infs == ("rt25.inf",)
    assert find_drivers(tmp_path, ARM64).infs == ("qc.inf",)


def test_a_missing_folder_is_not_a_problem(tmp_path):
    found = find_drivers(tmp_path / "nothing-here", X86_64)
    assert (found.files, found.infs, found.problems) == ({}, (), ())


def test_files_a_driver_doesnt_need_are_left_out(tmp_path):
    make_tree(
        tmp_path,
        REALTEK
        + [
            "x64/realtek/README.txt",
            "x64/realtek/setup.exe",
            "x64/realtek/Release Notes.pdf",
            "x64/realtek/.DS_Store",
            "x64/realtek/._rt25.inf",
            "x64/realtek/rt25.din",
        ],
    )
    found = find_drivers(tmp_path, X86_64)
    assert sorted(found.files) == ["rt25.cat", "rt25.din", "rt25.inf", "rt25.sys"]
    assert found.problems == ()


def test_autorun_isnt_a_driver(tmp_path):
    make_tree(tmp_path, ["x64/download/autorun.inf", "x64/download/setup.exe"])
    found = find_drivers(tmp_path, X86_64)
    assert found.infs == ()
    assert found.files == {}


def test_a_folder_with_no_inf_is_ignored(tmp_path):
    make_tree(tmp_path, ["x64/tools/helper.sys"])
    assert find_drivers(tmp_path, X86_64).files == {}


@pytest.mark.parametrize("bad", ["has space.sys", "a" * 28 + ".sys", "evil$(x).sys", "per%cent.sys", "semi;colon.sys"])
def test_a_driver_with_an_unusable_name_is_skipped_whole(tmp_path, bad):
    make_tree(tmp_path, REALTEK + [f"x64/realtek/{bad}"])
    found = find_drivers(tmp_path, X86_64)
    assert found.infs == ()
    assert found.files == {}
    assert "x64/realtek" in found.problems[0]
    assert bad in found.problems[0]


def test_the_longest_name_wimboot_keeps_is_accepted(tmp_path):
    name = "a" * 27 + ".inf"
    assert len(name) == 31
    make_tree(tmp_path, [f"x64/long/{name}"])
    assert find_drivers(tmp_path, X86_64).infs == (name,)


def test_a_name_clash_between_drivers_skips_the_second_whole(tmp_path):
    make_tree(tmp_path, ["x64/a/net.inf", "x64/a/net.sys", "x64/b/other.inf", "x64/b/NET.SYS"])
    found = find_drivers(tmp_path, X86_64)
    assert found.infs == ("net.inf",)
    assert "other.inf" not in found.files
    assert "x64/b" in found.problems[0]


def test_going_over_the_limit_skips_a_driver_whole(tmp_path):
    fits = [f"x64/a/f{n}.bin" for n in range(MAX_FILES - 1)] + ["x64/a/a.inf"]
    make_tree(tmp_path, fits + ["x64/b/b.inf", "x64/b/b.sys"])
    found = find_drivers(tmp_path, X86_64)
    assert len(found.files) == MAX_FILES
    assert found.infs == ("a.inf",)
    assert "x64/b" in found.problems[0]


def test_links_are_never_followed(tmp_path):
    make_tree(tmp_path, REALTEK)
    secret = tmp_path / "secrets.json"
    secret.write_text("private")
    (tmp_path / "x64/realtek/leak.bin").symlink_to(secret)
    assert "leak.bin" not in find_drivers(tmp_path, X86_64).files


def test_commands_load_each_driver_then_restart_the_network():
    lines = driver_commands(["a.inf", "b.inf"]).splitlines()
    assert "drvload X:\\Windows\\System32\\a.inf" in lines
    assert lines.index("drvload X:\\Windows\\System32\\b.inf") < lines.index("wpeutil InitializeNetwork")


def test_with_no_drivers_the_commands_do_nothing():
    text = driver_commands([])
    assert text.startswith("rem ")
    assert "drvload" not in text


def windows_script(client, drivers=()):
    return ipxe.render_windows(client, BASE, "i", "W", f"{BASE}/files/i", "b/BCD", "b/boot.sdi", "s/boot.wim", drivers)


def test_boot_script_injects_each_driver_file():
    client = ipxe.Client("52:54:00:aa:bb:cc", X86_64, "efi")
    script = windows_script(client, ["rt25.inf", "rt25.sys"])
    assert f"initrd --name rt25.inf {BASE}/ipxe/drivers/x64/rt25.inf rt25.inf || goto failed" in script
    assert f"initrd --name rt25.sys {BASE}/ipxe/drivers/x64/rt25.sys rt25.sys || goto failed" in script


def test_boot_script_uses_the_folder_for_the_clients_architecture():
    client = ipxe.Client("52:54:00:aa:bb:cc", ARM64, "efi")
    assert f"{BASE}/ipxe/drivers/arm64/qc.inf" in windows_script(client, ["qc.inf"])


def test_startup_script_is_told_the_architecture():
    assert "startup.bat?arch=arm64 startup.bat" in windows_script(ipxe.Client("", ARM64, "efi"))


STARTUP = (ROOT / "templates/windows/startup.bat").read_text()


def test_every_goto_in_the_startup_script_has_a_label():
    labels = set(re.findall(r"^:(\w+)$", STARTUP, re.M))
    targets = set(re.findall(r"goto (\w+)", STARTUP))
    assert targets
    assert targets <= labels


def test_startup_script_keeps_the_real_error():
    connect = next(line for line in STARTUP.splitlines() if line.startswith("net use"))
    assert ">nul" not in connect
    assert ">X:\\isovault-net.txt 2>&1" in connect
    assert "type X:\\isovault-net.txt" in STARTUP


def test_startup_script_loads_drivers_before_connecting():
    assert STARTUP.index("wpeinit") < STARTUP.index("@@DRIVER_COMMANDS@@") < STARTUP.index("net use")


def test_the_adapter_check_cant_stop_a_boot_that_would_work():
    assert STARTUP.index("if %attempts% geq 30 goto failed") < STARTUP.index(":failed")
    assert STARTUP.index(":failed") < STARTUP.index("findstr")


def test_adapter_check_doesnt_depend_on_the_language():
    check = next(line for line in STARTUP.splitlines() if "findstr" in line)
    assert '"%mac%"' in check
    pattern = next(line for line in STARTUP.splitlines() if line.startswith("set mac=")).split("=", 1)[1]
    assert re.match(pattern, "A4-BB-6D-01-02-03")
    # From `ipconfig /all` on a real PC with no driver, where Windows PE sees no adapter at all.
    no_adapter = """
Windows IP Configuration

   Host Name . . . . . . . . . . . . : minint-l3qtiau
   Primary Dns Suffix  . . . . . . . :
   Node Type . . . . . . . . . . . . : Hybrid
   IP Routing Enabled. . . . . . . . : No
   WINS Proxy Enabled. . . . . . . . : No
"""
    assert not re.search(pattern, no_adapter, re.I)
    assert re.search(pattern, "   Adres fizyczny. . . . . . . . . . : A4-BB-6D-01-02-03", re.I)
