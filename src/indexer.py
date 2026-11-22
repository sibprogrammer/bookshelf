"""Scan completely before replacing the saved index in one transaction."""

import hashlib
import logging
import os
import stat
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock

from .covers import CoverError, Covers
from .goodreads import NO_RATING, GoodreadsRating, fetch_rating
from .metadata import read_metadata
from .store import BookFile, Store, check_cancelled, group_files

logger = logging.getLogger(__name__)
GOODREADS_REQUEST_PAUSE = 0.25


class IndexInProgress(Exception):
    pass


def file_added_at(info: os.stat_result) -> datetime | None:
    if sys.platform == "linux":
        return datetime.fromtimestamp(info.st_mtime, UTC)
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


def lookup_book_rating(
    files: list[BookFile], lookup: Callable[[str], GoodreadsRating | None]
) -> GoodreadsRating | None:
    isbns = dict.fromkeys(file.metadata.isbn for file in files if file.metadata.isbn)
    result = NO_RATING if isbns else None
    for isbn in isbns:
        rating = lookup(isbn)
        if rating is None:
            result = None
        elif rating.votes > 0:
            return rating
    return result


class Indexer:
    def __init__(self, store: Store, root: Path, *, goodreads_ratings: bool = True):
        self.store = store
        self.root = Path(root)
        self.goodreads_ratings = goodreads_ratings
        self.lock = Lock()
        self.covers = Covers(store.path.parent / "covers")
        self.cover_errors: list[str] = []
        self.progress: dict = {}

    def _library_root(self) -> Path:
        root = self.root.resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(f"books_dir must be a directory: {root}")
        return root

    @staticmethod
    def _read_file(path: Path, cancel: Event | None = None) -> BookFile:
        check_cancelled(cancel)
        digest = hash_file(path, cancel)
        return BookFile(
            file_path=str(path),
            extension=path.suffix[1:].lower(),
            sha256=digest,
            added_at=file_added_at(path.lstat()),
            metadata=read_metadata(path),
        )

    def _update_cover(
        self, files: list[BookFile], cancel: Event | None = None, *, force: bool = False
    ) -> tuple[Path | None, str]:
        try:
            return self.covers.ensure(files, cancel, force=force), ""
        except CoverError as error:
            logger.warning("Cover unavailable: %s", error)
            return None, str(error)

    def refresh_book(self, book_id: int) -> bool | None:
        """Refresh one book's file metadata, cover, and Goodreads rating."""
        if not self.lock.acquire(blocking=False):
            raise IndexInProgress("Indexing is already in progress")
        try:
            book = self.store.book(book_id)
            if book is None:
                return None
            root = self._library_root()
            files = []
            for file in book.files:
                path = Path(file.file_path)
                if not path.is_relative_to(root) or path.resolve(strict=True) != path:
                    raise FileNotFoundError(path)
                files.append(self._read_file(path))
            cover, _ = self._update_cover(files, force=True)
            rating = (
                lookup_book_rating(files, fetch_rating)
                if self.goodreads_ratings
                else None
            )
            if not self.store.refresh_book(book, files, goodreads_rating=rating):
                raise OSError("The indexed book changed during the refresh")
            return cover is not None
        finally:
            self.lock.release()

    def reindex(self, cancel: Event | None = None) -> int:
        if not self.lock.acquire(blocking=False):
            raise IndexInProgress("Indexing is already in progress")

        def report(phase, completed=0, total=0):
            self.progress = dict(phase=phase, completed=completed, total=total)

        try:
            check_cancelled(cancel)
            report("Finding books")
            root = self._library_root()
            scanned_files = []
            pending_files = []

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
                    pending_files.append(path)
            report("Indexing files", 0, len(pending_files))
            for position, path in enumerate(pending_files, start=1):
                scanned_files.append(self._read_file(path, cancel))
                report("Indexing files", position, len(pending_files))
            self.cover_errors = []
            groups = group_files(scanned_files)
            active_covers = set()
            report("Updating covers", 0, len(groups))
            for position, files in enumerate(groups.values(), start=1):
                cover, error = self._update_cover(files, cancel)
                if cover is not None:
                    active_covers.add(cover.name)
                else:
                    self.cover_errors.append(error)
                report("Updating covers", position, len(groups))
            ratings = {}
            if self.goodreads_ratings:
                checked_books = self.store.goodreads_checked_book_keys()
                cache: dict[str, GoodreadsRating | None] = {}
                stop = cancel if cancel is not None else Event()

                def lookup(isbn: str) -> GoodreadsRating | None:
                    if isbn not in cache:
                        if cache:
                            stop.wait(GOODREADS_REQUEST_PAUSE)
                        check_cancelled(stop)
                        cache[isbn] = fetch_rating(isbn)
                    return cache[isbn]

                unchecked_books = {
                    key: files
                    for key, files in groups.items()
                    if key not in checked_books
                }
                report("Fetching ratings", 0, len(unchecked_books))
                for position, (key, files) in enumerate(
                    unchecked_books.items(), start=1
                ):
                    check_cancelled(stop)
                    rating = lookup_book_rating(files, lookup)
                    if rating is not None:
                        ratings[key] = rating
                    report("Fetching ratings", position, len(unchecked_books))
            report("Saving library")
            count = self.store.sync_files(
                scanned_files, cancel, root=root, ratings=ratings
            )
            self.covers.prune(active_covers)
            return count
        finally:
            self.lock.release()
