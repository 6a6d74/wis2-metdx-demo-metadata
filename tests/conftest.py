import copy
import json
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent


@pytest.fixture
def record():
    return copy.deepcopy(json.loads((FIXTURES / "valid-record.json").read_text()))


@pytest.fixture
def cfg():
    from wis2node import config
    return config.load(ROOT / "config" / "node.yml", env={})


class Repo:
    def __init__(self, path: Path):
        self.path = path
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.org")
        self.git("config", "user.name", "test")
        (path / "metadata").mkdir()
        (path / "metadata" / ".gitkeep").touch()
        self.commit("init")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.path, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, rel: str, data):
        f = self.path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(data if isinstance(data, str) else json.dumps(data, indent=2))

    def remove(self, rel: str):
        (self.path / rel).unlink()

    def commit(self, msg="change") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", msg)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)
