import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from re import findall

import pytest

from src.store import BookFile


def test_index_and_details_flow(app, client, library):
    (library / "<b>&book.pdf").write_text("abc")
    assert b"No books indexed yet" in client.get("/library").data
    assert client.get("/index").status_code == 405
    assert app.extensions["store"].books() == []
    response = client.post("/index")
    assert response.status_code == 303
    assert response.location == "/library?indexed=1"
    response = client.get(response.location)
    assert response.headers["Cache-Control"] == "no-store"
    assert b"Index complete" in response.data
    assert b">&lt;b&gt;&amp;book</a>" in response.data
    assert b"<b>&book" not in response.data
    book = app.extensions["store"].books()[0]
    assert f'href="/books/{book.id}"'.encode() in response.data
    assert b'aria-label="Open &lt;b&gt;&amp;book as PDF locally"' in response.data
    response = client.get(f"/books/{book.id}")
    assert response.status_code == 200
    assert response.content_type == "text/html; charset=utf-8"
    assert b"&lt;b&gt;&amp;book</h1>" in response.data
    assert book.files[0].sha256.encode() in response.data
    assert b"Open PDF locally" in response.data
    assert b"Absolute path" in response.data
    assert client.get("/missing").status_code == 404


def test_failed_and_overlapping_index(app, client, library):
    (library / "book.pdf").write_text("abc")
    client.post("/index")
    before = app.extensions["store"].books()
    with app.extensions["indexer"].lock:
        response = client.post("/index")
    assert response.status_code == 409
    library.rename(library.with_name("missing"))
    response = client.post("/index")
    assert response.status_code == 500
    assert b"existing index was kept" in response.data
    assert app.extensions["store"].books() == before


def test_open_book_uses_stored_path_and_handles_failures(app, client, library):
    path = library / "book's & name.pdf"
    path.write_text("abc")
    client.post("/index")
    book = app.extensions["store"].books()[0]
    calls = []
    app.extensions["open_book"] = calls.append
    url = f"/books/{book.id}/files/{book.files[0].id}/open"
    assert client.get(url).status_code == 405
    assert calls == []
    response = client.post(url + "?path=/unindexed.pdf")
    assert response.status_code == 303
    assert response.location == f"/books/{book.id}?opened=1"
    assert calls == [str(path.resolve())]
    assert b"Opening book in your default reader" in client.get(response.location).data

    def fail(path):
        raise subprocess.CalledProcessError(1, "open")

    app.extensions["open_book"] = fail
    response = client.post(url)
    assert response.status_code == 500
    assert b"Unable to open this book" in response.data
    path.unlink()
    assert client.post(url).status_code == 404
    assert b"Book file not found" in client.post(url).data
    path.mkdir()
    assert client.post(url).status_code == 404
    assert book.files[0].sha256.encode() in client.get(f"/books/{book.id}").data


@pytest.mark.parametrize("book_id", ["invalid", "0", "-1", "99999", "9" * 30])
def test_invalid_book_ids_are_404(client, book_id):
    assert client.get(f"/books/{book_id}").status_code == 404
    assert client.post(f"/books/{book_id}/files/1/open").status_code == 404


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "https://another-site.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "same-origin", "Origin": "https://another-site.example"},
    ],
)
def test_cross_origin_actions_are_blocked(app, client, library, headers):
    (library / "book.pdf").write_text("abc")
    assert client.post("/index", headers=headers).status_code == 403
    assert app.extensions["store"].books() == []
    client.post("/index")
    book = app.extensions["store"].books()[0]
    calls = []
    app.extensions["open_book"] = calls.append
    url = f"/books/{book.id}/files/{book.files[0].id}/open"
    assert client.post(url, headers=headers).status_code == 403
    assert calls == []


def test_same_origin_forms_are_allowed(client):
    response = client.post(
        "/index",
        headers={
            "Sec-Fetch-Site": "same-origin",
            "Origin": "http://localhost",
        },
    )
    assert response.status_code == 303


@pytest.mark.parametrize("path, method", [("/", "recent_books"), ("/library", "books")])
def test_database_errors_render_cleanly(app, client, monkeypatch, path, method):
    def fail():
        raise sqlite3.OperationalError("private database details")

    monkeypatch.setattr(app.extensions["store"], method, fail)
    response = client.get(path)
    assert response.status_code == 500
    assert b"Unable to load the book index" in response.data
    assert b"private database details" not in response.data


def test_bootstrap_is_served_locally(client):
    response = client.get("/static/vendor/bootstrap/bootstrap.min.css")
    assert response.status_code == 200
    assert b"v5.3.9" in response.data
    response = client.get("/static/vendor/bootstrap/bootstrap.bundle.min.js")
    assert response.status_code == 200
    assert b"Bootstrap v5.3.9" in response.data


