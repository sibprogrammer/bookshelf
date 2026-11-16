import sqlite3

import pytest
from PIL import Image

from src.store import BookFile, Store

SHA256 = "a" * 64


def test_existing_database_gains_want_to_read_and_keeps_other_marks(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_stem TEXT NOT NULL,
                title TEXT NOT NULL,
                directory TEXT NOT NULL,
                category_id INTEGER NOT NULL REFERENCES categories(id),
                favorite INTEGER NOT NULL DEFAULT 0 CHECK(favorite IN (0, 1)),
                finished INTEGER NOT NULL DEFAULT 0 CHECK(finished IN (0, 1)),
                UNIQUE(directory, file_stem)
            );
            INSERT INTO categories VALUES (5, 'Go');
            INSERT INTO books VALUES (7, 'Book', 'Book', '/Go', 5, 1, 1);
        """)
    store = Store(path)
    store.sync_files([BookFile("/Go/Book.pdf", "pdf", SHA256)])
    book = store.books()[0]
    assert book.id == 7
    assert book.category.id == 5
    assert book.favorite is True
    assert book.finished is True
    assert book.want_to_read is False
    assert store.want_to_read_books() == []
    assert store.set_want_to_read(book.id, True)
    saved = Store(path).book(book.id)
    assert saved.want_to_read is True
    assert saved.favorite is True
    assert saved.finished is True


def test_want_to_read_persists_across_reindex_and_format_changes(store):
    files = [
        BookFile("/Go/A.pdf", "pdf", SHA256),
        BookFile("/Go/A.epub", "epub", "b" * 64),
        BookFile("/Go/B.pdf", "pdf", "c" * 64),
    ]
    store.sync_files(files)
    book, other = store.books()
    assert not book.want_to_read
    assert not other.want_to_read
    assert store.set_want_to_read(book.id, True)
    assert store.set_want_to_read(book.id, True)
    marked = store.book(book.id)
    assert marked.want_to_read is True
    assert len(marked.files) == 2
    assert Store(store.path).want_to_read_books() == [marked]
    store.sync_files(files)
    assert store.want_to_read_books() == [marked]
    store.sync_files(files[1:])
    assert store.book(book.id).want_to_read is True
    assert len(store.want_to_read_books()[0].files) == 1
    assert store.set_want_to_read(book.id, False)
    assert not Store(store.path).book(book.id).want_to_read
    assert store.want_to_read_books() == []
    assert not store.set_want_to_read(99999, True)
    store.set_want_to_read(book.id, True)
    store.sync_files([files[-1]])
    assert store.want_to_read_books() == []


def test_want_to_read_toggle_navigation_and_badges(app, client):
    store = app.extensions["store"]
    store.sync_files(
        [
            BookFile("/Go/Book.pdf", "pdf", SHA256),
            BookFile("/Go/Book.epub", "epub", "b" * 64),
            BookFile("/Go/Other.pdf", "pdf", "c" * 64),
        ]
    )
    book, other = store.books()
    covers = app.extensions["indexer"].covers
    covers.directory.mkdir(parents=True)
    Image.new("RGB", (333, 500), "navy").save(covers.path(book.files[0]))
    details_url = f"/books/{book.id}"
    action_url = details_url + "/want-to-read"
    page = client.get("/want-to-read")
    assert page.status_code == 200
    assert b"No books marked as Want to Read yet" in page.data
    assert b'href="/want-to-read" aria-current="page">Want to Read</a>' in page.data
    assert (
        page.data.index(b">Favorites</a>")
        < page.data.index(b">Want to Read</a>")
        < page.data.index(b">Finished</a>")
    )
    page = client.get(details_url)
    assert b'aria-label="Mark as Want to Read"' in page.data
    assert b'name="want_to_read" value="1"' in page.data
    assert b'class="book-want-to-read-badge"' not in page.data
    assert client.get(action_url).status_code == 405
    response = client.post(action_url, data={"want_to_read": "1"})
    assert response.status_code == 303
    assert response.location == details_url
    marked = store.book(book.id)
    assert marked.want_to_read
    assert not marked.finished
    assert not marked.favorite
    client.post(details_url + "/favorite", data={"favorite": "1"})
    client.post(details_url + "/finished", data={"finished": "1"})
    page = client.get(details_url)
    assert (
        b'aria-label="Remove from Want to Read" '
        b'title="Remove from Want to Read" aria-pressed="true"' in page.data
    )
    assert b'name="want_to_read" value="0"' in page.data
    for url in [
        "/",
        "/want-to-read",
        "/finished",
        "/favorites",
        "/search?q=Book",
        f"/categories/{book.category.id}",
        details_url,
    ]:
        page = client.get(url)
        assert page.status_code == 200
        assert b'class="book-want-to-read-badge"' in page.data
        assert b'class="book-finished-badge"' in page.data
        assert b'class="book-favorite-badge"' in page.data
    page = client.get("/want-to-read")
    assert page.data.count(b'class="home-book-card card ') == 1
    assert f'href="{details_url}"'.encode() in page.data
    assert f'href="/books/{other.id}"'.encode() not in page.data
    assert f'src="{details_url}/cover"'.encode() in page.data
    response = client.post(action_url, data={"want_to_read": "0"})
    assert response.status_code == 303
    saved = store.book(book.id)
    assert not saved.want_to_read
    assert saved.favorite
    assert saved.finished
    assert b"No books marked as Want to Read yet" in client.get("/want-to-read").data
    assert b'class="book-want-to-read-badge"' not in client.get(details_url).data
    client.post(f"/books/{other.id}/want-to-read", data={"want_to_read": "1"})
    page = client.get("/want-to-read")
    assert b"Cover unavailable" in page.data
    assert page.data.count(b'class="book-want-to-read-badge"') == 1


@pytest.mark.parametrize("value", [None, "", "true", "2", "-1"])
def test_invalid_want_to_read_values_do_not_change_state(app, client, value):
    store = app.extensions["store"]
    store.sync_files([BookFile("/Book.pdf", "pdf", SHA256)])
    book = store.books()[0]
    data = {} if value is None else {"want_to_read": value}
    response = client.post(f"/books/{book.id}/want-to-read", data=data)
    assert response.status_code == 400
    assert not store.book(book.id).want_to_read


@pytest.mark.parametrize("book_id", ["invalid", "0", "-1", "99999", "9" * 30])
def test_unknown_books_cannot_be_marked_as_want_to_read(client, book_id):
    response = client.post(f"/books/{book_id}/want-to-read", data={"want_to_read": "1"})
    assert response.status_code == 404


def test_cross_origin_want_to_read_changes_are_blocked(app, client):
    store = app.extensions["store"]
    store.sync_files([BookFile("/Book.pdf", "pdf", SHA256)])
    book = store.books()[0]
    response = client.post(
        f"/books/{book.id}/want-to-read",
        data={"want_to_read": "1"},
        headers={"Origin": "https://another-site.example"},
    )
    assert response.status_code == 403
    assert not store.book(book.id).want_to_read
