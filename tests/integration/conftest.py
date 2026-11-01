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
