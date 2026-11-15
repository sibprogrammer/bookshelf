"""SQLite persistence using the existing schema and stable book IDs."""

import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event


class ScanCancelled(Exception):
    pass


def check_cancelled(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ScanCancelled("Indexing was cancelled")


@dataclass(frozen=True)
class Book:
    file_name: str
    file_path: str
    sha256: str
    id: int = 0


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY,
                file_name TEXT NOT NULL,
                file_path TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL CHECK(length(sha256) = 64)
            )""")

    @contextmanager
    def connect(self):
        # Each operation owns its connection, including requests on other threads.
        with closing(sqlite3.connect(self.path, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            with connection:
                yield connection

    def books(self) -> list[Book]:
        with self.connect() as connection:
            return [
                Book(**row)
                for row in connection.execute(
                    "SELECT id, file_name, file_path, sha256 FROM books "
                    "ORDER BY file_name COLLATE NOCASE, file_path"
                )
            ]

    def book(self, book_id: int) -> Book | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, file_name, file_path, sha256 FROM books WHERE id = ?",
                (book_id,),
            ).fetchone()
            return Book(**row) if row is not None else None

    def sync_books(self, books: list[Book], cancel: Event | None = None) -> None:
        with self.connect() as connection:
            check_cancelled(cancel)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TEMP TABLE scanned_paths (file_path TEXT PRIMARY KEY)"
            )
            for book in books:
                check_cancelled(cancel)
                connection.execute(
                    "INSERT INTO books (file_name, file_path, sha256) VALUES (?, ?, ?) "
                    "ON CONFLICT(file_path) DO UPDATE SET "
                    "file_name = excluded.file_name, sha256 = excluded.sha256",
                    (book.file_name, book.file_path, book.sha256),
                )
                connection.execute(
                    "INSERT INTO scanned_paths (file_path) VALUES (?)",
                    (book.file_path,),
                )
            connection.execute(
                "DELETE FROM books WHERE file_path NOT IN "
                "(SELECT file_path FROM scanned_paths)"
            )
            check_cancelled(cancel)
