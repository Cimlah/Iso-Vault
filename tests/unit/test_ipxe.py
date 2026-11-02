import re

import pytest

from isovault import ipxe
from isovault.profiles import ARM64, X86_64, BootPlan

BASE = "http://10.0.0.5:8080"
UEFI = ipxe.Client(mac="52:54:00:aa:bb:cc", arch=X86_64, platform="efi")
BIOS = ipxe.Client(mac="52:54:00:aa:bb:cc", arch=X86_64, platform="pcbios")
ARM = ipxe.Client(mac="52:54:00:aa:bb:cc", arch=ARM64, platform="efi")
ITEMS = [
    ipxe.MenuItem(id="ubuntu-1", name="ubuntu.iso", group=""),
    ipxe.MenuItem(id="fedora-2", name="fedora.iso", group="linux/server"),
    ipxe.MenuItem(id="debian-3", name="debian.iso", group="", note="needs internet"),
]


@pytest.mark.parametrize(
    "hostile",
    [
        "${net0/mac}.iso",
        "${shell}",
        "a||shell",
        "a&&reboot",
        'quote".iso',
        "line\nbreak\nshell.iso",
        "tab\there.iso",
        "#comment.iso",
        "$(reboot).iso",
        "a;reboot.iso",
    ],
)
def test_labels_cant_carry_ipxe_syntax(hostile):
    cleaned = ipxe.sanitize_label(hostile)
    assert not set(cleaned) & set('${}"\n\t\r#;&|')


def test_labels_stay_readable_and_bounded():
    assert ipxe.sanitize_label("Ubuntu 24.04 (Server).iso") == "Ubuntu 24.04 (Server).iso"
    assert ipxe.sanitize_label("Żółw.iso") == "Zow.iso"
    assert len(ipxe.sanitize_label("x" * 300)) <= 66
    assert ipxe.sanitize_label("日本語") == "Unnamed"
    assert ipxe.sanitize_label("") == "Unnamed"


def test_a_hostile_filename_cant_add_commands_to_the_menu():
    items = [ipxe.MenuItem(id="evil-1", name="x\nshell\n${reboot}.iso", group="a\nreboot")]
    script = ipxe.render_menu(BASE, UEFI, items)
    item_lines = [line for line in script.splitlines() if line.startswith("item iso0")]
    assert len(item_lines) == 1
    assert "$" not in item_lines[0]
    # The only ${...} left is the menu's own jump to the chosen label.
    assert re.findall(r"\$\{[^}]*\}", script) == ["${selected}"]


def test_menu_lists_items_and_links_to_their_boot_scripts():
    script = ipxe.render_menu(BASE, UEFI, ITEMS)
    assert script.startswith("#!ipxe")
    assert "choose --timeout 30000 --default local selected || goto local" in script
    assert f"chain {BASE}/ipxe/boot/ubuntu-1?mac=52:54:00:aa:bb:cc&arch=x86_64&platform=efi || goto failed" in script
    assert "item --gap -- linux/server/" in script
    assert "debian.iso (needs internet)" in script


def test_every_goto_in_the_menu_has_a_label():
    script = ipxe.render_menu(BASE, UEFI, ITEMS)
    labels = set(re.findall(r"^:(\S+)$", script, re.M))
    targets = set(re.findall(r"goto (\w+)", script))
    assert targets <= labels
    chosen = set(re.findall(r"^item (iso\d+|local|refresh|reboot|shell) ", script, re.M))
    assert chosen <= labels


def test_menu_default_and_timeout():
    script = ipxe.render_menu(BASE, UEFI, ITEMS, timeout_seconds=5, default_id="fedora-2")
    line = next(l for l in script.splitlines() if l.startswith("choose"))
    label = re.search(r"--default (\S+)", line).group(1)
    assert "--timeout 5000" in line
    assert label != "local"
    assert re.search(rf"^:{label}\n.*fedora-2", script, re.M)


def test_a_zero_timeout_waits_forever():
    assert "--timeout" not in next(
        l for l in ipxe.render_menu(BASE, UEFI, ITEMS, timeout_seconds=0).splitlines() if l.startswith("choose")
    )


def test_an_unknown_default_falls_back_to_local_disk():
    assert "--default local" in ipxe.render_menu(BASE, UEFI, ITEMS, default_id="gone")


def test_empty_menu_says_so_and_mentions_hidden_isos():
    script = ipxe.render_menu(BASE, UEFI, [], hidden_count=2)
    assert "No ISOs are ready" in script
    assert "2 ISOs are hidden" in script


