from io import BytesIO

import pymupdf
import pytest
from PIL import Image

from src.store import BookFile


@pytest.fixture
def preview_book(app, library):
    path = library / "Preview.pdf"
    with pymupdf.open() as document:
        document.new_page(width=600, height=900)
        for width, height, color in [(900, 600, (1, 0, 0)), (600, 900, (0, 0, 1))]:
            page = document.new_page(width=width, height=height)
            page.draw_rect(page.rect, color=color, fill=color)
        document.save(path)
    app.extensions["store"].sync_files([BookFile(str(path), "pdf", "a" * 64)])
    return app.extensions["store"].books()[0]


def test_preview_renders_each_pdf_page_in_order(client, preview_book):
    for number, color in enumerate(
        [(255, 255, 255), (255, 0, 0), (0, 0, 255)], start=1
    ):
        response = client.get(
            f"/books/{preview_book.id}/preview/{number}?path=/unindexed.pdf"
        )
        assert response.status_code == 200
        assert response.mimetype == "image/png"
        assert response.headers["X-Page-Count"] == "3"
        assert response.headers["Cache-Control"] == "no-store"
        with Image.open(BytesIO(response.data)) as image:
            assert image.width <= 666
            assert image.height <= 1000
            assert image.getpixel((image.width // 2, image.height // 2)) == color
            assert (image.width > image.height) == (number == 2)


@pytest.mark.parametrize(
    "size, width, height", [("large", 1332, 2000), ("999999999", 666, 1000)]
)
def test_preview_resolution_is_bounded(client, preview_book, size, width, height):
    response = client.get(f"/books/{preview_book.id}/preview/2?size={size}")
    assert response.status_code == 200
    assert response.headers["X-Page-Count"] == "3"
    with Image.open(BytesIO(response.data)) as image:
        assert image.width == width
        assert image.height <= height
        assert image.getpixel((image.width // 2, image.height // 2)) == (255, 0, 0)


@pytest.mark.parametrize("number", ["0", "-1", "4", "9" * 30, "invalid"])
def test_preview_rejects_out_of_range_pages(client, preview_book, number):
    assert client.get(f"/books/{preview_book.id}/preview/{number}").status_code == 404


@pytest.mark.parametrize("book_id", ["0", "99999", "9" * 30, "invalid"])
def test_preview_rejects_unknown_books(client, book_id):
    assert client.get(f"/books/{book_id}/preview/1").status_code == 404


def test_pdf_details_offer_preview_even_without_cached_cover(client, preview_book):
    response = client.get(f"/books/{preview_book.id}")
    assert response.status_code == 200
    assert (
        f'data-book-preview="/books/{preview_book.id}/preview/1"'.encode()
        in response.data
    )
    assert b'aria-label="Previous PDF page"' in response.data
    assert b'aria-label="Next PDF page"' in response.data
    assert b"data-preview-status" in response.data
    assert b'aria-label="Open larger PDF preview"' in response.data
    assert b'aria-controls="book-preview-modal"' in response.data
    assert b'id="book-preview-modal"' in response.data
    assert b'id="book-preview-modal-title">Preview</h2>' in response.data
    assert b'data-bs-dismiss="modal" aria-label="Close preview"' in response.data


def test_missing_epub_keeps_details_available(app, client, library):
    path = library / "EPUB only.epub"
    app.extensions["store"].sync_files([BookFile(str(path), "epub", "b" * 64)])
    book = app.extensions["store"].books()[0]
    response = client.get(f"/books/{book.id}")
    assert response.status_code == 200
    assert b"data-book-preview" in response.data
    assert b'aria-label="Previous EPUB page"' in response.data
    assert b'aria-label="Next EPUB page"' in response.data
    assert b'id="book-preview-modal"' in response.data
    assert client.get(f"/books/{book.id}/preview/1").status_code == 404


@pytest.mark.parametrize("kind", ["missing", "directory", "corrupt", "encrypted"])
def test_unavailable_pdf_fails_cleanly(client, preview_book, kind):
    from pathlib import Path

    path = Path(preview_book.files[0].file_path)
    path.unlink()
    if kind == "directory":
        path.mkdir()
    elif kind == "corrupt":
        path.write_text("Not a PDF")
    elif kind == "encrypted":
        with pymupdf.open() as document:
            document.new_page()
            document.save(
                path,
                encryption=pymupdf.PDF_ENCRYPT_AES_256,
                owner_pw="owner",
                user_pw="reader",
            )
    response = client.get(f"/books/{preview_book.id}/preview/1")
    assert response.status_code == (404 if kind in {"missing", "directory"} else 422)
    assert str(path).encode() not in response.data
    assert client.get(f"/books/{preview_book.id}").status_code == 200


def test_dual_format_preview_uses_pdf(app, client, preview_book, library):
    epub = BookFile(str(library / "Preview.epub"), "epub", "b" * 64)
    app.extensions["store"].sync_files([epub, preview_book.files[0]])
    response = client.get(f"/books/{preview_book.id}/preview/2")
    assert response.status_code == 200
    with Image.open(BytesIO(response.data)) as image:
        assert image.getpixel((image.width // 2, image.height // 2)) == (255, 0, 0)
