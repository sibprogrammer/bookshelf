from io import BytesIO
from zipfile import ZIP_STORED, ZipFile

import pymupdf
import pytest
from PIL import Image

from src.store import BookFile


def make_epub(path, *, second_body='<img src="blue.png"/>', cover=None):
    with ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="book.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        package = (
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            'unique-identifier="id">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:identifier id="id">preview</dc:identifier>'
            "<dc:title>EPUB Preview</dc:title><dc:language>en</dc:language></metadata>"
            '<manifest><item id="first" href="first.xhtml" '
            'media-type="application/xhtml+xml"/>'
            '<item id="second" href="second.xhtml" '
            'media-type="application/xhtml+xml"/>'
            '<item id="red" href="red.png" media-type="image/png"/>'
            '<item id="blue" href="blue.png" media-type="image/png"/></manifest>'
            '<spine><itemref idref="first"/><itemref idref="second"/></spine>'
            "</package>"
        )
        if cover:
            properties = ' properties="cover-image"' if cover == "epub3" else ""
            package = package.replace(
                "</manifest>",
                '<item id="cover" href="green.png" media-type="image/png"'
                f"{properties}/></manifest>",
            )
            if cover == "epub2":
                package = package.replace(
                    "</metadata>", '<meta name="cover" content="cover"/></metadata>'
                ).replace('version="3.0"', 'version="2.0"')
        archive.writestr("book.opf", package)
        for name, body in [("first", '<img src="red.png"/>'), ("second", second_body)]:
            archive.writestr(
                f"{name}.xhtml",
                '<html xmlns="http://www.w3.org/1999/xhtml">'
                f"<head><title>{name}</title></head><body>{body}</body></html>",
            )
        for color in ["red", "blue", "green"]:
            image = BytesIO()
            Image.new("RGB", (300, 500), color).save(image, "PNG")
            archive.writestr(f"{color}.png", image.getvalue())
    return path


@pytest.fixture
def epub_book(app, library):
    path = make_epub(library / "EPUB Preview.epub")
    app.extensions["store"].sync_files([BookFile(str(path), "epub", "b" * 64)])
    return app.extensions["store"].books()[0]


def test_epub_details_offer_page_controls_and_modal(client, epub_book):
    response = client.get(f"/books/{epub_book.id}")
    assert response.status_code == 200
    assert (
        f'data-book-preview="/books/{epub_book.id}/preview/0"'.encode() in response.data
    )
    assert b'data-preview-start="0"' in response.data
    assert b'aria-label="Previous EPUB page"' in response.data
    assert b'aria-label="Next EPUB page"' in response.data
    assert b'aria-label="Open larger EPUB preview"' in response.data
    assert b'id="book-preview-modal"' in response.data
    assert b'id="book-preview-modal-title">EPUB Preview</h2>' in response.data
    assert b"PDF preview" not in response.data
    modal = response.data.split(b'id="book-preview-modal"', 1)[1]
    open_url = f"/books/{epub_book.id}/files/{epub_book.files[0].id}/open"
    assert f'action="{open_url}" method="post"'.encode() in modal
    assert b'aria-label="Open EPUB externally"' in modal