def test_dual_format_book_renders_once_and_each_format_opens_its_own_file(
    app,
    client,
    library,
):
    pdf = library / "A Book.pdf"
    epub = library / "A Book.EPUB"
    pdf.write_text("PDF")
    epub.write_text("EPUB")
    response = client.post("/index")
    assert response.location == "/library?indexed=1"
    book = app.extensions["store"].books()[0]
    response = client.get("/library")
    assert response.data.count(f'href="/books/{book.id}"'.encode()) == 1
    assert b">A Book</a>" in response.data
    assert b"A Book.pdf" not in response.data
    assert b"A Book.EPUB" not in response.data
    assert b"1 book(s)" in response.data
    assert b">PDF</button>" in response.data
    assert b">EPUB</button>" in response.data
    details = client.get(f"/books/{book.id}")
    assert b">A Book</h1>" in details.data
    assert b"Open PDF locally" in details.data
    assert b"Open EPUB locally" in details.data
    calls = []
    app.extensions["open_book"] = calls.append
    for file in book.files:
        url = f"/books/{book.id}/files/{file.id}/open"
        assert f'action="{url}"'.encode() in response.data
        assert f'action="{url}"'.encode() in details.data
        assert file.file_path.encode() in details.data
        assert file.sha256.encode() in details.data
        assert client.get(url).status_code == 405
        opened = client.post(url + "?path=/unindexed.pdf&extension=wrong")
        assert opened.status_code == 303
        assert opened.location == f"/books/{book.id}?opened=1"
        assert calls[-1] == file.file_path
    assert calls == [str(pdf.resolve()), str(epub.resolve())]
    pdf.unlink()
    assert (
        client.post(f"/books/{book.id}/files/{book.files[0].id}/open").status_code
        == 404
    )
    assert (
        client.post(f"/books/{book.id}/files/{book.files[1].id}/open").status_code
        == 303
    )
    assert calls[-1] == str(epub.resolve())


def test_cannot_open_file_from_another_book(app, client, library):
    (library / "First.pdf").write_text("first")
    (library / "Second.epub").write_text("second")
    client.post("/index")
    first, second = app.extensions["store"].books()
    calls = []
    app.extensions["open_book"] = calls.append
    for file_id in [second.files[0].id, 0, 99999, "9" * 30, "invalid"]:
        url = f"/books/{first.id}/files/{file_id}/open"
        assert client.post(url).status_code == 404
    assert calls == []


def test_home_lists_ten_recent_books_and_library_keeps_every_book(app, client):
    store = app.extensions["store"]
    base = datetime(2025, 1, 1, tzinfo=UTC)
    files = [
        BookFile(f"/Book{i:02}.pdf", "pdf", "a" * 64, added_at=base + timedelta(days=i))
        for i in range(12)
    ]
    # The newer format makes this the newest book; both files must still render.
    files.append(
        BookFile("/Book00.epub", "epub", "b" * 64, added_at=base + timedelta(days=20))
    )
    files.append(BookFile("/Unknown.pdf", "pdf", "c" * 64))
    store.sync_files(files)
    books = {book.title: book for book in store.books()}
    expected = [books["Book00"]] + [books[f"Book{i:02}"] for i in range(11, 2, -1)]
    assert store.recent_books() == expected
    response = client.get("/")
    assert response.status_code == 200
    assert b">Recently Added</h1>" in response.data
    assert b'href="/" aria-current="page">Home</a>' in response.data
    assert "Home · Bookshelf".encode() in response.data
    assert [
        int(book_id) for book_id in findall(rb'href="/books/(\d+)"', response.data)
    ] == [book.id for book in expected]
    assert b'action="/index"' not in response.data
    for file in books["Book00"].files:
        url = f"/books/{books['Book00'].id}/files/{file.id}/open"
        assert f'action="{url}"'.encode() in response.data
    response = client.get("/library")
    assert response.status_code == 200
    assert b">Library</h1>" in response.data
    assert b'href="/library" aria-current="page">Library</a>' in response.data
    assert b'action="/index"' in response.data
    assert b"13 book(s)" in response.data
    assert [
        int(book_id) for book_id in findall(rb'href="/books/(\d+)"', response.data)
    ] == [book.id for book in store.books()]
    response = client.get(f"/books/{books['Book00'].id}")
    assert b'href="/library" aria-current="page">Library</a>' in response.data
    assert b">Book00</h1>" in response.data


def test_recent_books_sort_unknown_dates_last_and_break_ties_alphabetically(store):
    date = datetime(2025, 1, 1, tzinfo=UTC)
    store.sync_files(
        [
            BookFile("/Z dated.pdf", "pdf", "a" * 64, added_at=date),
            BookFile("/A dated.pdf", "pdf", "b" * 64, added_at=date),
            BookFile("/A unknown.pdf", "pdf", "c" * 64),
            BookFile("/B unknown.epub", "epub", "d" * 64),
        ]
    )
    assert [book.title for book in store.recent_books()] == [
        "A dated",
        "Z dated",
        "A unknown",
        "B unknown",
    ]


def test_empty_home_links_to_library_for_indexing(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b">Recently Added</h1>" in response.data
    assert b"No books indexed yet" in response.data
    assert b'href="/library">Library</a> to index your books' in response.data
    assert b'action="/index"' not in response.data
    response = client.get("/library")
    assert response.status_code == 200
    assert b"Use the button above to scan" in response.data
    assert b'action="/index"' in response.data
