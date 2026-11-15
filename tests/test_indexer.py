import hashlib
import os
import sqlite3
from threading import Event

import pytest

from src import indexer as indexer_module
from src.indexer import Indexer, IndexInProgress, hash_file
from src.store import BookFile, ScanCancelled, Store

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
    assert [b.title for b in before] == ["a", "z", "z"]
    assert all(b.id > 0 and b.files[0].sha256 == ABC_HASH for b in before)
    assert all(b.directory.startswith(str(library.resolve())) for b in before)
    assert before[0].files[0].extension == "epub"
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
    assert books[0].files[0].sha256 == hashlib.sha256(b"changed").hexdigest()
    assert books[1].files[0].file_path == str(added.resolve())
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
    original = BookFile("/library/book.pdf", "pdf", ABC_HASH)
    store.sync_files([original])
    before = store.books()
    with pytest.raises(sqlite3.IntegrityError):
        store.sync_files(
            [
                BookFile(original.file_path, "pdf", "0" * 64),
                BookFile("/library/invalid.pdf", "pdf", "invalid hash"),
            ]
        )
    assert store.books() == before
    store.sync_files([original])
    assert Store(path).books() == before


def test_cancellation_during_transaction_rolls_back(library, store, monkeypatch):
    import src.store as store_module

    original = BookFile("/book.pdf", "pdf", ABC_HASH)
    store.sync_files([original])
    before = store.books()
    calls = 0

    def cancel_during_save(cancel):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ScanCancelled()

    monkeypatch.setattr(store_module, "check_cancelled", cancel_during_save)
    with pytest.raises(ScanCancelled):
        store.sync_files([BookFile("/new.pdf", "pdf", ABC_HASH)])
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


def test_formats_share_one_book_and_keep_individual_hashes(library, store):
    pdf = library / "A book.with.dots.PDF"
    epub = library / "A book.with.dots.ePuB"
    pdf.write_text("PDF contents")
    epub.write_text("EPUB contents")
    indexer = Indexer(store, library)
    assert indexer.reindex() == 1
    books = store.books()
    assert len(books) == 1
    book = books[0]
    assert book.title == "A book.with.dots"
    assert [file.extension for file in book.files] == ["pdf", "epub"]
    assert [file.sha256 for file in book.files] == [
        hashlib.sha256(b"PDF contents").hexdigest(),
        hashlib.sha256(b"EPUB contents").hexdigest(),
    ]
    assert store.book(book.id) == book
    assert indexer.reindex() == 1
    assert store.books() == books


def test_adding_and_removing_formats_preserves_book_and_remaining_file(library, store):
    pdf = library / "Book.pdf"
    epub = library / "Book.epub"
    pdf.write_text("PDF")
    indexer = Indexer(store, library)
    indexer.reindex()
    original = store.books()[0]
    epub.write_text("EPUB")
    assert indexer.reindex() == 1
    both = store.books()[0]
    assert both.id == original.id
    assert both.files[0] == original.files[0]
    pdf.unlink()
    assert indexer.reindex() == 1
    remaining = store.books()[0]
    assert remaining.id == original.id
    assert remaining.files == (both.files[1],)
    epub.unlink()
    assert indexer.reindex() == 0
    assert store.book(original.id) is None
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM book_files").fetchone()[0] == 0


def test_identical_titles_in_different_directories_are_distinct(library, store):
    (library / "one").mkdir()
    (library / "two").mkdir()
    for name in ["one/Book.pdf", "one/Book.epub", "two/Book.epub"]:
        (library / name).write_text("abc")
    assert Indexer(store, library).reindex() == 2
    books = store.books()
    assert [book.title for book in books] == ["Book", "Book"]
    assert [len(book.files) for book in books] == [2, 1]
    assert books[0].directory != books[1].directory
