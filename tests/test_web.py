import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from re import findall

import pytest
from PIL import Image

from src.store import BookFile


def test_index_and_details_flow(app, client, library):
    (library / "<b>&book.pdf").write_text("abc")
    assert b"No categories yet" in client.get("/categories").data
    assert client.get("/index").status_code == 405
    assert app.extensions["store"].books() == []
    response = client.post("/index")
    assert response.status_code == 303
    assert response.location == "/categories?indexed=1"
    response = client.get(response.location)
    assert response.headers["Cache-Control"] == "no-store"
    assert b"Index complete" in response.data
    assert b'href="/categories" aria-current="page">Categories</a>' in response.data
    assert b'id="index-form" action="/index" method="post"' in response.data
    category = app.extensions["store"].categories()[0]
    assert f'href="/categories/{category.id}"'.encode() in response.data
    response = client.get(f"/categories/{category.id}")
    assert b">&lt;b&gt;&amp;book</h3>" in response.data
    assert b"<b>&book" not in response.data
    book = app.extensions["store"].books()[0]
    assert f'href="/books/{book.id}"'.encode() in response.data
    assert b'class="home-book-card card ' in response.data
    response = client.get(f"/books/{book.id}")
    assert response.status_code == 200
    assert response.content_type == "text/html; charset=utf-8"
    assert b"&lt;b&gt;&amp;book</h1>" in response.data
    assert b">PDF</button>" in response.data
    assert b"Available formats" not in response.data
    assert b"Absolute path" not in response.data
    assert b"SHA-256" not in response.data
    assert client.get("/missing").status_code == 404
    response = client.get("/library")
    assert response.status_code == 404


def test_failed_and_overlapping_index(app, client, library):
    (library / "book.pdf").write_text("abc")
    client.post("/index")
    before = app.extensions["store"].books()
    with app.extensions["indexer"].lock:
        response = client.post("/index")
    assert response.status_code == 409
    assert b">Categories</h1>" in response.data
    assert b'href="/categories" aria-current="page">Categories</a>' in response.data
    assert b'id="index-form"' in response.data
    library.rename(library.with_name("missing"))
    response = client.post("/index")
    assert response.status_code == 500
    assert b">Categories</h1>" in response.data
    assert b'id="index-form"' in response.data
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
    assert response.location == f"/books/{book.id}"
    assert calls == [str(path.resolve())]
    assert b"alert-success" not in client.get(response.location).data
    assert b"Opening book" not in client.get(f"/books/{book.id}?opened=1").data

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
    assert b">PDF</button>" in client.get(f"/books/{book.id}").data


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


@pytest.mark.parametrize(
    "path, method",
    [
        ("/", "recent_books"),
        ("/", "random_books"),
        ("/categories", "categories"),
        ("/categories", "category_book_counts"),
        ("/search?q=book", "search_books"),
    ],
)
def test_database_errors_render_cleanly(app, client, monkeypatch, path, method):
    def fail(*args):
        raise sqlite3.OperationalError("private database details")

    monkeypatch.setattr(app.extensions["store"], method, fail)
    response = client.get(path)
    assert response.status_code == 500
    assert b"Unable to load the book index" in response.data
    assert b'href="/categories">Back to categories</a>' in response.data
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
    assert response.location == "/categories?indexed=1"
    book = app.extensions["store"].books()[0]
    response = client.get(f"/categories/{book.category.id}")
    assert response.data.count(f'href="/books/{book.id}"'.encode()) == 1
    assert b">A Book</h3>" in response.data
    assert b"A Book.pdf" not in response.data
    assert b"A Book.EPUB" not in response.data
    assert b"1 book(s)" in response.data
    assert response.data.count(b'class="home-book-card card ') == 1
    assert b"/open" not in response.data
    details = client.get(f"/books/{book.id}")
    assert b">A Book</h1>" in details.data
    assert b">PDF</button>" in details.data
    assert b">EPUB</button>" in details.data
    assert b"Available formats" not in details.data
    calls = []
    app.extensions["open_book"] = calls.append
    for file in book.files:
        url = f"/books/{book.id}/files/{file.id}/open"
        assert f'action="{url}"'.encode() in details.data
        assert client.get(url).status_code == 405
        opened = client.post(url + "?path=/unindexed.pdf&extension=wrong")
        assert opened.status_code == 303
        assert opened.location == f"/books/{book.id}"
        assert b"alert-success" not in client.get(opened.location).data
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


