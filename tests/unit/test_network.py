import socket
import struct

import pytest

from conftest import ROOT
from isovault.config import Config, ConfigError, load, load_secrets
from isovault.dhcp_probe import build_discover, parse_offer
from isovault.network import NetInfo, choose_interface, default_range, describe, port_free
from isovault.services import dnsmasq_config, render, write_exports

MAC = "02:00:00:00:00:01"
NET = NetInfo("eth0", "192.168.1.10", 24, "192.168.1.0", "255.255.255.0", "192.168.1.1", MAC)


def offer(xid, offered="192.168.1.50", server="192.168.1.1", message=2, op=2):
    header = struct.pack(
        "!BBBBIHH4s4s4s4s16s64s128s",
        op, 1, 6, 0, xid, 0, 0x8000,
        b"\0" * 4, socket.inet_aton(offered), b"\0" * 4, b"\0" * 4,
        b"\0" * 16, b"\0" * 64, b"\0" * 128,
    )
    options = b"\x63\x82\x53\x63" + bytes([53, 1, message, 54, 4]) + socket.inet_aton(server) + b"\xff"
    return header + options


def test_discover_is_well_formed_and_broadcast():
    packet = build_discover(MAC, 0x11223344)
    assert packet[0] == 1
    assert packet[4:8] == bytes.fromhex("11223344")
    assert packet[10:12] == b"\x80\x00"
    assert packet[28:34] == bytes.fromhex("020000000001")
    assert packet[236:240] == b"\x63\x82\x53\x63"
    assert bytes([53, 1, 1]) in packet[240:]
    assert len(packet) >= 300


def test_a_matching_offer_is_parsed():
    parsed = parse_offer(offer(7), 7)
    assert parsed.server == "192.168.1.1"
    assert parsed.offered_ip == "192.168.1.50"


@pytest.mark.parametrize(
    "packet",
    [
        offer(8),  # someone else's transaction
        offer(7, message=5),  # an ACK, not an offer
        offer(7, op=1),  # a request from another client
        offer(7, offered="0.0.0.0"),  # a ProxyDHCP server, which hands out no addresses
        b"",
        b"\x02" * 100,
    ],
)
def test_other_packets_are_ignored(packet):
    assert parse_offer(packet, 7) is None


def test_truncated_options_dont_crash():
    # The server ID is cut short, but it's still an offer that server mode must respect.
    assert parse_offer(offer(7)[:-3], 7).server == "unknown"
    assert parse_offer(offer(7)[:241], 7) is None


def test_prefers_the_default_route_interface():
    routes = [{"dst": "10.0.0.0/8", "dev": "wg0"}, {"dst": "default", "dev": "enp3s0"}]
    assert choose_interface(routes, []) == "enp3s0"


def test_without_a_default_route_skips_virtual_interfaces():
    inet = [{"family": "inet", "local": "10.0.0.1", "prefixlen": 24}]
    links = [
        {"ifname": "lo", "addr_info": inet},
        {"ifname": "docker0", "addr_info": inet},
        {"ifname": "br-abc", "addr_info": inet},
        {"ifname": "eth1", "addr_info": []},
        {"ifname": "eth0", "addr_info": inet},
    ]
    assert choose_interface([], links) == "eth0"


def test_describe_computes_the_network():
    link = {
        "ifname": "eth0",
        "address": MAC,
        "addr_info": [{"family": "inet", "local": "192.168.1.10", "prefixlen": 24}],
    }
    info = describe(link, "192.168.1.1")
    assert (info.network, info.netmask, info.prefix) == ("192.168.1.0", "255.255.255.0", 24)


def test_describe_honours_a_chosen_address():
    link = {
        "ifname": "eth0",
        "addr_info": [
            {"family": "inet", "local": "192.168.1.10", "prefixlen": 24},
            {"family": "inet", "local": "10.9.9.9", "prefixlen": 16},
        ],
    }
    assert describe(link, "", "10.9.9.9").ip == "10.9.9.9"
    with pytest.raises(ConfigError):
        describe(link, "", "172.16.0.1")


@pytest.mark.parametrize(
    "ip, prefix, expected",
    [
        ("192.168.1.10", 24, ("192.168.1.100", "192.168.1.200")),
        ("10.20.30.40", 16, ("10.20.30.100", "10.20.30.200")),
        ("192.168.1.10", 28, ("192.168.1.5", "192.168.1.14")),
    ],
)
def test_default_range(ip, prefix, expected):
    assert default_range(ip, prefix) == expected


