import pytest

from isovault.profiles import (
    ARM64,
    X86_64,
    BootContext,
    ProfileError,
    Tree,
    build_linux_plan,
    detect_archs,
    identify,
    load_profiles,
    parse_profile,
)

UBUNTU_DESKTOP = [
    "EFI/BOOT/BOOTx64.EFI",
    ".disk/info",
    "casper/vmlinuz",
    "casper/initrd",
    "casper/filesystem.squashfs",
]
UBUNTU_SERVER = [
    "EFI/boot/bootx64.efi",
    "casper/vmlinuz",
    "casper/initrd",
    "casper/ubuntu-server-minimal.squashfs",
]
UBUNTU_ARM = [
    "EFI/boot/bootaa64.efi",
    "casper/vmlinuz",
    "casper/initrd",
    "casper/ubuntu-server-minimal.squashfs",
]
DEBIAN_LIVE = [
    "EFI/boot/bootx64.efi",
    "live/vmlinuz-6.12.0-amd64",
    "live/initrd.img-6.12.0-amd64",
    "live/filesystem.squashfs",
]
KALI_LIVE = ["live/vmlinuz", "live/initrd.img", "live/filesystem.squashfs", "install/", (".disk/info", "Kali")]
DEBIAN_NETINST = [
    "EFI/boot/bootx64.efi",
    (".disk/info", "Debian GNU/Linux 13.1.0 Trixie - Official amd64 NETINST"),
    "install.amd/vmlinuz",
    "install.amd/initrd.gz",
    "dists/trixie/Release",
    "dists/stable/Release",
]
FEDORA_SERVER = [
    "EFI/BOOT/BOOTX64.EFI",
    (".treeinfo", "[general]\narch = x86_64\n"),
    "images/pxeboot/vmlinuz",
    "images/pxeboot/initrd.img",
    "images/install.img",
]
ROCKY_ARM = [
    "EFI/BOOT/BOOTAA64.EFI",
    (".treeinfo", "[general]\narch = aarch64\n"),
    "images/pxeboot/vmlinuz",
    "images/pxeboot/initrd.img",
    "images/install.img",
]
FEDORA_LIVE_NEW = ["boot/x86_64/loader/linux", "boot/x86_64/loader/initrd", "LiveOS/squashfs.img"]
FEDORA_LIVE_OLD = ["images/pxeboot/vmlinuz", "images/pxeboot/initrd.img", "LiveOS/squashfs.img"]
ARCH = [
    "arch/boot/x86_64/vmlinuz-linux",
    "arch/boot/x86_64/initramfs-linux.img",
    "arch/boot/intel-ucode.img",
    "arch/boot/amd-ucode.img",
    "arch/x86_64/airootfs.sfs",
]
OPENSUSE = ["media.1/media", "boot/x86_64/loader/linux", "boot/x86_64/loader/initrd", "boot/x86_64/root"]
OPENSUSE_ARM = ["media.1/media", "boot/aarch64/linux", "boot/aarch64/initrd", "boot/aarch64/root"]
OPENSUSE_NET = ["media.1/media", "boot/x86_64/loader/linux", "boot/x86_64/loader/initrd", ".treeinfo"]
ALPINE = [
    ".alpine-release",
    "boot/vmlinuz-virt",
    "boot/initramfs-virt",
    "boot/modloop-virt",
    "apks/x86_64/APKINDEX.tar.gz",
]
WINDOWS = ["bootmgr", "boot/bcd", "boot/boot.sdi", "efi/boot/bootx64.efi", "sources/boot.wim", "setup.exe"]
WINDOWS_UPPER = ["BOOTMGR", "BOOT/BCD", "BOOT/BOOT.SDI", "EFI/BOOT/BOOTAA64.EFI", "SOURCES/BOOT.WIM", "SETUP.EXE"]
MEMTEST = ["boot/memtest64.bin", "EFI/BOOT/memtest.efi"]


def context(arch=X86_64, features=frozenset({"nfs", "smb"}), **overrides):
    values = dict(
        arch=arch,
        platform="efi",
        server="10.0.0.5",
        http_base="http://10.0.0.5:8080/files/abc",
        iso_url="http://10.0.0.5:8080/raw/abc.iso",
        nfs_path="/run/isovault/mnt/abc",
        iso_size=3 * 2**30,
        mac="52:54:00:aa:bb:cc",
        features=features,
    )
    values.update(overrides)
    return BootContext(**values)


