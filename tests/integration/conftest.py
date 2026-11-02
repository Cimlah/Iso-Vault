import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import harness  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def helper_image():
    harness.build_helper()


@pytest.fixture(scope="session")
def iso_volume(helper_image):
    """A shared, read-only set of test ISOs."""
    volume = f"iv-it-isos-{uuid.uuid4().hex[:8]}"
    harness.build_iso_volume(volume)
    yield volume
    harness.remove_volume(volume)


@pytest.fixture
def stack(iso_volume):
    instance = harness.Stack(iso_volume)
    yield instance
    instance.close()


@pytest.fixture
def private_isos(helper_image):
    """An ISO volume this test can change."""
    volume = f"iv-it-isos-{uuid.uuid4().hex[:8]}"
    docker_volume = harness.docker("volume", "create", volume)
    assert docker_volume.returncode == 0
    yield volume
    harness.remove_volume(volume)


@pytest.fixture
def private_stack(private_isos):
    instance = harness.Stack(private_isos)
    yield instance
    instance.close()


@pytest.fixture
def driver_volume():
    """Driver files laid out the way a download usually unpacks, with private permissions."""
    volume = f"iv-it-drivers-{uuid.uuid4().hex[:8]}"
    harness.docker("volume", "create", volume)
    harness.on_volume(
        volume,
        """
        mkdir -p x64/realtek arm64/qualcomm x64/messy
        printf inf-text > x64/realtek/rt25.inf
        printf sys-bytes > x64/realtek/rt25.sys
        printf cat-bytes > x64/realtek/rt25.cat
        printf no > x64/realtek/README.txt
        printf no > x64/realtek/setup.exe
        chmod 600 x64/realtek/rt25.sys
        printf arm-inf > arm64/qualcomm/qc.inf
        printf a > x64/messy/ok.inf
        printf b > "x64/messy/not ok.sys"
        """,
    )
    yield volume
    harness.remove_volume(volume)
