"""Scan completely before replacing the saved index in one transaction."""

import hashlib
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock

from .store import BookFile, Store, check_cancelled


class IndexInProgress(Exception):
    pass


def file_added_at(info: os.stat_result) -> datetime | None:
    birthtime = getattr(info, "st_birthtime", None)
    if birthtime is None:
        return None
    return datetime.fromtimestamp(birthtime, UTC)


def hash_file(path: Path, cancel: Event | None = None) -> str:
    check_cancelled(cancel)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise OSError(f"File is no longer a regular file: {path}")
        digest = hashlib.sha256()
        size = 0
        while True:
            check_cancelled(cancel)
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(source.fileno())
        current = path.lstat()
        if (
            size != before.st_size
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or current.st_ino != after.st_ino
            or current.st_dev != after.st_dev
            or not stat.S_ISREG(current.st_mode)
        ):
            raise OSError(f"File changed while hashing; please reindex again: {path}")
        return digest.hexdigest()


class Indexer:
    def __init__(self, store: Store, root: Path):
        self.store = store
        self.root = Path(root)
        self.lock = Lock()

    def reindex(self, cancel: Event | None = None) -> int:
        if not self.lock.acquire(blocking=False):
            raise IndexInProgress("Indexing is already in progress")
        try:
            check_cancelled(cancel)
            root = self.root.resolve(strict=True)
            if not root.is_dir():
                raise OSError(f"books_dir must be a directory: {root}")
            scanned_files = []

            def fail(error: OSError) -> None:
                raise error

            for directory, dirs, files in os.walk(
                root, onerror=fail, followlinks=False
            ):
                check_cancelled(cancel)
                dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
                for name in files:
                    check_cancelled(cancel)
                    path = Path(directory) / name
                    if path.suffix.lower() not in {".pdf", ".epub"}:
                        continue
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    scanned_files.append(
                        BookFile(
                            file_path=str(path),
                            extension=path.suffix[1:].lower(),
                            sha256=hash_file(path, cancel),
                            added_at=file_added_at(info),
                        )
                    )
            return self.store.sync_files(scanned_files, cancel)
        finally:
            self.lock.release()
