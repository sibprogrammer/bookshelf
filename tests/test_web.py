import sqlite3
import subprocess

import pytest


def test_index_and_details_flow(app, client, library):
    (library / "<b>&book.pdf").write_text("abc")
    assert b"No books indexed yet" in client.get("/").data
    assert client.get("/index").status_code == 405
    assert app.extensions["store"].books() == []
    response = client.post("/index")
    assert response.status_code == 303
    assert response.location == "/?indexed=1"
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


def test_database_errors_render_cleanly(app, client, monkeypatch):
    def fail():
        raise sqlite3.OperationalError("private database details")

    monkeypatch.setattr(app.extensions["store"], "books", fail)
    response = client.get("/")
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
    assert response.location == "/?indexed=1"
    book = app.extensions["store"].books()[0]
    response = client.get("/")
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