def test_home_lists_twenty_recent_books_as_cards_and_category_keeps_every_book(
    app, client
):
    store = app.extensions["store"]
    base = datetime(2025, 1, 1, tzinfo=UTC)
    files = [
        BookFile(
            f"/Book{i:02}.pdf", "pdf", f"{i:064x}", added_at=base + timedelta(days=i)
        )
        for i in range(22)
    ]
    files.append(
        BookFile("/Book00.epub", "epub", "b" * 64, added_at=base + timedelta(days=40))
    )
    files.append(BookFile("/Unknown.pdf", "pdf", "c" * 64))
    store.sync_files(files)
    books = {book.title: book for book in store.books()}
    expected = [books["Book00"]] + [books[f"Book{i:02}"] for i in range(21, 2, -1)]
    assert store.recent_books() == expected
    covers = app.extensions["indexer"].covers
    covers.directory.mkdir(parents=True)
    Image.new("RGB", (333, 500), "navy").save(covers.path(books["Book00"].files[0]))
    response = client.get("/")
    assert response.status_code == 200
    assert b">Recently Added</h2>" in response.data
    assert response.data.index(b">Random Picks</h2>") < response.data.index(
        b">Recently Added</h2>"
    )
    assert b'href="/" aria-current="page">Home</a>' in response.data
    assert "Home · Bookshelf".encode() in response.data
    card_ids = [
        int(book_id) for book_id in findall(rb'href="/books/(\d+)"', response.data)
    ]
    assert len(set(card_ids[:5])) == 5
    assert set(card_ids[:5]) <= {book.id for book in books.values()}
    assert card_ids[5:] == [book.id for book in expected]
    assert response.data.count(b'class="home-book-card card ') == 25
    assert b'method="post"' not in response.data
    assert b"<table" not in response.data
    assert b"/open" not in response.data
    assert f'src="/books/{books["Book00"].id}/cover"'.encode() in response.data
    assert b'width="333" height="500"' in response.data
    image_count = response.data.count(b'<img class="home-book-cover card-img-top"')
    assert response.data.count(b"Cover unavailable") == 25 - image_count
    response = client.get(f"/categories/{books['Book00'].category.id}")
    assert response.status_code == 200
    assert b'href="/categories" aria-current="page">Categories</a>' in response.data
    assert b">Library</a>" not in response.data
    assert b'action="/index"' not in response.data
    assert b"23 book(s)" in response.data
    assert response.data.count(b'class="home-book-card card ') == 23
    assert b"<table" not in response.data
    assert b"/open" not in response.data
    assert f'src="/books/{books["Book00"].id}/cover"'.encode() in response.data
    assert response.data.count(b"Cover unavailable") == 22
    assert [
        int(book_id) for book_id in findall(rb'href="/books/(\d+)"', response.data)
    ] == [book.id for book in store.books()]
    response = client.get(f"/books/{books['Book00'].id}")
    assert b">Library</a>" not in response.data
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


def test_empty_home_links_to_categories_for_indexing(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b">Recently Added</h2>" in response.data
    assert b">Random Picks</h2>" in response.data
    assert b"No books indexed yet" in response.data
    assert b'href="/categories">Categories</a> to index your books' in response.data
    assert b'action="/index"' not in response.data
    response = client.get("/categories")
    assert response.status_code == 200
    assert b"Use the button above to scan" in response.data
    assert b'id="index-form" action="/index" method="post"' in response.data
    assert b"data-button-label>Reindex</span>" in response.data
    assert response.data.index(b'id="index-form"') < response.data.index(
        b'aria-label="Book categories"'
    )
    assert b">Library</a>" not in response.data


@pytest.mark.parametrize("count", [0, 1, 3, 8])
def test_random_picks_limit_unique_books_and_keep_all_formats(store, count):
    files = [BookFile(f"/Book{i}.pdf", "pdf", f"{i:064x}") for i in range(count)]
    if count:
        files.append(BookFile("/Book0.epub", "epub", "b" * 64))
    store.sync_files(files)
    picks = store.random_books()
    assert len(picks) == min(count, 5)
    assert len({book.id for book in picks}) == len(picks)
    for book in picks:
        assert book == store.book(book.id)
    if count <= 5:
        assert {book.id for book in picks} == {book.id for book in store.books()}
