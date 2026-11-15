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
class Category:
    id: int
    name: str


@dataclass(frozen=True)
class Book:
    title: str
    directory: str
    files: tuple[BookFile, ...]
    id: int
    category: Category
    metadata: Metadata = field(default_factory=Metadata)

    @property
    def file_title(self) -> str:
        return Path(self.files[0].file_path).stem

    @property
    def added_at(self) -> datetime | None:
        return next(
            (file.added_at for file in self.files if file.added_at is not None), None
        )


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_stem TEXT NOT NULL,
                title TEXT NOT NULL,
                directory TEXT NOT NULL,
                category_id INTEGER NOT NULL REFERENCES categories(id),
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
                "CREATE INDEX IF NOT EXISTS books_category_id ON books(category_id)"
            )
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
        random_picks: bool = False,
        category_id: int | None = None,
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
        elif random_picks:
            query = (
                "WITH random_books AS (SELECT id FROM books "
                "WHERE EXISTS (SELECT 1 FROM book_files WHERE book_id = books.id) "
                "ORDER BY RANDOM() LIMIT 5) "
            )
        query += (
            "SELECT b.id AS book_id, b.title, b.file_stem, b.directory, "
            "c.id AS category_id, c.name AS category_name, "
            "f.id AS file_id, "
            "f.file_path, f.extension, f.sha256, f.added_at, f.metadata "
            "FROM books b JOIN book_files f ON f.book_id = b.id "
            "JOIN categories c ON c.id = b.category_id "
        )
        if recent:
            query += "JOIN recent_books r ON r.id = b.id "
        elif random_picks:
            query += "JOIN random_books r ON r.id = b.id "
        conditions = []
        parameters = []
        if book_id is not None:
            conditions.append("b.id = ?")
            parameters.append(book_id)
        if category_id is not None:
            conditions.append("b.category_id = ?")
            parameters.append(category_id)
        if conditions:
            query += "WHERE " + " AND ".join(conditions) + " "
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
            books[key] = (
                row["title"],
                row["directory"],
                row["file_stem"],
                Category(row["category_id"], row["category_name"]),
            )
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
                category=category,
                metadata=merge_metadata(
                    [(file.extension, file.metadata) for file in files[key]], file_stem
                ),
            )
            for key, (title, directory, file_stem, category) in books.items()
        ]

    def books(self, category_id: int | None = None) -> list[Book]:
        with self.connect() as connection:
            return self._read_books(connection, category_id=category_id)

    def categories(self) -> list[Category]:
        with self.connect() as connection:
            return [
                Category(**row)
                for row in connection.execute(
                    "SELECT id, name FROM categories ORDER BY name COLLATE NOCASE, name"
                )
            ]

    def category_book_counts(self) -> dict[int, int]:
        with self.connect() as connection:
            return dict(
                connection.execute(
                    "SELECT category_id, COUNT(*) FROM books GROUP BY category_id"
                )
            )

    def category(self, category_id: int) -> Category | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, name FROM categories WHERE id = ?", (category_id,)
            ).fetchone()
            return Category(**row) if row is not None else None

    def recent_books(self) -> list[Book]:
        with self.connect() as connection:
            return self._read_books(connection, recent=True)

    def random_books(self) -> list[Book]:
        with self.connect() as connection:
            return self._read_books(connection, random_picks=True)

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
                folder = Path(group[0].file_path).parent
                category_name = folder.name or folder.anchor or "Uncategorized"
                connection.execute(
                    "INSERT INTO categories (name) VALUES (?) "
                    "ON CONFLICT(name) DO NOTHING",
                    (category_name,),
                )
                category_id = connection.execute(
                    "SELECT id FROM categories WHERE name = ?",
                    (category_name,),
                ).fetchone()["id"]
                metadata = merge_metadata(
                    [(file.extension, file.metadata) for file in group], file_stem
                )
                connection.execute(
                    "INSERT INTO books (file_stem, title, directory, category_id) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(directory, file_stem) DO UPDATE SET "
                    "title = excluded.title, category_id = excluded.category_id",
                    (file_stem, metadata.title, directory, category_id),
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
            connection.execute(
                "DELETE FROM categories WHERE NOT EXISTS "
                "(SELECT 1 FROM books WHERE books.category_id = categories.id)"
            )
            check_cancelled(cancel)
            return connection.execute("SELECT count(*) FROM books").fetchone()[0]