def test_local_boot_differs_by_firmware():
    assert "sanboot --no-describe --drive 0x80" in ipxe.render_menu(BASE, BIOS, ITEMS)
    uefi = ipxe.render_menu(BASE, UEFI, ITEMS)
    assert "--drive 0x80" not in uefi
    assert ":local\nexit 1" in uefi


def test_pinned_script_offers_the_menu_and_marks_the_pin():
    script = ipxe.render_pinned(BASE, UEFI, "ubuntu-1", "ubuntu.iso")
    assert "prompt --timeout 5000" in script
    assert "&& goto menu" in script
    assert f"{BASE}/ipxe/boot/ubuntu-1?" in script and "pin=1" in script
    assert "nopin=1" in script


def test_linux_on_uefi_uses_initrd_magic():
    plan = BootPlan(kernel="casper/vmlinuz", initrds=["casper/initrd"], args="boot=casper")
    script = ipxe.render_linux(UEFI, "ubuntu.iso", f"{BASE}/files/x", plan)
    assert f"kernel {BASE}/files/x/casper/vmlinuz initrd=initrd.magic boot=casper || goto failed" in script
    assert f"initrd {BASE}/files/x/casper/initrd || goto failed" in script
    assert script.index("imgfree") < script.index("kernel")


def test_linux_on_bios_omits_initrd_magic():
    plan = BootPlan(kernel="k", initrds=["i"], args="a")
    assert "initrd.magic" not in ipxe.render_linux(BIOS, "x.iso", BASE, plan)


def test_linux_loads_initrds_in_order():
    plan = BootPlan(kernel="k", initrds=["ucode", "main"], args="a")
    script = ipxe.render_linux(UEFI, "x.iso", BASE, plan)
    assert script.index("/ucode") < script.index("/main")


def test_windows_picks_the_loader_for_the_arch():
    args = ("i", "Win.iso", f"{BASE}/files/i", "Boot/BCD", "Boot/boot.sdi", "Sources/boot.wim")
    assert f"kernel {BASE}/loaders/wimboot || goto failed" in ipxe.render_windows(UEFI, BASE, *args)
    assert f"kernel {BASE}/loaders/wimboot.arm64 || goto failed" in ipxe.render_windows(ARM, BASE, *args)


def test_windows_uses_the_real_paths_and_required_names():
    script = ipxe.render_windows(UEFI, BASE, "i", "W", f"{BASE}/files/i", "Boot/BCD", "Boot/boot.sdi", "Sources/boot.wim")
    assert f"initrd --name BCD {BASE}/files/i/Boot/BCD BCD" in script
    assert f"initrd --name boot.wim {BASE}/files/i/Sources/boot.wim boot.wim" in script
    assert script.index("winpeshl.ini") < script.index("BCD")


def test_sanboot_uses_the_raw_iso():
    assert f"sanboot --no-describe {BASE}/raw/x.iso" in ipxe.render_sanboot("X", f"{BASE}/raw/x.iso")


def test_error_scripts_fail_back_to_the_caller():
    script = ipxe.error_script("That ISO ${is} gone")
    assert script.rstrip().endswith("exit 1")
    assert "${" not in script


@pytest.mark.parametrize("build, expected", [("i386", X86_64), ("x86_64", X86_64), ("arm64", ARM64), ("", X86_64)])
def test_client_arch(build, expected):
    assert ipxe.client_arch(build) == expected


def test_no_script_contains_a_quote_character():
    plan = BootPlan(kernel="k", initrds=["i"], args="a")
    scripts = [
        ipxe.render_menu(BASE, UEFI, ITEMS, hidden_count=2),
        ipxe.render_menu(BASE, BIOS, [ipxe.MenuItem(id="a", name="Bob's \"disc\".iso", group="it's")]),
        ipxe.render_pinned(BASE, UEFI, "a", "Bob's.iso"),
        ipxe.render_linux(UEFI, "Bob's.iso", BASE, plan),
        ipxe.render_debian_netboot(UEFI, "Bob's.iso", BASE),
        ipxe.render_windows(UEFI, BASE, "a", "Bob's.iso", BASE, "b", "c", "d"),
        ipxe.render_sanboot("Bob's.iso", BASE),
        ipxe.error_script("It's gone"),
    ]
    for script in scripts:
        assert "'" not in script and '"' not in script
