import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from isovault.profiles import load_profiles  # noqa: E402


@pytest.fixture(scope="session")
def profiles():
    return load_profiles(ROOT / "isovault" / "profiles")


def make_tree(root: Path, files) -> Path:
    """Build a fake ISO. Paths ending in / become directories; a tuple gives a file's content."""
    root.mkdir(parents=True, exist_ok=True)
    for item in files:
        path, content = item if isinstance(item, tuple) else (item, "")
        target = root / path
        if path.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    return root


@pytest.fixture
def build_tree(tmp_path):
    counter = {"n": 0}

    def build(files):
        counter["n"] += 1
        return make_tree(tmp_path / f"iso{counter['n']}", files)

    return build
