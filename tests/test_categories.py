import sqlite3
from pathlib import Path
from threading import Event

import pytest

from src.indexer import Indexer
from src.store import BookFile, ScanCancelled, Store

SHA256 = "a" * 64


def test_folder_names_create_categories_and_grouped_books_link_once(store):
    assert store.categories() == []
    store.sync_files(
        [
            BookFile("/books/Go/A.epub", "epub", SHA256),
            BookFile("/books/Go/A.pdf", "pdf", "b" * 64),
            BookFile("/books/Go/B.pdf", "pdf", "c" * 64),
            BookFile("/books/Python/C.epub", "epub", "d" * 64),
        ]
    )
    categories = store.categories()
    assert [category.name for category in categories] == ["Go", "Python"]
    go, python = categories
    assert store.category_book_counts() == {go.id: 2, python.id: 1}
    assert store.category(go.id) == go
    assert store.category(99999) is None
    assert [book.file_title for book in store.books(category_id=go.id)] == ["A", "B"]
    assert [book.file_title for book in store.books(category_id=python.id)] == ["C"]
    assert store.books(category_id=99999) == []
    books = store.books()
    assert len(books) == 3
    assert len(books[0].files) == 2
    for book in books:
        assert book.category.name == Path(book.files[0].file_path).parent.name
        assert book.category == store.category(book.category.id)
        assert store.book(book.id) == book
    assert Store(store.path).books() == books
    with store.connect() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM categories WHERE id = ?", (go.id,))


def test_equal_folder_names_share_category_and_nested_folders_use_immediate_parent(
    store,
):
    store.sync_files(
        [
            BookFile("/books/Go/A.pdf", "pdf", SHA256),
            BookFile("/archive/Go/A.epub", "epub", "b" * 64),
            BookFile("/books/Programming/Python/B.pdf", "pdf", "c" * 64),
        ]
    )
    go, python = store.categories()
    assert [go.name, python.name] == ["Go", "Python"]
    assert len(store.books(category_id=go.id)) == 2
    assert len(store.books(category_id=python.id)) == 1


def test_reindex_preserves_category_ids_and_removes_empty_categories(library, store):
    go = library / "Go"
    python = library / "Python"
    go.mkdir()
    python.mkdir()
    (go / "Book.pdf").write_text("pdf")
    (go / "Book.epub").write_text("epub")
    (python / "Other.pdf").write_text("pdf")
    indexer = Indexer(store, library)
    assert indexer.reindex() == 2
    before = store.categories()
    assert indexer.reindex() == 2
    assert store.categories() == before
    (python / "Other.pdf").unlink()
    assert indexer.reindex() == 1
    assert store.categories() == [before[0]]
    (go / "Book.pdf").unlink()
    (go / "Book.epub").unlink()
    assert indexer.reindex() == 0
    assert store.categories() == []


def test_failed_or_cancelled_save_keeps_categories_and_books(store):
    original = BookFile("/Go/Book.pdf", "pdf", SHA256)
    store.sync_files([original])
    books = store.books()
    categories = store.categories()
    with pytest.raises(sqlite3.IntegrityError):
        store.sync_files([original, BookFile("/New/Invalid.pdf", "pdf", "invalid")])
    assert store.books() == books
    assert store.categories() == categories
    cancel = Event()
    cancel.set()
    with pytest.raises(ScanCancelled):
        store.sync_files([BookFile("/New/Book.pdf", "pdf", SHA256)], cancel)
    assert store.categories() == categories


def test_categories_navigation_browsing_and_book_details(app, client):
    store = app.extensions["store"]
    store.sync_files(
        [
            BookFile("/Go/A.pdf", "pdf", SHA256),
            BookFile("/Go/A.epub", "epub", "b" * 64),
            BookFile("/Python/B.pdf", "pdf", "c" * 64),
        ]
    )
    go, python = store.categories()
    response = client.get("/categories")
    assert response.status_code == 200
    assert b'href="/categories" aria-current="page">Categories</a>' in response.data
    assert f'href="/categories/{go.id}">Go (1)</a>'.encode() in response.data
    assert f'href="/categories/{python.id}">Python (1)</a>'.encode() in response.data
    response = client.get(f"/categories/{go.id}")
    assert response.status_code == 200
    assert b">Go</h1>" in response.data
    assert b"1 book(s)" in response.data
    book = store.books(category_id=go.id)[0]
    other = store.books(category_id=python.id)[0]
    assert f'href="/books/{book.id}"'.encode() in response.data
    assert f'href="/books/{other.id}"'.encode() not in response.data
    assert b'href="/categories" aria-current="page">Categories</a>' in response.data
    assert response.data.count(b'class="home-book-card card ') == 1
    assert b"/open" not in response.data
    response = client.get(f"/books/{book.id}")
    assert response.status_code == 200
    assert b">Category</dt>" in response.data
    assert f'href="/categories/{go.id}">Go</a>'.encode() in response.data


def test_empty_categories_and_escaped_folder_names(app, client):
    response = client.get("/categories")
    assert response.status_code == 200
    assert b"No categories yet." in response.data
    assert b'href="/library"' in response.data
    store = app.extensions["store"]
    store.sync_files([BookFile("/<Go & Books>/Book.pdf", "pdf", SHA256)])
    category = store.categories()[0]
    for url in [
        "/categories",
        f"/categories/{category.id}",
        f"/books/{store.books()[0].id}",
    ]:
        response = client.get(url)
        assert response.status_code == 200
        assert b"&lt;Go &amp; Books&gt;" in response.data
        assert b"<Go & Books>" not in response.data


@pytest.mark.parametrize("category_id", ["invalid", "-1", "0", "99999", "9" * 30])
def test_unknown_categories_return_not_found(client, category_id):
    assert client.get(f"/categories/{category_id}").status_code == 404
