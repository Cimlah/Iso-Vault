import json

import pytest

from isovault.state import MAX_CLIENTS, State, StateError, normalize_mac

MAC = "52:54:00:aa:bb:cc"


def test_macs_are_normalized():
    assert normalize_mac("52-54-00-AA-BB-CC") == MAC
    assert normalize_mac(" 52:54:00:aa:bb:cc ") == MAC


@pytest.mark.parametrize("bad", ["", "nope", "52:54:00:aa:bb", "52:54:00:aa:bb:cc:dd", "gg:54:00:aa:bb:cc", "../../etc"])
def test_invalid_macs_are_rejected(bad):
    assert normalize_mac(bad) is None


def test_pins_survive_a_restart(tmp_path):
    path = tmp_path / "state.json"
    State(path).set_pin("52-54-00-AA-BB-CC", "ubuntu-1", "always")
    assert State(path).get_pin(MAC)["iso_id"] == "ubuntu-1"


def test_set_pin_rejects_bad_input(tmp_path):
    state = State(tmp_path / "state.json")
    with pytest.raises(StateError):
        state.set_pin("nope", "x")
    with pytest.raises(StateError):
        state.set_pin(MAC, "x", "forever")


def test_once_pin_is_used_up(tmp_path):
    state = State(tmp_path / "state.json")
    state.set_pin(MAC, "ubuntu-1", "once")
    state.consume_pin(MAC, "ubuntu-1")
    assert state.get_pin(MAC) is None
    assert State(tmp_path / "state.json").get_pin(MAC) is None


def test_always_pin_stays(tmp_path):
    state = State(tmp_path / "state.json")
    state.set_pin(MAC, "ubuntu-1", "always")
    state.consume_pin(MAC, "ubuntu-1")
    assert state.get_pin(MAC) is not None


def test_booting_another_iso_doesnt_use_up_a_once_pin(tmp_path):
    state = State(tmp_path / "state.json")
    state.set_pin(MAC, "ubuntu-1", "once")
    state.consume_pin(MAC, "fedora-2")
    assert state.get_pin(MAC) is not None


def test_clear_pin_reports_whether_it_existed(tmp_path):
    state = State(tmp_path / "state.json")
    state.set_pin(MAC, "ubuntu-1")
    assert state.clear_pin(MAC) is True
    assert state.clear_pin(MAC) is False


def test_client_sightings_are_batched_until_flush(tmp_path):
    path = tmp_path / "state.json"
    state = State(path)
    state.record_client(MAC, "x86_64", "efi", "10.0.0.9")
    assert not path.exists()
    state.flush()
    assert MAC in json.loads(path.read_text())["clients"]


def test_recording_keeps_first_seen_and_last_iso(tmp_path):
    times = iter([100.0, 200.0, 300.0])
    state = State(tmp_path / "state.json", clock=lambda: next(times))
    state.record_client(MAC, "x86_64", "efi", "10.0.0.9", iso_id="ubuntu-1")
    state.record_client(MAC, "x86_64", "efi", "10.0.0.10")
    client = state.snapshot()["clients"][MAC]
    assert client["first_seen"] == 100.0
    assert client["last_seen"] == 200.0
    assert client["ip"] == "10.0.0.10"
    assert client["last_iso"] == "ubuntu-1"


def test_client_list_is_bounded_and_keeps_pinned_devices(tmp_path):
    clock = iter(float(n) for n in range(10000))
    state = State(tmp_path / "state.json", clock=lambda: next(clock))
    pinned = "aa:aa:aa:aa:aa:aa"
    state.record_client(pinned, "x86_64", "efi", "10.0.0.1")
    state.set_pin(pinned, "ubuntu-1")
    for number in range(MAX_CLIENTS + 50):
        mac = f"52:54:00:00:{number >> 8:02x}:{number & 255:02x}"
        state.record_client(mac, "x86_64", "efi", "10.0.0.2")
    clients = state.snapshot()["clients"]
    assert len(clients) == MAX_CLIENTS
    assert pinned in clients


def test_a_corrupt_file_is_kept_aside(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ not json")
    state = State(path)
    assert state.snapshot() == {"pins": {}, "clients": {}}
    assert (tmp_path / "state.corrupt").read_text() == "{ not json"


def test_writes_leave_no_temp_file(tmp_path):
    state = State(tmp_path / "state.json")
    state.set_pin(MAC, "ubuntu-1")
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
