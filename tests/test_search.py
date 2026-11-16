from re import findall

import pytest
from markupsafe import escape
from PIL import Image

from src.metadata import Metadata
from src.store import BookFile


@pytest.fixture
def searchable_books(store):
    store.sync_files(
        [
            BookFile(
                "/Programming/A Python Guide.pdf",
                "pdf",
                "a" * 64,
                metadata=Metadata(
                    title="Practical Coding",
                    author="Émile Smith",
                    isbn="9780132350884",
                ),
            ),
            BookFile(
                "/Programming/A Python Guide.epub",
                "epub",
                "b" * 64,
                metadata=Metadata(description="Learn asynchronous network automation."),
            ),
            BookFile("/Programming/B Python Reference.pdf", "pdf", "c" * 64),
            BookFile("/Fiction/100%_Magic.epub", "epub", "d" * 64),
        ]
    )
    return store.books()


@pytest.mark.parametrize(
    "query,expected",
    [
        ("pyTHon", ["A Python Guide", "B Python Reference"]),
        (" practical CODING ", ["A Python Guide"]),
        ("ÉMILE", ["A Python Guide"]),
        ("Programming", ["A Python Guide", "B Python Reference"]),
        ("9780132350884", ["A Python Guide"]),
        ("smith programming guide", ["A Python Guide"]),
        ("ASYNCHRONOUS", ["A Python Guide"]),
        ("network smith", ["A Python Guide"]),
        ("%_", ["100%_Magic"]),
        ("' OR 1=1 --", []),
        ("missing", []),
        ("", []),
        (" \t\n ", []),
    ],
)
def test_search_matches_fields_and_groups_formats(
    store, searchable_books, query, expected
):
    books = store.search_books(query)
    assert [book.file_title for book in books] == expected
    for book in books:
        assert book == store.book(book.id)


def test_search_results_show_all_matches_as_cards_with_covers(app, client):
    store = app.extensions["store"]
    files = [BookFile(f"/Books/Guide{i:02}.pdf", "pdf", f"{i:064x}") for i in range(23)]
    files.extend(
        [
            BookFile("/Books/Guide00.epub", "epub", "b" * 64),
            BookFile("/Books/Unrelated.pdf", "pdf", "c" * 64),
        ]
    )
    store.sync_files(files)
    books = store.search_books("guide")
    covers = app.extensions["indexer"].covers
    covers.directory.mkdir(parents=True)
    Image.new("RGB", (333, 500), "navy").save(covers.path(books[0].files[0]))

    response = client.get("/search", query_string={"q": " guide "})
    assert response.status_code == 200
    assert b"Results for " in response.data
    assert b'value="guide"' in response.data
    assert b"23 book(s)" in response.data
    assert response.data.count(b'class="home-book-card card ') == 23
    assert [
        int(book_id) for book_id in findall(rb'href="/books/(\d+)"', response.data)
    ] == [book.id for book in books]
    assert f'src="/books/{books[0].id}/cover"'.encode() in response.data
    assert response.data.count(b"Cover unavailable") == 22
    assert b"Unrelated" not in response.data
    assert b"<table" not in response.data
    assert b"/open" not in response.data
    assert client.get(f"/books/{books[0].id}").status_code == 200


@pytest.mark.parametrize(
    "query", [None, "", "   ", "no matches", '"><script>alert(1)</script>']
)
def test_search_handles_empty_queries_and_escapes_input(client, query):
    response = client.get("/search", query_string={} if query is None else {"q": query})
    assert response.status_code == 200
    assert b'class="home-book-card card ' not in response.data
    if query and query.strip():
        assert b"No books found." in response.data
        assert escape(query).encode() in response.data
        assert b"<script>alert(1)</script>" not in response.data
    else:
        assert (
            b"Search by title, author, description, category, or ISBN." in response.data
        )
        assert b"No books found." not in response.data


def test_search_bar_is_available_in_navigation_across_pages(app, client):
    store = app.extensions["store"]
    store.sync_files([BookFile("/Books/Guide.pdf", "pdf", "a" * 64)])
    book = store.books()[0]
    for path in (
        "/",
        "/categories",
        f"/categories/{book.category.id}",
        f"/books/{book.id}",
        "/search?q=guide",
    ):
        response = client.get(path)
        navigation = response.data.split(b"<nav", 1)[1].split(b"</nav>", 1)[0]
        assert b'action="/search" method="get" role="search"' in navigation
        assert b'for="book-search">Search books</label>' in navigation
        assert b'id="book-search" name="q" type="search"' in navigation
        assert b'type="submit">Search</button>' in navigation