@pytest.mark.parametrize("size", ["", "large"])
def test_epub_renders_pages_across_chapters_in_reading_order(client, epub_book, size):
    for number, color in [(1, (255, 0, 0)), (2, (0, 0, 255))]:
        response = client.get(f"/books/{epub_book.id}/preview/{number}?size={size}")
        assert response.status_code == 200
        assert response.mimetype == "image/png"
        assert response.headers["X-Page-Count"] == "2"
        with Image.open(BytesIO(response.data)) as image:
            assert image.width <= (1332 if size else 666)
            assert image.height <= (2000 if size else 1000)
            assert image.getpixel((image.width // 2, image.height // 2)) == color
    assert client.get(f"/books/{epub_book.id}/preview/-1").status_code == 404
    assert client.get(f"/books/{epub_book.id}/preview/3").status_code == 404


@pytest.mark.parametrize("cover", ["epub2", "epub3"])
@pytest.mark.parametrize("size", ["", "large"])
def test_epub_starts_with_declared_cover_outside_reading_order(
    client, epub_book, cover, size
):
    make_epub(epub_book.files[0].file_path, cover=cover)
    for number, color in [(0, (0, 128, 0)), (1, (255, 0, 0)), (0, (0, 128, 0))]:
        response = client.get(f"/books/{epub_book.id}/preview/{number}?size={size}")
        assert response.status_code == 200
        assert response.headers["X-Page-Count"] == "2"
        with Image.open(BytesIO(response.data)) as image:
            expected = (*color, 255) if number == 0 else color
            assert image.getpixel((image.width // 2, image.height // 2)) == expected
            if number == 0:
                assert image.mode == "RGBA"
                assert image.size == ((1332, 2000) if size else (666, 1000))


def test_epub_without_declared_cover_uses_cover_fallback(client, epub_book):
    response = client.get(f"/books/{epub_book.id}/preview/0")
    assert response.status_code == 200
    assert response.headers["X-Page-Count"] == "2"
    with Image.open(BytesIO(response.data)) as image:
        assert image.getpixel((image.width // 2, image.height // 2)) == (255, 0, 0, 255)


def test_epub_text_pagination_is_identical_at_both_resolutions(client, epub_book):
    make_epub(
        epub_book.files[0].file_path,
        second_body="<p>A paragraph of text that flows across pages.</p>" * 200,
    )
    counts = []
    for size in ["", "large"]:
        response = client.get(f"/books/{epub_book.id}/preview/2?size={size}")
        assert response.status_code == 200
        counts.append(int(response.headers["X-Page-Count"]))
        with Image.open(BytesIO(response.data)) as image:
            assert image.convert("L").getextrema()[0] < 200
    assert counts[0] == counts[1]
    assert counts[0] > 2
    assert client.get(f"/books/{epub_book.id}/preview/{counts[0]}").status_code == 200


def test_corrupt_epub_fails_without_exposing_file_path(client, epub_book):
    from pathlib import Path

    path = Path(epub_book.files[0].file_path)
    path.write_text("Invalid EPUB")
    response = client.get(f"/books/{epub_book.id}/preview/1")
    assert response.status_code == 422
    assert str(path).encode() not in response.data
    assert client.get(f"/books/{epub_book.id}").status_code == 200


def test_pdf_is_preferred_when_both_formats_are_present(
    app, client, library, epub_book
):
    pdf = library / "EPUB Preview.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.draw_rect(page.rect, color=(0, 1, 0), fill=(0, 1, 0))
        document.save(pdf)
    app.extensions["store"].sync_files(
        [epub_book.files[0], BookFile(str(pdf), "pdf", "a" * 64)]
    )
    details = client.get(f"/books/{epub_book.id}")
    assert b'aria-label="Previous PDF page"' in details.data
    assert b'id="book-preview-modal-title">EPUB Preview</h2>' in details.data
    assert b'data-preview-start="1"' in details.data
    assert b"EPUB preview" not in details.data
    book = app.extensions["store"].book(epub_book.id)
    pdf_file = next(file for file in book.files if file.extension == "pdf")
    modal = details.data.split(b'id="book-preview-modal"', 1)[1]
    open_url = f"/books/{book.id}/files/{pdf_file.id}/open"
    assert f'action="{open_url}" method="post"'.encode() in modal
    assert modal.index(b'aria-label="Open PDF externally"') < modal.index(
        b'aria-label="Close preview"'
    )
    assert b'aria-label="Open EPUB externally"' not in modal
    assert client.get(f"/books/{epub_book.id}/preview/0").status_code == 404
    response = client.get(f"/books/{epub_book.id}/preview/1")
    assert response.status_code == 200
    assert response.headers["X-Page-Count"] == "1"
    with Image.open(BytesIO(response.data)) as image:
        assert image.getpixel((image.width // 2, image.height // 2)) == (0, 255, 0)
