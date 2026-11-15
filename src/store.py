"""SQLite persistence for books and their PDF/EPUB files."""

import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from .metadata import Metadata, merge_metadata


class ScanCancelled(Exception):
    pass


def check_cancelled(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ScanCancelled("Indexing was cancelled")


@dataclass(frozen=True)
class BookFile:
    file_path: str
    extension: str
    sha256: str
    id: int = 0
    added_at: datetime | None = None
    metadata: Metadata = field(default_factory=Metadata)


@dataclass(frozen=True)
class Book:
    title: str
    directory: str
    files: tuple[BookFile, ...]
    id: int
    metadata: Metadata = field(default_factory=Metadata)

    @property
    def file_title(self) -> str:
        return Path(self.files[0].file_path).stem


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_stem TEXT NOT NULL,
                title TEXT NOT NULL,
                directory TEXT NOT NULL,
                UNIQUE(directory, file_stem)
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS book_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                extension TEXT NOT NULL CHECK(extension IN ('pdf', 'epub')),
                file_path TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
                added_at TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            )""")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS book_files_book_id ON book_files(book_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS book_files_added_at ON book_files(added_at)"
            )

    @contextmanager
    def connect(self):
        # Each operation owns its connection, including requests on other threads.
        with closing(sqlite3.connect(self.path, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection

    @staticmethod
    def _read_books(
        connection: sqlite3.Connection,
        book_id: int | None = None,
        *,
        recent: bool = False,
    ):
        query = ""
        if recent:
            # Limit books before joining their files, so every format is retained.
            query = (
                "WITH recent_books AS ("
                "SELECT b.id, MAX(f.added_at) AS latest_added_at "
                "FROM books b JOIN book_files f ON f.book_id = b.id "
                "GROUP BY b.id ORDER BY latest_added_at DESC, "
                "b.file_stem COLLATE NOCASE, b.directory, b.id LIMIT 20) "
            )
        query += (
            "SELECT b.id AS book_id, b.title, b.file_stem, b.directory, "
            "f.id AS file_id, "
            "f.file_path, f.extension, f.sha256, f.added_at, f.metadata "
            "FROM books b JOIN book_files f ON f.book_id = b.id "
        )
        if recent:
            query += "JOIN recent_books r ON r.id = b.id "
        parameters = ()
        if book_id is not None:
            query += "WHERE b.id = ? "
            parameters = (book_id,)
        query += "ORDER BY "
        if recent:
            query += "r.latest_added_at DESC, "
        query += (
            "b.file_stem COLLATE NOCASE, b.directory, b.id, "
            "CASE f.extension WHEN 'pdf' THEN 0 ELSE 1 END, f.file_path"
        )
        books = {}
        files = {}
        for row in connection.execute(query, parameters):
            key = row["book_id"]
            books[key] = (row["title"], row["directory"], row["file_stem"])
            files.setdefault(key, []).append(
                BookFile(
                    file_path=row["file_path"],
                    extension=row["extension"],
                    sha256=row["sha256"],
                    id=row["file_id"],
                    added_at=datetime.fromisoformat(row["added_at"])
                    if row["added_at"] is not None
                    else None,
                    metadata=Metadata(**json.loads(row["metadata"])),
                )
            )
        return [
            Book(
                title,
                directory,
                tuple(files[key]),
                key,
                metadata=merge_metadata(
                    [(file.extension, file.metadata) for file in files[key]], file_stem
                ),
            )
            for key, (title, directory, file_stem) in books.items()
        ]

    def books(self) -> list[Book]:
        with self.connect() as connection:
            return self._read_books(connection)

    def recent_books(self) -> list[Book]:
        with self.connect() as connection:
            return self._read_books(connection, recent=True)

    def book(self, book_id: int) -> Book | None:
        with self.connect() as connection:
            books = self._read_books(connection, book_id)
            return books[0] if books else None

    def sync_files(self, files: list[BookFile], cancel: Event | None = None) -> int:
        groups: dict[tuple[str, str], list[BookFile]] = {}
        for file in files:
            path = Path(file.file_path)
            groups.setdefault((str(path.parent), path.stem), []).append(file)
        with self.connect() as connection:
            check_cancelled(cancel)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TEMP TABLE scanned_paths (file_path TEXT PRIMARY KEY)"
            )
            for (directory, file_stem), group in groups.items():
                group.sort(key=lambda file: (file.extension != "pdf", file.file_path))
                metadata = merge_metadata(
                    [(file.extension, file.metadata) for file in group], file_stem
                )
                connection.execute(
                    "INSERT INTO books (file_stem, title, directory) VALUES (?, ?, ?) "
                    "ON CONFLICT(directory, file_stem) DO UPDATE SET "
                    "title = excluded.title",
                    (file_stem, metadata.title, directory),
                )
                book_id = connection.execute(
                    "SELECT id FROM books WHERE directory = ? AND file_stem = ?",
                    (directory, file_stem),
                ).fetchone()["id"]
                for file in group:
                    check_cancelled(cancel)
                    connection.execute(
                        "INSERT INTO book_files "
                        "(book_id, extension, file_path, sha256, added_at, metadata) "
                        "VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(file_path) DO UPDATE SET "
                        "book_id = excluded.book_id, extension = excluded.extension, "
                        "sha256 = excluded.sha256, added_at = excluded.added_at, "
                        "metadata = excluded.metadata",
                        (
                            book_id,
                            file.extension,
                            file.file_path,
                            file.sha256,
                            file.added_at.astimezone(UTC).isoformat(
                                timespec="microseconds"
                            )
                            if file.added_at is not None
                            else None,
                            json.dumps(asdict(file.metadata), ensure_ascii=False),
                        ),
                    )
                    connection.execute(
                        "INSERT INTO scanned_paths (file_path) VALUES (?)",
                        (file.file_path,),
                    )
            connection.execute(
                "DELETE FROM book_files WHERE file_path NOT IN "
                "(SELECT file_path FROM scanned_paths)"
            )
            connection.execute(
                "DELETE FROM books WHERE NOT EXISTS "
                "(SELECT 1 FROM book_files WHERE book_files.book_id = books.id)"
            )
            check_cancelled(cancel)
            return connection.execute("SELECT count(*) FROM books").fetchone()[0]
