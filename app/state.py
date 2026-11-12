"""Copy Harness state between the local DSH_HOME and the sessionStorage mount.

dsh writes session logs with link()-based publication, which sessionStorage
does not support, so the live home stays on local disk. After each run the
home is mirrored to the mount; a fresh microVM restores it before dsh starts.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

# Top-level entries owned by the image (profiles) or regenerable (cache).
EXCLUDED_TOP_LEVEL = frozenset({"profiles", "cache"})
EXCLUDED_NAMES = frozenset({"session.lock", ".credentials.yaml"})
TMP_SUFFIX = ".sync-tmp"


def _skip(rel: Path) -> bool:
    if rel.parts and rel.parts[0] in EXCLUDED_TOP_LEVEL:
        return True
    return rel.name in EXCLUDED_NAMES or rel.name.endswith(TMP_SUFFIX)


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        rel_dir = base.relative_to(root)
        if rel_dir.parts and rel_dir.parts[0] in EXCLUDED_TOP_LEVEL:
            dirnames[:] = []
            continue
        for name in filenames:
            path = base / name
            rel = path.relative_to(root)
            if _skip(rel) or path.is_symlink() or not path.is_file():
                continue
            yield path, rel


def _same(src: os.stat_result, dst: Path) -> bool:
    try:
        st = dst.stat()
    except FileNotFoundError:
        return False
    return st.st_size == src.st_size and int(st.st_mtime) == int(src.st_mtime)


def sync_tree(src: Path, dst: Path) -> int:
    """Copy new or changed files from src to dst. Never deletes. Returns files copied."""
    if not src.is_dir():
        return 0
    copied = 0
    for path, rel in _iter_files(src):
        target = dst / rel
        st = path.stat()
        if _same(st, target):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + TMP_SUFFIX)
        shutil.copy2(path, tmp)
        os.replace(tmp, target)
        copied += 1
    return copied


def has_sessions(dsh_home: Path) -> bool:
    sessions = dsh_home / "sessions"
    if not sessions.is_dir():
        return False
    return any(True for _ in _iter_files(sessions))


def restore(state_dir: Path, dsh_home: Path) -> int:
    count = sync_tree(state_dir, dsh_home)
    if count:
        log.info("restored %d state files from %s", count, state_dir)
    return count


def persist(dsh_home: Path, state_dir: Path) -> int:
    count = sync_tree(dsh_home, state_dir)
    log.info("persisted %d state files to %s", count, state_dir)
    return count
