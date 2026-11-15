import hashlib
import os
import sqlite3
from threading import Event

import pytest

from src import indexer as indexer_module
from src.indexer import Indexer, IndexInProgress, hash_file
from src.store import Book, ScanCancelled, Store

ABC_HASH = hashlib.sha256(b"abc").hexdigest()


def test_recursive_scan_filters_hashes_and_preserves_ids(library, store, tmp_path):
    (library / "nested").mkdir()
    for name in ["z.pdf", "nested/a.EPUB", "nested/z.pdf"]:
        (library / name).write_bytes(b"abc")
    for name in ["ignore.txt", "ignore.pdf.txt"]:
        (library / name).write_text("ignored")
    (library / "directory.pdf").mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_text("abc")
    (library / "linked.pdf").symlink_to(outside)
    (library / "linked-directory").symlink_to(tmp_path, target_is_directory=True)
    if hasattr(os, "mkfifo"):
        os.mkfifo(library / "pipe.pdf")
    root_link = tmp_path / "root-link"
    root_link.symlink_to(library, target_is_directory=True)
    indexer = Indexer(store, root_link)
    assert indexer.reindex() == 3
    before = store.books()
    assert [b.file_name for b in before] == ["a.EPUB", "z.pdf", "z.pdf"]
    assert all(b.id > 0 and b.sha256 == ABC_HASH for b in before)
    assert all(b.file_path.startswith(str(library.resolve())) for b in before)
    assert indexer.reindex() == 3
    assert store.books() == before


def test_updates_adds_removes_and_clears(library, store):
    keep = library / "keep.pdf"
    remove = library / "remove.epub"
    keep.write_text("abc")
    remove.write_text("abc")
    indexer = Indexer(store, library)
    assert indexer.reindex() == 2
    old_id = store.books()[0].id
    keep.write_text("changed")
    remove.unlink()
    added = library / "new.epub"
    added.write_text("abc")
    assert indexer.reindex() == 2
    books = store.books()
    assert books[0].id == old_id
    assert books[0].sha256 == hashlib.sha256(b"changed").hexdigest()
    assert books[1].file_path == str(added.resolve())
    keep.unlink()
    added.unlink()
    assert indexer.reindex() == 0
    assert store.books() == []


def test_failed_cancelled_and_missing_scans_preserve_index(library, store, monkeypatch):
    path = library / "book.pdf"
    path.write_text("abc")
    indexer = Indexer(store, library)
    indexer.reindex()
    before = store.books()
    cancel = Event()
    cancel.set()
    with pytest.raises(ScanCancelled):
        indexer.reindex(cancel)
    with monkeypatch.context() as patch:

        def fail(*args):
            raise PermissionError("Unable to read file")

        patch.setattr(indexer_module, "hash_file", fail)
        with pytest.raises(PermissionError):
            indexer.reindex()
    library.rename(library.with_name("moved"))
    with pytest.raises(FileNotFoundError):
        indexer.reindex()
    library.write_text("not a directory")
    with pytest.raises(OSError, match="must be a directory"):
        indexer.reindex()
    assert store.books() == before


def test_directory_scan_errors_preserve_index(library, store, monkeypatch):
    (library / "book.pdf").write_text("abc")
    indexer = Indexer(store, library)
    indexer.reindex()
    before = store.books()

    def failing_walk(root, *, onerror, followlinks):
        yield str(root), [], []
        onerror(PermissionError("unreadable subdirectory"))

    monkeypatch.setattr(indexer_module.os, "walk", failing_walk)
    with pytest.raises(PermissionError):
        indexer.reindex()
    assert store.books() == before


def test_rejects_overlap_and_releases_lock(library, store):
    indexer = Indexer(store, library)
    with indexer.lock, pytest.raises(IndexInProgress):
        indexer.reindex()
    assert indexer.reindex() == 0


def test_store_rollback_persistence_and_special_paths(tmp_path):
    path = tmp_path / "special ?# directory" / "index.db"
    store = Store(path)
    original = Book("book.pdf", "/library/book.pdf", ABC_HASH)
    store.sync_books([original])
    before = store.books()
    with pytest.raises(sqlite3.IntegrityError):
        store.sync_books(
            [
                Book(original.file_name, original.file_path, "0" * 64),
                Book("invalid.pdf", "/library/invalid.pdf", "invalid hash"),
            ]
        )
    assert store.books() == before
    store.sync_books([original])
    assert Store(path).books() == before


def test_cancellation_during_transaction_rolls_back(library, store, monkeypatch):
    import src.store as store_module

    original = Book("book.pdf", "/book.pdf", ABC_HASH)
    store.sync_books([original])
    before = store.books()
    calls = 0

    def cancel_during_save(cancel):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ScanCancelled()

    monkeypatch.setattr(store_module, "check_cancelled", cancel_during_save)
    with pytest.raises(ScanCancelled):
        store.sync_books([Book("new.pdf", "/new.pdf", ABC_HASH)])
    assert store.books() == before


def test_changed_file_is_rejected(library, monkeypatch):
    path = library / "book.pdf"
    path.write_text("abc")
    original_fstat = os.fstat
    calls = 0

    def modified_fstat(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text("a changed file")
        return original_fstat(fd)

    monkeypatch.setattr(indexer_module.os, "fstat", modified_fstat)
    with pytest.raises(OSError, match="File changed while hashing"):
        hash_file(path)


def test_hash_streams_large_file(library):
    path = library / "large.pdf"
    contents = b"abc123" * 400_000
    path.write_bytes(contents)
    assert hash_file(path) == hashlib.sha256(contents).hexdigest()


def test_original_database_schema_is_compatible(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE books (
                id INTEGER PRIMARY KEY,
                file_name TEXT NOT NULL,
                file_path TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL CHECK(length(sha256) = 64)
            );
        """)
        connection.execute(
            "INSERT INTO books VALUES (42, ?, ?, ?)",
            (
                "book.pdf",
                "/book.pdf",
                ABC_HASH,
            ),
        )
    store = Store(path)
    original = store.books()[0]
    assert original.id == 42
    store.sync_books([Book("book.pdf", "/book.pdf", "1" * 64)])
    assert store.books()[0].id == 42