def test_a_tiny_network_needs_an_explicit_range():
    with pytest.raises(ConfigError):
        default_range("192.168.1.1", 30)


def test_port_free_detects_a_listener():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert port_free(port, "tcp", "127.0.0.1") is False
    assert port_free(port, "tcp", "127.0.0.1") is True


def test_render_fills_placeholders_and_leaves_other_syntax():
    assert render("$host @@A@@ ${mac}", {"A": "x"}) == "$host x ${mac}"
    with pytest.raises(KeyError):
        render("@@MISSING@@", {})


def config_for(**overrides):
    return Config(app_dir=ROOT, **overrides)


def test_proxy_config_hands_out_no_leases():
    text = dnsmasq_config(config_for(), NET, "proxy")
    assert "dhcp-range=192.168.1.0,proxy,255.255.255.0" in text
    assert "dhcp-leasefile" not in text


def test_proxy_config_serves_firmware_and_ipxe_clients_differently():
    text = dnsmasq_config(config_for(), NET, "proxy")
    for service in ("ARM64_EFI", "X86-64_EFI", "BC_EFI", "x86PC"):
        assert f"pxe-service=tag:!ipxe,{service}," in text
    assert "dhcp-userclass=set:ipxe,iPXE" in text
    assert "dhcp-boot=tag:ipxe,boot.ipxe,,192.168.1.10" in text


def test_server_config_hands_out_leases_and_boot_files():
    text = dnsmasq_config(config_for(), NET, "server")
    assert "dhcp-range=192.168.1.100,192.168.1.200,255.255.255.0,12h" in text
    assert "dhcp-option=option:router,192.168.1.1" in text
    assert "dhcp-boot=tag:bios,undionly.kpxe,,192.168.1.10" in text
    assert "dhcp-boot=tag:x64,snponly-x64.efi,,192.168.1.10" in text
    assert "dhcp-boot=tag:arm64,snponly-arm64.efi,,192.168.1.10" in text
    assert "http://192.168.1.10:8080/loaders/snponly-arm64.efi" in text


def test_server_config_honours_overrides():
    settings = config_for(
        dhcp_range_start="192.168.1.20",
        dhcp_range_end="192.168.1.30",
        dhcp_gateway="192.168.1.254",
        dhcp_dns="1.1.1.1",
        dhcp_lease_time="1h",
    )
    text = dnsmasq_config(settings, NET, "server")
    assert "dhcp-range=192.168.1.20,192.168.1.30,255.255.255.0,1h" in text
    assert "option:router,192.168.1.254" in text
    assert "option:dns-server,1.1.1.1" in text


def test_upstream_config_has_no_dhcp():
    text = dnsmasq_config(config_for(), NET, "upstream")
    assert "enable-tftp" in text
    assert "dhcp-range" not in text
    assert "dhcp-boot" not in text


def test_exports_are_read_only():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "exports"
        write_exports(path, ["/b", "/a"])
        assert path.read_text() == "/a (ro,no_root_squash,insecure)\n/b (ro,no_root_squash,insecure)\n"


def test_config_rejects_invalid_values():
    with pytest.raises(ConfigError):
        load({"DHCP_MODE": "maybe"})
    with pytest.raises(ConfigError):
        load({"BOOT_PORT": "seventy"})
    with pytest.raises(ConfigError):
        load({"BOOT_PORT": "70000"})
    with pytest.raises(ConfigError):
        load({"BOOT_PORT": "9000", "UI_PORT": "9000"})


def test_config_defaults_to_the_safe_mode():
    assert load({}).dhcp_mode == "proxy"
    assert load({}).dhcp_force is False


def test_secrets_are_generated_once_and_kept_private(tmp_path):
    first = load_secrets(tmp_path, {})
    second = load_secrets(tmp_path, {})
    assert first == second
    assert first.admin_password and first.session_secret and first.smb_password
    assert first.smb_password.isalnum()
    assert (tmp_path / "secrets.json").stat().st_mode & 0o077 == 0


def test_secrets_from_the_environment_win(tmp_path):
    load_secrets(tmp_path, {})
    secrets = load_secrets(tmp_path, {"ADMIN_PASSWORD": "from-env"})
    assert secrets.admin_password == "from-env"
    assert load_secrets(tmp_path, {}).admin_password == "from-env"


def test_an_smb_password_with_shell_characters_is_refused(tmp_path):
    with pytest.raises(ConfigError):
        load_secrets(tmp_path, {"SMB_PASSWORD": "a&b"})
