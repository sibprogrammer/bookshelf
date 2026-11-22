from io import BytesIO
from threading import Event
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import pymupdf
import pytest

from src import goodreads
from src.config import Config
from src.indexer import Indexer
from src.main import create_app


def write_pdf(path, text):
    with pymupdf.open() as document:
        document.new_page().insert_text((50, 50), text)
        document.save(path)


def test_metadata_refresh_fetches_by_isbn_and_displays_rating(
    app, client, library, monkeypatch
):
    calls = []
    offline = False

    def open_rating(request, *, timeout):
        calls.append(parse_qs(urlsplit(request.full_url).query))
        if offline:
            raise URLError("Goodreads unavailable")
        return BytesIO(b'[{"avgRating":"3.58","ratingsCount":163}]')

    monkeypatch.setattr(goodreads, "urlopen", open_rating)
    for name, text in (
        ("With ISBN", "ISBN-10: 1934356158"),
        ("Without ISBN", "Book contents"),
    ):
        write_pdf(library / f"{name}.pdf", text)
    client.post("/index")
    store = app.extensions["store"]
    rated, unrated = store.books()
    assert unrated.goodreads_rating is None
    expected_query = {"format": ["json"], "q": ["1934356158"]}
    assert calls == [expected_query]
    unrated_details_url = f"/books/{unrated.id}"
    assert b">Rating</dt>" not in client.get(unrated_details_url).data

    store.set_favorite(rated.id, True)
    store.set_finished(rated.id, True)
    store.set_want_to_read(rated.id, True)
    badge_label = b'aria-label="Rating 3.58 out of 5" title="3.58"'
    card_pages = (
        "/",
        "/search?q=ISBN",
        "/favorites",
        "/finished",
        "/want-to-read",
        f"/categories/{rated.category.id}",
        unrated_details_url,
    )
    for url in card_pages:
        response = client.get(url)
        assert response.status_code == 200
        assert badge_label in response.data, url

    client.post(f"/books/{unrated.id}/refresh-metadata")
    assert calls == [expected_query]
    refresh_url = f"/books/{rated.id}/refresh-metadata"
    response = client.post(refresh_url, follow_redirects=True)
    assert calls == [expected_query, expected_query]
    assert response.status_code == 200
    assert b"3.58 of 5 (163 votes)" in response.data
    assert b">Rating</dt>" in response.data
    assert badge_label in response.data
    assert response.data.count(b'class="book-rating-star"') == 5
    assert b"--fill: 58.00%" in response.data
    assert store.book(rated.id).goodreads_rating == goodreads.GoodreadsRating(3.58, 163)

    offline = True
    response = client.post(refresh_url, follow_redirects=True)
    assert response.status_code == 200
    assert b"3.58 of 5 (163 votes)" in response.data
    client.post("/index")
    assert store.book(rated.id).goodreads_rating == goodreads.GoodreadsRating(3.58, 163)
    assert len(calls) == 3

    disabled = create_app(Config(library, store.path, goodreads_ratings=False))
    disabled_client = disabled.test_client()
    response = disabled_client.post(refresh_url, follow_redirects=True)
    assert response.status_code == 200
    assert b">Rating</dt>" not in response.data
    assert b'class="book-rating-badge"' not in disabled_client.get("/").data
    assert len(calls) == 3


@pytest.mark.parametrize("no_rating", [b"[]", b'[{"avgRating":0,"ratingsCount":0}]'])
def test_book_without_goodreads_votes_is_cached_and_hidden(
    app, client, library, monkeypatch, no_rating
):
    calls = 0
    payload = no_rating

    def open_rating(request, *, timeout):
        nonlocal calls
        calls += 1
        return BytesIO(payload)

    monkeypatch.setattr(goodreads, "urlopen", open_rating)
    write_pdf(library / "Book.pdf", "ISBN-10: 1934356158")
    assert client.post("/index").status_code == 303
    store = app.extensions["store"]
    (book,) = store.books()
    assert book.goodreads_rating == goodreads.GoodreadsRating(0, 0)

    details_url = f"/books/{book.id}"
    assert b">Rating</dt>" not in client.get(details_url).data
    for url in ("/", details_url):
        assert b'class="book-rating-badge"' not in client.get(url).data

    assert client.post("/index").status_code == 303
    assert calls == 1

    payload = b'[{"avgRating":"3.58","ratingsCount":163}]'
    response = client.post(f"{details_url}/refresh-metadata", follow_redirects=True)
    assert calls == 2
    assert b"3.58 of 5 (163 votes)" in response.data


def test_bulk_index_pauses_between_uncached_isbns(library, store, monkeypatch):
    events = []

    class RecordingEvent(Event):
        def wait(self, timeout=None):
            events.append(("wait", timeout))

    def open_rating(request, *, timeout):
        isbn = parse_qs(urlsplit(request.full_url).query)["q"][0]
        events.append(("fetch", isbn))
        return BytesIO(b'[{"avgRating":"3.58","ratingsCount":163}]')

    monkeypatch.setattr(goodreads, "urlopen", open_rating)
    write_pdf(library / "Book.pdf", "ISBN-10: 1934356158")
    write_pdf(library / "Another copy.pdf", "ISBN-10: 1934356158")
    write_pdf(library / "Other book.pdf", "ISBN-13: 9780201616224")
    write_pdf(library / "No ISBN.pdf", "Book contents")

    indexer = Indexer(store, library)
    assert indexer.reindex(RecordingEvent()) == 4
    assert [event[0] for event in events] == ["fetch", "wait", "fetch"]
    assert events[1] == ("wait", 0.25)
    assert {events[0][1], events[2][1]} == {"1934356158", "9780201616224"}
    assert sum(book.goodreads_rating is not None for book in store.books()) == 3

    events.clear()
    assert indexer.reindex(RecordingEvent()) == 4
    assert events == []
