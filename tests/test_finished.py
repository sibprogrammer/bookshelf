import sqlite3

import pytest
from PIL import Image

from src.store import BookFile, Store

SHA256 = "a" * 64


def test_existing_database_gains_read_status_and_keeps_favorites(tmp_path):
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
                UNIQUE(directory, file_stem)
            );
            INSERT INTO categories VALUES (5, 'Go');
            INSERT INTO books VALUES (7, 'Book', 'Book', '/Go', 5, 1);
        """)
    store = Store(path)
    store.sync_files([BookFile("/Go/Book.pdf", "pdf", SHA256)])
    book = store.books()[0]
    assert book.id == 7
    assert book.category.id == 5
    assert book.favorite is True
    assert book.finished is False
    assert store.finished_books() == []
    store.set_finished(book.id, True)
    assert Store(path).book(book.id).finished is True
    assert Store(path).book(book.id).favorite is True


def test_read_status_persists_across_reindex_and_format_changes(store):
    files = [
        BookFile("/Go/A.pdf", "pdf", SHA256),
        BookFile("/Go/A.epub", "epub", "b" * 64),
        BookFile("/Go/B.pdf", "pdf", "c" * 64),
    ]
    store.sync_files(files)
    book, other = store.books()
    assert not book.finished
    assert not other.finished
    assert store.set_finished(book.id, True)
    assert store.set_finished(book.id, True)
    finished = store.book(book.id)
    assert finished.finished is True
    assert len(finished.files) == 2
    assert store.finished_books() == [finished]
    assert Store(store.path).finished_books() == [finished]
    store.sync_files(files)
    assert store.finished_books() == [finished]
    store.sync_files(files[1:])
    assert store.book(book.id).finished is True
    assert len(store.finished_books()[0].files) == 1
    assert store.set_finished(book.id, False)
    assert not Store(store.path).book(book.id).finished
    assert store.finished_books() == []
    assert not store.set_finished(99999, True)
    store.set_finished(book.id, True)
    store.sync_files([files[-1]])
    assert store.finished_books() == []


def test_mark_as_read_navigation_badges_and_independent_favorites(app, client):
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
    action_url = details_url + "/finished"
    page = client.get("/finished")
    assert page.status_code == 200
    assert b"No finished books yet" in page.data
    assert b'href="/finished" aria-current="page">Finished</a>' in page.data
    assert page.data.index(b">Favorites</a>") < page.data.index(b">Finished</a>")
    page = client.get(details_url)
    assert b'aria-label="Mark as read" title="Mark as read" aria-pressed="false"' in (
        page.data
    )
    assert b'name="finished" value="1"' in page.data
    assert b'class="book-finished-badge"' not in page.data
    assert client.get(action_url).status_code == 405
    response = client.post(action_url, data={"finished": "1"})
    assert response.status_code == 303
    assert response.location == details_url
    assert store.book(book.id).finished
    assert not store.book(book.id).favorite
    client.post(details_url + "/favorite", data={"favorite": "1"})
    page = client.get(details_url)
    assert (
        b'aria-label="Mark as unread" title="Mark as unread" aria-pressed="true"'
        in (page.data)
    )
    assert b'name="finished" value="0"' in page.data
    for url in [
        "/",
        "/library",
        "/finished",
        "/favorites",
        "/search?q=Book",
        f"/categories/{book.category.id}",
        details_url,
    ]:
        page = client.get(url)
        assert page.status_code == 200
        assert b'class="book-finished-badge"' in page.data
        assert b'class="book-favorite-badge"' in page.data
    page = client.get("/finished")
    assert page.data.count(b'class="home-book-card card ') == 1
    assert f'href="{details_url}"'.encode() in page.data
    assert f'href="/books/{other.id}"'.encode() not in page.data
    assert f'src="{details_url}/cover"'.encode() in page.data
    response = client.post(action_url, data={"finished": "0"})
    assert response.status_code == 303
    assert not store.book(book.id).finished
    assert store.book(book.id).favorite
    assert b"No finished books yet" in client.get("/finished").data
    assert b'class="book-finished-badge"' not in client.get("/library").data
    client.post(f"/books/{other.id}/finished", data={"finished": "1"})
    client.post(f"/books/{other.id}/favorite", data={"favorite": "1"})
    client.post(f"/books/{other.id}/favorite", data={"favorite": "0"})
    assert store.book(other.id).finished
    page = client.get("/finished")
    assert b"Cover unavailable" in page.data
    assert page.data.count(b'class="book-finished-badge"') == 1


@pytest.mark.parametrize("value", [None, "", "true", "2", "-1"])
def test_invalid_finished_values_do_not_change_read_status(app, client, value):
    store = app.extensions["store"]
    store.sync_files([BookFile("/Book.pdf", "pdf", SHA256)])
    book = store.books()[0]
    data = {} if value is None else {"finished": value}
    assert client.post(f"/books/{book.id}/finished", data=data).status_code == 400
    assert not store.book(book.id).finished


@pytest.mark.parametrize("book_id", ["invalid", "0", "-1", "99999", "9" * 30])
def test_unknown_books_cannot_be_marked_as_read(client, book_id):
    response = client.post(f"/books/{book_id}/finished", data={"finished": "1"})
    assert response.status_code == 404


def test_cross_origin_read_status_changes_are_blocked(app, client):
    store = app.extensions["store"]
    store.sync_files([BookFile("/Book.pdf", "pdf", SHA256)])
    book = store.books()[0]
    response = client.post(
        f"/books/{book.id}/finished",
        data={"finished": "1"},
        headers={"Origin": "https://another-site.example"},
    )
    assert response.status_code == 403
    assert not store.book(book.id).finished
