"""SQLite persistence for books and their PDF/EPUB files."""

import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event


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


@dataclass(frozen=True)
class Book:
    title: str
    directory: str
    files: tuple[BookFile, ...]
    id: int


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                directory TEXT NOT NULL,
                UNIQUE(directory, title)
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS book_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                extension TEXT NOT NULL CHECK(extension IN ('pdf', 'epub')),
                file_path TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
                added_at TEXT
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
                "b.title COLLATE NOCASE, b.directory, b.id LIMIT 20) "
            )
        query += (
            "SELECT b.id AS book_id, b.title, b.directory, f.id AS file_id, "
            "f.file_path, f.extension, f.sha256, f.added_at "
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
            "b.title COLLATE NOCASE, b.directory, b.id, "
            "CASE f.extension WHEN 'pdf' THEN 0 ELSE 1 END, f.file_path"
        )
        books = {}
        files = {}
        for row in connection.execute(query, parameters):
            key = row["book_id"]
            books[key] = (row["title"], row["directory"])
            files.setdefault(key, []).append(
                BookFile(
                    file_path=row["file_path"],
                    extension=row["extension"],
                    sha256=row["sha256"],
                    id=row["file_id"],
                    added_at=datetime.fromisoformat(row["added_at"])
                    if row["added_at"] is not None
                    else None,
                )
            )
        return [
            Book(title, directory, tuple(files[key]), key)
            for key, (title, directory) in books.items()
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
        with self.connect() as connection:
            check_cancelled(cancel)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TEMP TABLE scanned_paths (file_path TEXT PRIMARY KEY)"
            )
            for file in files:
                check_cancelled(cancel)
                path = Path(file.file_path)
                connection.execute(
                    "INSERT INTO books (title, directory) VALUES (?, ?) "
                    "ON CONFLICT(directory, title) DO NOTHING",
                    (path.stem, str(path.parent)),
                )
                book_id = connection.execute(
                    "SELECT id FROM books WHERE directory = ? AND title = ?",
                    (str(path.parent), path.stem),
                ).fetchone()["id"]
                connection.execute(
                    "INSERT INTO book_files "
                    "(book_id, extension, file_path, sha256, added_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(file_path) DO UPDATE SET "
                    "book_id = excluded.book_id, extension = excluded.extension, "
                    "sha256 = excluded.sha256, added_at = excluded.added_at",
                    (
                        book_id,
                        file.extension,
                        file.file_path,
                        file.sha256,
                        file.added_at.astimezone(UTC).isoformat(timespec="microseconds")
                        if file.added_at is not None
                        else None,
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