@pytest.mark.parametrize(
    "files, expected",
    [
        (UBUNTU_DESKTOP, "ubuntu-casper"),
        (UBUNTU_SERVER, "ubuntu-server"),
        (UBUNTU_ARM, "ubuntu-server"),
        (DEBIAN_LIVE, "debian-live"),
        (KALI_LIVE, "debian-live"),
        (DEBIAN_NETINST, "debian-installer"),
        (FEDORA_SERVER, "anaconda"),
        (ROCKY_ARM, "anaconda"),
        (FEDORA_LIVE_NEW, "fedora-live"),
        (FEDORA_LIVE_OLD, "fedora-live"),
        (ARCH, "arch"),
        (OPENSUSE, "opensuse"),
        (OPENSUSE_ARM, "opensuse"),
        (OPENSUSE_NET, "opensuse-net"),
        (ALPINE, "alpine"),
        (WINDOWS, "windows"),
        (WINDOWS_UPPER, "windows"),
        (MEMTEST, "generic"),
        (["readme.txt"], "generic"),
    ],
)
def test_identifies_the_family(build_tree, profiles, files, expected):
    assert identify(Tree(build_tree(files)), profiles).name == expected


@pytest.mark.parametrize(
    "files, name, expected",
    [
        (UBUNTU_DESKTOP, "x.iso", (X86_64,)),
        (UBUNTU_ARM, "x.iso", (ARM64,)),
        (FEDORA_SERVER, "x.iso", (X86_64,)),
        (ROCKY_ARM, "x.iso", (ARM64,)),
        (WINDOWS_UPPER, "x.iso", (ARM64,)),
        (["install.a64/vmlinuz"], "x.iso", (ARM64,)),
        (ARCH, "x.iso", (X86_64,)),
        (OPENSUSE_ARM, "x.iso", (ARM64,)),
        (["a"], "tool-amd64.iso", (X86_64,)),
        (["a"], "tool_aarch64.iso", (ARM64,)),
        (["a"], "tool-x86_64.iso", (X86_64,)),
        (["a"], "foox64bar.iso", ()),
        (["a"], "plain.iso", ()),
    ],
)
def test_detects_the_architecture(build_tree, files, name, expected):
    assert detect_archs(Tree(build_tree(files)), name) == expected


def test_multi_arch_iso_lists_both(build_tree):
    tree = Tree(build_tree(["efi/boot/bootx64.efi", "efi/boot/bootaa64.efi"]))
    assert detect_archs(tree, "x.iso") == (ARM64, X86_64)


def test_markers_beat_a_misleading_filename(build_tree):
    tree = Tree(build_tree(["efi/boot/bootaa64.efi"]))
    assert detect_archs(tree, "tool-amd64.iso") == (ARM64,)


def test_lookups_ignore_case_and_return_the_real_path(build_tree):
    tree = Tree(build_tree(["Boot/BCD", "Sources/Boot.WIM"]))
    assert tree.find("boot/bcd") == "Boot/BCD"
    assert tree.find("sources/boot.wim") == "Sources/Boot.WIM"
    assert tree.find("sources/missing") is None


def test_a_trailing_slash_only_matches_directories(build_tree):
    tree = Tree(build_tree(["media.1/media", "install"]))
    assert tree.find("media.1/") == "media.1"
    assert tree.find("install/") is None


def plan_for(build_tree, profiles, files, ctx=None):
    tree = Tree(build_tree(files))
    profile = identify(tree, profiles)
    return profile, build_linux_plan(profile, tree, ctx or context())


def test_ubuntu_desktop_prefers_nfs(build_tree, profiles):
    _, plan = plan_for(build_tree, profiles, UBUNTU_DESKTOP)
    assert plan.kernel == "casper/vmlinuz"
    assert plan.initrds == ["casper/initrd"]
    assert "netboot=nfs" in plan.args
    assert "nfsroot=10.0.0.5:/run/isovault/mnt/abc" in plan.args
    assert plan.warnings == []


def test_ubuntu_desktop_falls_back_to_ram_without_nfs(build_tree, profiles):
    _, plan = plan_for(build_tree, profiles, UBUNTU_DESKTOP, context(features=frozenset()))
    assert "url=http://10.0.0.5:8080/raw/abc.iso" in plan.args
    assert "nfsroot" not in plan.args
    assert "6 GiB" in plan.warnings[0]


def test_ubuntu_server_prefers_nfs(build_tree, profiles):
    _, plan = plan_for(build_tree, profiles, UBUNTU_SERVER)
    assert "netboot=nfs" in plan.args
    assert "url=" not in plan.args
    assert plan.warnings == []


