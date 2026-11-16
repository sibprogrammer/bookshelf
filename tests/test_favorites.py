import sqlite3

import pytest
from PIL import Image

from src.store import BookFile, Store

SHA256 = "a" * 64


def test_existing_database_gains_favorites_without_losing_books(tmp_path):
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
                UNIQUE(directory, file_stem)
            );
            INSERT INTO categories VALUES (5, 'Go');
            INSERT INTO books VALUES (7, 'Book', 'Book', '/Go', 5);
        """)
    store = Store(path)
    store.sync_files([BookFile("/Go/Book.pdf", "pdf", SHA256)])
    book = store.books()[0]
    assert book.id == 7
    assert book.category.id == 5
    assert book.favorite is False
    assert store.favorite_books() == []
    assert store.set_favorite(book.id, True)
    assert Store(path).book(book.id).favorite is True


def test_favorites_persist_across_reindex_and_keep_all_formats(store):
    files = [
        BookFile("/Go/A.pdf", "pdf", SHA256),
        BookFile("/Go/A.epub", "epub", "b" * 64),
        BookFile("/Python/B.pdf", "pdf", "c" * 64),
    ]
    store.sync_files(files)
    book, other = store.books()
    assert not book.favorite
    assert not other.favorite
    assert store.set_favorite(book.id, True)
    assert store.set_favorite(book.id, True)
    favorite = store.book(book.id)
    assert favorite.favorite is True
    assert len(favorite.files) == 2
    assert store.favorite_books() == [favorite]
    assert Store(store.path).favorite_books() == [favorite]
    store.sync_files(files)
    assert store.favorite_books() == [favorite]
    # Updating the available formats also retains the book's favorite flag.
    store.sync_files(files[1:])
    assert store.book(book.id).favorite is True
    assert len(store.favorite_books()[0].files) == 1
    assert store.set_favorite(book.id, False)
    assert not Store(store.path).book(book.id).favorite
    assert store.favorite_books() == []
    assert not store.set_favorite(99999, True)
    store.set_favorite(book.id, True)
    store.sync_files([files[-1]])
    assert store.favorite_books() == []


def test_favorite_toggle_navigation_and_badges(app, client):
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
    action_url = details_url + "/favorite"
    page = client.get("/favorites")
    assert page.status_code == 200
    assert b"No favorite books yet" in page.data
    assert b'href="/favorites" aria-current="page">Favorites</a>' in page.data
    assert page.data.index(b">Categories</a>") < page.data.index(b">Favorites</a>")
    page = client.get(details_url)
    assert b'<footer class="card-footer' in page.data
    assert b'aria-label="Add to favorites"' in page.data
    assert b'aria-pressed="false"' in page.data
    assert b'name="favorite" value="1"' in page.data
    assert b'class="book-favorite-badge"' not in page.data
    assert client.get(action_url).status_code == 405
    response = client.post(action_url, data={"favorite": "1"})
    assert response.status_code == 303
    assert response.location == details_url
    page = client.get(details_url)
    assert b'aria-label="Remove from favorites"' in page.data
    assert b'aria-pressed="true"' in page.data
    assert b'name="favorite" value="0"' in page.data
    for url in [
        "/",
        "/favorites",
        "/search?q=Book",
        f"/categories/{book.category.id}",
        details_url,
    ]:
        page = client.get(url)
        assert page.status_code == 200
        assert b'class="book-favorite-badge"' in page.data
    page = client.get("/favorites")
    assert page.data.count(b'class="home-book-card card ') == 1
    assert f'href="{details_url}"'.encode() in page.data
    assert f'href="/books/{other.id}"'.encode() not in page.data
    assert f'src="{details_url}/cover"'.encode() in page.data
    response = client.post(action_url, data={"favorite": "0"})
    assert response.status_code == 303
    assert not store.book(book.id).favorite
    assert b"No favorite books yet" in client.get("/favorites").data
    assert b'class="book-favorite-badge"' not in client.get(details_url).data
    # Favorite badges also render when a cover image is unavailable.
    client.post(f"/books/{other.id}/favorite", data={"favorite": "1"})
    page = client.get("/favorites")
    assert b"Cover unavailable" in page.data
    assert page.data.count(b'class="book-favorite-badge"') == 1


@pytest.mark.parametrize("value", [None, "", "true", "2", "-1"])
def test_invalid_favorite_values_do_not_change_state(app, client, value):
    store = app.extensions["store"]
    store.sync_files([BookFile("/Book.pdf", "pdf", SHA256)])
    book = store.books()[0]
    data = {} if value is None else {"favorite": value}
    assert client.post(f"/books/{book.id}/favorite", data=data).status_code == 400
    assert not store.book(book.id).favorite


@pytest.mark.parametrize("book_id", ["invalid", "0", "-1", "99999", "9" * 30])
def test_unknown_books_cannot_be_favorited(client, book_id):
    response = client.post(f"/books/{book_id}/favorite", data={"favorite": "1"})
    assert response.status_code == 404


def test_cross_origin_favorite_changes_are_blocked(app, client):
    store = app.extensions["store"]
    store.sync_files([BookFile("/Book.pdf", "pdf", SHA256)])
    book = store.books()[0]
    response = client.post(
        f"/books/{book.id}/favorite",
        data={"favorite": "1"},
        headers={"Origin": "https://another-site.example"},
    )
    assert response.status_code == 403
    assert not store.book(book.id).favorite
