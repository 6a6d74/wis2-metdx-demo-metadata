"""Find the metadata records added, modified or deleted between two commits."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

METADATA_DIR = "metadata"
ZERO_SHA = "0" * 40

ADDED, MODIFIED, DELETED = "A", "M", "D"


class GitError(Exception):
    """A git command failed."""


@dataclass(frozen=True)
class Change:
    status: str  # ADDED, MODIFIED or DELETED
    path: str


def git(*args: str, cwd: str | None = None) -> str:
    try:
        return subprocess.run(["git", *args], check=True, capture_output=True,
                              text=True, cwd=cwd).stdout
    except subprocess.CalledProcessError as exc:
        raise GitError(f"git {' '.join(args)} failed: {exc.stderr.strip()}") from None


def is_record_path(path: str) -> bool:
    """Records live directly in metadata/ as *.json."""
    parts = path.split("/")
    return len(parts) == 2 and parts[0] == METADATA_DIR and parts[1].endswith(".json")


def changed_records(base: str, head: str, cwd: str | None = None) -> list[Change]:
    """Record changes from base to head. Renames are reported as delete + add.

    An empty or all-zero base (e.g. the first push to a branch) means every
    record at head counts as added.
    """
    if not base or base == ZERO_SHA:
        out = git("ls-tree", "-r", "--name-only", head, "--", METADATA_DIR, cwd=cwd)
        return [Change(ADDED, p) for p in out.splitlines() if is_record_path(p)]

    out = git("diff", "--name-status", "--no-renames", base, head, "--", METADATA_DIR, cwd=cwd)
    changes = []
    for line in out.splitlines():
        status, path = line.split("\t", 1)
        status = status[0]
        if status == "T":  # type change (e.g. file <-> symlink): treat as a modification
            status = MODIFIED
        if is_record_path(path) and status in (ADDED, MODIFIED, DELETED):
            changes.append(Change(status, path))
    return changes


def other_paths(base: str, head: str, cwd: str | None = None) -> list[str]:
    """Paths changed under metadata/ that are not valid record locations."""
    if not base or base == ZERO_SHA:
        return []
    out = git("diff", "--name-only", "--no-renames", base, head, "--", METADATA_DIR, cwd=cwd)
    return [p for p in out.splitlines()
            if not is_record_path(p) and p != f"{METADATA_DIR}/.gitkeep"]


def show(rev: str, path: str, cwd: str | None = None) -> bytes:
    """File contents at a revision."""
    try:
        return subprocess.run(["git", "show", f"{rev}:{path}"], check=True,
                              capture_output=True, cwd=cwd).stdout
    except subprocess.CalledProcessError as exc:
        raise GitError(f"git show {rev}:{path} failed: {exc.stderr.decode().strip()}") from None