def test_ubuntu_server_falls_back_to_ram_without_nfs(build_tree, profiles):
    _, plan = plan_for(build_tree, profiles, UBUNTU_SERVER, context(features=frozenset()))
    assert "url=http://10.0.0.5:8080/raw/abc.iso" in plan.args
    assert "cloud-config-url=/dev/null" in plan.args
    assert "6 GiB" in plan.warnings[0]


def test_opensuse_dvd_installs_from_itself(build_tree, profiles):
    _, plan = plan_for(build_tree, profiles, OPENSUSE)
    assert "install=http://10.0.0.5:8080/files/abc/" in plan.args


def test_opensuse_net_doesnt_point_at_itself(build_tree, profiles):
    profile, plan = plan_for(build_tree, profiles, OPENSUSE_NET)
    assert "install=" not in plan.args
    assert profile.menu_note == "needs internet"


def test_debian_live_resolves_versioned_filenames(build_tree, profiles):
    _, plan = plan_for(build_tree, profiles, DEBIAN_LIVE)
    assert plan.kernel == "live/vmlinuz-6.12.0-amd64"
    assert plan.initrds == ["live/initrd.img-6.12.0-amd64"]


def test_arch_puts_microcode_first(build_tree, profiles):
    _, plan = plan_for(build_tree, profiles, ARCH)
    assert plan.initrds == [
        "arch/boot/intel-ucode.img",
        "arch/boot/amd-ucode.img",
        "arch/boot/x86_64/initramfs-linux.img",
    ]
    assert "archiso_http_srv=http://10.0.0.5:8080/files/abc/" in plan.args
    assert "BOOTIF=01-52-54-00-aa-bb-cc" in plan.args


def test_fedora_live_finds_the_new_layout(build_tree, profiles):
    _, plan = plan_for(build_tree, profiles, FEDORA_LIVE_NEW)
    assert plan.kernel == "boot/x86_64/loader/linux"
    assert "root=live:http://10.0.0.5:8080/files/abc/LiveOS/squashfs.img" in plan.args


def test_opensuse_kernel_path_follows_the_client_arch(build_tree, profiles):
    _, plan = plan_for(build_tree, profiles, OPENSUSE_ARM, context(arch=ARM64))
    assert plan.kernel == "boot/aarch64/linux"


def test_alpine_expands_globs_in_arguments(build_tree, profiles):
    _, plan = plan_for(build_tree, profiles, ALPINE)
    assert "modloop=http://10.0.0.5:8080/files/abc/boot/modloop-virt" in plan.args
    assert "alpine_repo=http://10.0.0.5:8080/files/abc/apks" in plan.args


def test_extra_arguments_are_appended(build_tree, profiles):
    _, plan = plan_for(build_tree, profiles, ALPINE, context(extra_args="console=ttyS0"))
    assert plan.args.endswith("console=ttyS0")


def test_missing_kernel_is_reported(build_tree, profiles):
    tree = Tree(build_tree(UBUNTU_DESKTOP))
    profile = identify(tree, profiles)
    (tree.root / "casper" / "vmlinuz").unlink()
    plan = build_linux_plan(profile, Tree(tree.root), context())
    assert "kernel" in plan.error


def test_user_profiles_override_built_in_ones(tmp_path, profiles):
    first = tmp_path / "builtin"
    second = tmp_path / "custom"
    first.mkdir()
    second.mkdir()
    body = 'name = "x"\nlabel = "%s"\npriority = 5\n[boot]\nmethod = "sanboot"\n'
    (first / "x.toml").write_text(body % "original")
    (second / "x.toml").write_text(body % "custom")
    loaded = load_profiles(first, second)
    assert [p.label for p in loaded] == ["custom"]


def test_a_broken_user_profile_is_skipped(tmp_path):
    (tmp_path / "bad.toml").write_text("this isn't toml [[[")
    (tmp_path / "incomplete.toml").write_text('name = "y"\n[boot]\nmethod = "linux"\n')
    (tmp_path / "good.toml").write_text('name = "z"\n[boot]\nmethod = "sanboot"\n')
    assert [p.name for p in load_profiles(tmp_path)] == ["z"]


def test_an_unknown_boot_method_is_rejected():
    with pytest.raises(ProfileError):
        parse_profile({"name": "x", "boot": {"method": "teleport"}}, "test")


def test_generic_stays_the_last_resort(profiles):
    assert profiles[-1].name == "generic"
    assert all(p.priority > 0 for p in profiles[:-1])
